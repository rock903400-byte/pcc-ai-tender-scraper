# -*- coding: utf-8 -*-
"""
政府電子採購網 - AI 與資訊勞務最低標標案爬蟲 Streamlit 版 (UI/UX 增強版)

原桌面版 (ttkbootstrap) 已移除，全面改以 Streamlit 提供操作介面。
核心爬取/解析/校驗邏輯仍共用自 pcc_core / pcc_mirror，與 CLI 版 crawler.py 為同一份實作。

執行方式：
    streamlit run app.py

特色：
- 側邊欄集中所有搜尋條件（關鍵字、日期模式、採購性質、決標方式等）
- 主區域五個分頁：🏆 精選 / 📋 所有標案 / 📊 數據分析 / ⭐ 追蹤清單 / 📝 執行紀錄
- 📊 數據分析看板：預算級距分佈、Top 10 發包機關、關鍵字熱度、截標急迫度統計
- 🎛️ 進階動態篩選器：預算區間滑桿、截標急迫度、決標狀態過濾
- ⭐ 標案收藏/追蹤清單：一鍵加入追蹤、本地持久化與獨立匯出
- st.dataframe 原生排序/篩選，LinkColumn 直接開啟官方頁面
- 背景執行緒非阻塞搜尋 + 即時進度/日誌 + 可中途停止
- 一鍵下載 Excel / CSV（同時自動備份至 output/，舊檔自動輪替）
- 快取/待確認佇列/設定檔/追蹤清單完全持久化（output/award_cache.json 等）
- 手動「立即補齊」非阻塞執行，結果寫回快取並刷新表格
"""

import glob
import io
import json
import os
import queue
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st

import pcc_core as core
from config import DEFAULT_KEYWORDS, USE_MIRROR_SOURCE
from ui_logic import (
    DISPLAY_COLUMNS,
    DISQUALIFIED_PREFIX,
    KEYWORDS_MAX_CHARS,
    KEYWORDS_MAX_WORDS,
    KEYWORDS_MAX_WORD_LEN,
    PENDING_PREFIX,
    SORT_OPTIONS,
    award_composition_frame,
    budget_tier_frame,
    build_advanced_display_rows,
    describe_filter,
    get_days_remaining,
    is_award_pending,
    keyword_ranking_frame,
    kpi_summary,
    parse_deadline_date,
    sanitize_excel_value,
    sanitize_rows,
    sort_tenders,
    tenders_to_dataframe,
    top_agencies_frame,
    urgency_bins_frame,
    validate_keywords,
)
import watchlist_utils as wl_utils
from app_export import (
    auto_export_backup as _auto_export_backup,
    build_csv_bytes,
    build_excel_bytes,
    cached_csv_bytes,
    cached_excel_bytes,
    rows_fingerprint,
    tender_key,
    tender_label,
)

core.install_ipv4_preference()

# ==================== 常數 ====================

SEARCH_PROGRESS_SHARE = 70
DEFAULT_DAYS_OPTIONS = ["1 (今日)", "3", "7", "14", "30", "60"]
ATTR_OPTIONS = ["勞務", "不限", "財物", "工程"]
AWARD_OPTIONS = ["最低標", "不限", "最有利標/評選"]
DATE_MODE_OPTIONS = [f"{core.DATE_MODE_SPDT} (現正招標中)", core.DATE_MODE_RANGE]

URGENCY_OPTIONS = [
    "全部",
    "🔥 3天內即將截標 (≤3天)",
    "⏳ 7天內截標 (≤7天)",
    "📅 充足 (8~14天)",
    "🗓️ 充裕 (>14天)",
    "⌛ 排除已截標",
]

AWARD_STATUS_FILTER_OPTIONS = [
    "全部",
    "✅ 僅已確認 (鏡像/官方)",
    "🟠 僅待確認 (推估)",
]

OUTPUT_DIR = os.path.abspath("output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

WATCHLIST_FILENAME = "watchlist.json"

MAX_LOG_LINES = 1000
LOG_KEEP = 500
MAX_OUTPUT_KEEP = 20
RATE_LIMIT_SECONDS = 60
LOG_LOCK = threading.Lock()

# 單一定義的欄位設定，三個分頁共用（延後初始化以符合 set_page_config 必須為首個 st 呼叫之規範，修復 Cloud healthz 8501 衝突）
def get_tender_column_config():
    """回傳表格欄位設定，需在 st.set_page_config 之後呼叫。"""
    return {
        "#": st.column_config.NumberColumn("#", width="small"),
        "公告日期": st.column_config.TextColumn("公告日期", width="small"),
        "招標機關": st.column_config.TextColumn("招標機關", width="medium"),
        "標案名稱": st.column_config.TextColumn("標案名稱", width="large"),
        "預算金額": st.column_config.NumberColumn("預算金額", width="small", help="點擊此欄可按金額正確排序（數值排序）", format="%d 元"),
        "決標方式": st.column_config.TextColumn("決標方式", width="medium", help="🟠=推估待確認，⚪=已確認但不符目前決標篩選"),
        "招標方式": st.column_config.TextColumn("招標方式", width="medium"),
        "決標方式來源": st.column_config.TextColumn("決標依據", width="small"),
        "截止投標": st.column_config.TextColumn("截止投標", width="small"),
        "剩餘天數": st.column_config.TextColumn("剩餘天數", width="small", help="🔥≤3天 ⏳4-7天 📅8-14天 🗓️>14天 已截標/— 為特殊狀態；上方「排序」選單可做數值排序"),
        "命中關鍵字": st.column_config.TextColumn("命中關鍵字", width="small"),
        "詳細連結": st.column_config.LinkColumn("詳細連結", display_text="🔗 開啟", width="small", help="點擊開啟官方公告頁面"),
    }


# 相容舊引用：實際值在 set_page_config 後初始化（避免在 import 時期即呼叫 st.column_config）
TENDER_COLUMN_CONFIG = None


def table_height(row_count: int, max_rows: int = 18, cap: int = 580) -> int:
    """依列數計算表格高度，避免重複的魔術數字。"""
    return min(cap, 80 + 35 * min(row_count, max_rows))


def render_filter_panel(key_prefix: str, rows: list = None) -> dict:
    """渲染快速搜尋與進階篩選器，回傳過濾參數 dict。（C4：預算上限依資料驅動，9分版新增機關篩選/排序/一鍵清除）"""
    import math
    from collections import Counter
    # 依實際資料計算上限，進位到百萬元整，空資料沿用 2000
    if rows:
        max_wan = max([core.parse_amount(t.get("預算金額", "")) for t in rows] + [0]) / 10000.0
        slider_max = int(math.ceil(max_wan / 100.0) * 100) or 2000
        # 無資料或最大值≤2000萬時保底 2000，避免小資料集滑桿過窄；>2000萬則依實際放大
        slider_max = max(slider_max, 2000) if max_wan <= 2000 else slider_max
        # 機關選項：依本次結果 Top 30
        cnt = Counter(t.get("招標機關", "未知機關") for t in rows if t.get("招標機關"))
        agency_options = [a for a, _ in cnt.most_common(30)]
    else:
        slider_max = 2000
        agency_options = []
    col_q, col_exp = st.columns([3, 1])
    with col_q:
        q = st.text_input("🔍 快速搜尋（機關 / 案名 / 案號 / 關鍵字）", key=f"filter_{key_prefix}", placeholder="輸入關鍵字即時過濾…")
    with col_exp:
        st.write("")
        st.caption("👇 可展開下方進行多維度篩選與排序")
    # 進階篩選器：預設收合，但若已有篩選則自動展開提升可見性
    has_active = False
    try:
        if st.session_state.get(f"budget_slider_{key_prefix}", (0, slider_max)) != (0, slider_max):
            has_active = True
        if st.session_state.get(f"urgency_sel_{key_prefix}", "全部") != "全部":
            has_active = True
        if st.session_state.get(f"award_sel_{key_prefix}", "全部") != "全部":
            has_active = True
        if st.session_state.get(f"agency_{key_prefix}", []):
            has_active = True
    except Exception:
        has_active = False
    with st.expander("🎛️ 進階條件篩選器 (預算金額 / 截標急迫度 / 決標狀態 / 招標機關)", expanded=has_active):
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            budget_range = st.slider(
                "預算金額範圍 (萬元)",
                min_value=0,
                max_value=slider_max,
                value=(0, slider_max),
                step=50,
                key=f"budget_slider_{key_prefix}",
                help=f"上限依本次資料自動調整至 {slider_max} 萬元，超過上限的標案將被篩掉",
            )
        with fc2:
            urgency_sel = st.selectbox("截止投標急迫度", options=URGENCY_OPTIONS, key=f"urgency_sel_{key_prefix}")
        with fc3:
            award_status_sel = st.selectbox("決標校驗狀態", options=AWARD_STATUS_FILTER_OPTIONS, key=f"award_sel_{key_prefix}")
        # 招標機關多選（第二列，全寬）
        if agency_options:
            agency_sel = st.multiselect(
                "🏛️ 招標機關篩選（可複選，子字串比對）",
                options=agency_options,
                key=f"agency_{key_prefix}",
                placeholder="全部機關",
                help="依本次結果 Top 30 機關，支援子字串比對，可複選過濾",
            )
        else:
            agency_sel = st.multiselect(
                "🏛️ 招標機關篩選",
                options=[],
                key=f"agency_{key_prefix}",
                placeholder="無可選機關",
                disabled=True,
            )
            agency_sel = []
        # 已套用摘要 + 清除按鈕
        active_tags = []
        if budget_range != (0, slider_max):
            active_tags.append(f"預算 {budget_range[0]}~{budget_range[1]}萬")
        if urgency_sel != "全部":
            active_tags.append(urgency_sel)
        if award_status_sel != "全部":
            active_tags.append(award_status_sel)
        if agency_sel:
            active_tags.append(f"機關 {len(agency_sel)} 項")
        if active_tags:
            st.caption(f"🔎 已套用 {len(active_tags)} 項：{' · '.join(active_tags)}")
        if st.button("🧹 清除進階篩選", key=f"clear_filter_{key_prefix}", help="重置預算、急迫度、決標狀態與機關篩選"):
            # 重置為預設值（直接賦值避免刪除 widget key 時的競態）
            st.session_state[f"budget_slider_{key_prefix}"] = (0, slider_max)
            st.session_state[f"urgency_sel_{key_prefix}"] = "全部"
            st.session_state[f"award_sel_{key_prefix}"] = "全部"
            st.session_state[f"agency_{key_prefix}"] = []
            st.rerun()
    # 排序控制器：常駐顯示，解決剩餘天數文字排序不準（C2 遺留）
    col_sort, col_info = st.columns([2, 3])
    with col_sort:
        sort_opt = st.selectbox("↕️ 排序方式", options=SORT_OPTIONS, key=f"sort_{key_prefix}", help="「剩餘天數」為文字，此處提供數值排序；預設為公告日期新→舊")
    with col_info:
        st.write("")
        if active_tags:
            st.caption(f"⬆️ 排序已設為「{sort_opt}」· 篩選已套用 {len(active_tags)} 項")
        else:
            st.caption(f"⬆️ 排序已設為「{sort_opt}」· 尚未套用進階篩選")
    min_b = budget_range[0] if budget_range[0] > 0 else 0
    max_b = budget_range[1]
    if max_b == 0:
        max_b = 0
    return {"query": q, "min_budget_wan": min_b, "max_budget_wan": max_b, "urgency": urgency_sel, "award_status": award_status_sel, "agencies": agency_sel, "sort": sort_opt}


def render_tender_table(rows: list, key_prefix: str, caption: str, max_rows: int = 18, cap: int = 580, selectable: bool = False) -> None:
    """渲染標案表格（共用欄位設定與高度計算）。支援可選取模式（C3），9分版優化 caption 與空狀態。"""
    if not rows:
        return
    df = tenders_to_dataframe(rows, st.session_state.active_award_target)
    display_df = df[DISPLAY_COLUMNS] if set(DISPLAY_COLUMNS).issubset(df.columns) else df
    if selectable:
        # 可選取模式：表格本身支援多列選取，移除原本的下拉選單
        event = st.dataframe(
            display_df,
            width="stretch",
            hide_index=True,
            height=table_height(len(display_df), max_rows, cap),
            column_config=TENDER_COLUMN_CONFIG,
            on_select="rerun",
            selection_mode="multi-row",
            key=f"table_{key_prefix}",
        )
        st.caption(caption + " · 勾選列後按下方按鈕加入追蹤 · 欄頭點擊可再排序")
        # 顯示缺少案號提示（不可選）
        missing = sum(1 for r in rows if not tender_key(r))
        if missing > 0:
            st.caption(f"有 {missing} 筆標案缺少案號，無法加入追蹤")
        # 取得選取列（位置索引對應傳入的 rows 順序）
        try:
            selected_idx = event.selection.rows if hasattr(event, "selection") and event.selection is not None else []
        except Exception:
            selected_idx = []
        if selected_idx is None:
            selected_idx = []
        selected_count = len(selected_idx)
        # 按鈕顯示已選筆數
        btn_label = f"➕ 加入追蹤（已選 {selected_count} 筆）" if selected_count else "➕ 加入追蹤（請先勾選）"
        if st.button(btn_label, width="stretch", key=f"btn_add_bm_{key_prefix}", disabled=(selected_count == 0)):
            if selected_idx:
                wl = st.session_state.watchlist
                added_count = 0
                for i in selected_idx:
                    if 0 <= i < len(rows):
                        t = rows[i]
                        key_id = tender_key(t)
                        if key_id and key_id not in wl:
                            wl[key_id] = {"added_at": datetime.now().isoformat(), "snapshot": t}
                            added_count += 1
                st.session_state.watchlist = wl
                save_watchlist(wl)
                st.toast(f"已加入 {added_count} 筆標案至追蹤清單！", icon="⭐")
                st.rerun()
    else:
        st.dataframe(
            display_df,
            width="stretch",
            hide_index=True,
            height=table_height(len(display_df), max_rows, cap),
            column_config=TENDER_COLUMN_CONFIG,
        )
        st.caption(caption + " · 欄頭點擊可排序")


def paginate_rows(rows: list, key_prefix: str, default_limit: int = 100) -> tuple:
    """分頁控制器：處理每頁顯示與頁碼按鈕，回傳 (display_rows, pagination_caption)。"""
    DISPLAY_LIMIT_OPTIONS = [50, 100, 300, 1000]
    if not rows:
        return rows, ""
    # 決定預設索引
    try:
        default_idx = DISPLAY_LIMIT_OPTIONS.index(default_limit)
    except ValueError:
        default_idx = 1
    col_limit, _ = st.columns([1, 3])
    with col_limit:
        limit_sel = st.selectbox(
            "📄 每頁顯示",
            options=DISPLAY_LIMIT_OPTIONS + ["全部"],
            index=default_idx,
            key=f"limit_{key_prefix}",
            help="避免一次渲染 6000 筆造成卡頓",
        )
    if limit_sel != "全部" and len(rows) > int(limit_sel):
        page_total = (len(rows) + int(limit_sel) - 1) // int(limit_sel)
        page = st.session_state.get(f"page_{key_prefix}", 1)
        if page > page_total:
            page = 1
            st.session_state[f"page_{key_prefix}"] = 1
        c_prev, c_info, c_next = st.columns([1, 3, 1])
        with c_prev:
            if st.button("◀ 上一頁", key=f"prev_{key_prefix}", disabled=(page <= 1)):
                st.session_state[f"page_{key_prefix}"] = max(1, page - 1)
                st.rerun()
        with c_info:
            st.caption(f"第 {page} / {page_total} 頁 · 共 {len(rows)} 筆，顯示 {int(limit_sel)} 筆/頁")
        with c_next:
            if st.button("下一頁 ▶", key=f"next_{key_prefix}", disabled=(page >= page_total)):
                st.session_state[f"page_{key_prefix}"] = min(page_total, page + 1)
                st.rerun()
        start = (page - 1) * int(limit_sel)
        display = rows[start:start + int(limit_sel)]
        return display, f"第 {page}/{page_total} 頁"
    else:
        if f"page_{key_prefix}" in st.session_state:
            st.session_state[f"page_{key_prefix}"] = 1
        return rows, ""


# ==================== 檔案路徑與狀態 ====================

def award_cache_path() -> str:
    return core.award_cache_path(OUTPUT_DIR)


def pending_queue_path() -> str:
    return core.pending_queue_path(OUTPUT_DIR)


def settings_path() -> str:
    return core.settings_path(OUTPUT_DIR)


def watchlist_path() -> str:
    return wl_utils.watchlist_path(OUTPUT_DIR)


def load_watchlist() -> dict:
    """載入追蹤清單（委派至 watchlist_utils，原子寫 + 舊格式相容）。"""
    return wl_utils.load_watchlist(OUTPUT_DIR)


def save_watchlist(wl: dict) -> bool:
    """儲存追蹤清單（原子寫，委派至 watchlist_utils）。"""
    return wl_utils.save_watchlist(OUTPUT_DIR, wl)


def _default_keywords_str() -> str:
    return " ".join(DEFAULT_KEYWORDS)


def init_session_state():
    defaults = {
        "tenders_all": [],
        "tenders_qualified": [],
        "tenders_keyword_hits": [],
        "tenders_by_pk": {},
        "watchlist": load_watchlist(),
        "active_filter_label": "勞務最低標",
        "active_attr_target": "勞務",
        "active_award_target": "最低標",
        "log_lines": [],
        "notices": [],
        "is_running": False,
        "last_search_summary": "",
        "kw_input": _default_keywords_str(),
        "date_mode": DATE_MODE_OPTIONS[0],
        "days": "7",
        "attr": "勞務",
        "award": "最低標",
        "verify": True,
        "include_misses": False,
        "hide_pending": False,
        "filter_matched": "",
        "filter_all": "",
        "_settings_restored": False,
        # 非阻塞搜尋相關
        "search_queue": None,
        "search_thread": None,
        "stop_event": None,
        "search_progress": 0,
        "search_status": None,
        "search_failed": None,
        "last_search_ts": 0.0,
        # 補齊
        "trickle_queue": None,
        "trickle_thread": None,
        "trickle_stop_event": None,
        "is_trickling": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def append_log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    try:
        with LOG_LOCK:
            st.session_state.log_lines.append(f"[{timestamp}] {message}")
            if len(st.session_state.log_lines) > MAX_LOG_LINES:
                st.session_state.log_lines = st.session_state.log_lines[-LOG_KEEP:]
    except Exception:
        # 降級：若鎖或 session_state 異常，盡力寫入
        try:
            st.session_state.log_lines.append(f"[{timestamp}] {message}")
        except Exception:
            pass


def clear_notices():
    st.session_state.notices = []


def add_notice(message: str):
    if message not in st.session_state.notices:
        st.session_state.notices.append(message)


def ss_get(key: str, default=None):
    """安全取得 session_state 值，兼容 fragment 與主執行緒的不同代理實作。"""
    try:
        return st.session_state[key] if key in st.session_state else default
    except Exception:
        try:
            return getattr(st.session_state, key, default)
        except Exception:
            return default


# ==================== 安全與加固 helpers ====================

def prune_output_excels(keep: int = MAX_OUTPUT_KEEP):
    """委派至 app_export.prune_output_excels（統一輸出輪替邏輯）。"""
    from app_export import prune_output_excels as _prune
    return _prune(OUTPUT_DIR, keep)


# ==================== 設定檔存讀 ====================

def current_settings() -> dict:
    return {
        "keywords": ss_get("kw_input", _default_keywords_str()),
        "date_mode": ss_get("date_mode", DATE_MODE_OPTIONS[0]),
        "days": ss_get("days", "7"),
        "attr": ss_get("attr", "勞務"),
        "award": ss_get("award", "最低標"),
        "verify": bool(ss_get("verify", True)),
        "include_misses": bool(ss_get("include_misses", False)),
        "hide_pending": bool(ss_get("hide_pending", False)),
    }


def save_settings():
    try:
        if not core.save_json_dict(current_settings(), settings_path()):
            append_log(f"  ⚠️ 無法寫入搜尋條件 {settings_path()}，下次開啟不會還原這次的條件。")
    except Exception as e:
        append_log(f"  ⚠️ 儲存搜尋條件失敗: {e}")


def restore_settings():
    saved = core.load_json_dict(settings_path())
    if not saved:
        return False
    keywords = saved.get("keywords")
    if isinstance(keywords, str) and keywords.strip():
        if len(keywords) > KEYWORDS_MAX_CHARS:
            keywords = keywords[:KEYWORDS_MAX_CHARS]
        words = [w for w in re.split(r"[\s,]+", keywords.strip()) if w]
        if len(words) > KEYWORDS_MAX_WORDS:
            keywords = " ".join(words[:KEYWORDS_MAX_WORDS])
        words = [w[:KEYWORDS_MAX_WORD_LEN] if len(w) > KEYWORDS_MAX_WORD_LEN else w for w in words]
        keywords = " ".join(words) if words else keywords
        st.session_state.kw_input = keywords
    mapping = {
        "date_mode": DATE_MODE_OPTIONS,
        "days": DEFAULT_DAYS_OPTIONS,
        "attr": ATTR_OPTIONS,
        "award": AWARD_OPTIONS,
    }
    for key, options in mapping.items():
        val = saved.get(key)
        if val in options:
            st.session_state[key] = val
    for key in ("verify", "include_misses", "hide_pending"):
        if isinstance(saved.get(key), bool):
            st.session_state[key] = saved[key]
    return True


# ==================== 資料處理與多維度篩選 ====================

def selected_date_type() -> str:
    label = ss_get("date_mode", DATE_MODE_OPTIONS[0])
    return core.DATE_TYPE_RANGE if str(label).startswith(core.DATE_MODE_RANGE) else core.DATE_TYPE_SPDT


def refresh_datasets():
    st.session_state.tenders_qualified = core.filter_tenders(
        st.session_state.tenders_all,
        st.session_state.active_attr_target,
        st.session_state.active_award_target,
    )
    st.session_state.tenders_keyword_hits = core.filter_tenders(
        st.session_state.tenders_all,
        st.session_state.active_attr_target,
        st.session_state.active_award_target,
        require_keyword_hit=True,
    )


# ==================== 匯出（已抽至 app_export.py，保留薄包裝以維持舊引用） ====================
# build_excel_bytes / build_csv_bytes / rows_fingerprint / tender_key / tender_label /
# cached_excel_bytes / cached_csv_bytes 已由 from app_export import 取得


def auto_export_backup(all_tenders: list, matched_tenders: list):
    """委派至 app_export.auto_export_backup（統一輪替邏輯）。"""
    return _auto_export_backup(OUTPUT_DIR, all_tenders, matched_tenders, keep=MAX_OUTPUT_KEEP)


# ==================== 非阻塞搜尋核心 ====================

def run_search_thread(keywords: list, days: int, target_attr: str, target_award: str,
                      date_type: str, verify: bool, include_misses: bool,
                      q: queue.Queue, stop_event: threading.Event):
    def qlog(msg: str):
        q.put(("log", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"))
    def qprogress(pct: int):
        q.put(("progress", pct))
    def qstatus(typ: str, msg: str):
        q.put(("status", (typ, msg)))
    def qnotice(msg: str):
        q.put(("notice", msg))

    try:
        if date_type == core.DATE_TYPE_RANGE:
            days = core.clamp_date_range_days(days, log=lambda m: qlog(m))

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        start_ad = start_date.strftime("%Y/%m/%d")
        end_ad = end_date.strftime("%Y/%m/%d")
        proctrg_cate = core.PROCTRG_CATE.get(target_attr)

        if date_type == core.DATE_TYPE_RANGE:
            qlog(f"🚀 全面掃描【{target_attr}】標案：公告日期 {start_ad} ~ {end_ad} (最近 {days} 天)")
        else:
            qlog(f"🚀 全面掃描【{target_attr}】標案：等標期內（現正招標中，站方會忽略日期區間）")
        qlog(f"🏷️ 標記關鍵字共 {len(keywords)} 組: {', '.join(keywords)}")
        qstatus("info", "🔍 搜尋中… 正在翻頁擷取標案")
        qprogress(5)

        def _on_page(done_pages, total_pages):
            pct = int(done_pages / max(total_pages, 1) * SEARCH_PROGRESS_SHARE)
            qprogress(pct)

        rows = core.search_pcc(
            "", start_ad, end_ad, proctrg_cate=proctrg_cate,
            date_type=date_type, log=lambda m: qlog(m),
            progress_cb=_on_page,
            should_stop=stop_event.is_set,
        )
        if stop_event.is_set():
            qlog("⏹ 已停止搜尋，以下為中斷前取得的部分結果。")

        unique_tenders = {}
        core.merge_by_tender_id(unique_tenders, rows, "")
        tenders_list = list(unique_tenders.values())
        core.tag_keywords(tenders_list, keywords)

        hits = sum(1 for t in tenders_list if t.get("命中關鍵字群"))
        qlog(f"📦 掃描完畢，共 {len(tenders_list)} 筆不重複標案（其中 {hits} 筆命中關鍵字）。")
        qprogress(SEARCH_PROGRESS_SHARE)

        cache_path = award_cache_path()
        cache = core.load_award_cache(cache_path)
        applied = core.apply_award_cache(tenders_list, cache)
        if applied:
            qlog(f"♻️ 由快取套用 {applied} 筆先前已確認的官方決標方式（不必重查）。")

        pending_rows = core.select_rows_for_enrichment(
            tenders_list, target_attr, target_award, limit=0,
            require_keyword_hit=not include_misses)
        if core.save_pending_queue(pending_rows, pending_queue_path()):
            qlog(f"🗂️ 待確認清單已更新（{len(pending_rows)} 筆），可手動按「立即補齊」持續確認。")
        else:
            qlog(f"  ⚠️ 無法寫入待確認清單 {pending_queue_path()}，補齊功能將無東西可撿。")

        if stop_event.is_set():
            qlog("⏹ 已略過深度校驗（搜尋已被停止）。")
            verify = False

        if verify and tenders_list:
            targets = core.select_rows_for_enrichment(
                tenders_list, target_attr, target_award,
                require_keyword_hit=not include_misses)
            # Cloud 環境保守：縮小單次校驗量避免 90s+ 阻塞導致前端超時跳掉（H5 完整加固）
            is_cloud = os.getenv("STREAMLIT_SERVER_HEADLESS") == "1" or os.path.exists("/mount/src")
            if is_cloud and len(targets) > 30:
                qlog(f"  [Cloud] 候選 {len(targets)} 筆較多，為避免雲端超時，本次僅校驗前 30 筆，剩餘可手動按「立即補齊」。")
                targets = targets[:30]

            def _on_progress(done, total):
                # H5 修復：每筆皆更新進度，避免 70-100% 龜速假死；每 5 筆更新狀態文字
                share = 100 - SEARCH_PROGRESS_SHARE
                qprogress(SEARCH_PROGRESS_SHARE + int(done / total * share))
                if done % 5 == 0 or done == total:
                    qstatus("info", f"⚡ 校驗決標方式中… ({done}/{total} 筆候選)")

            if targets:
                qlog(f"⚡ 從 {len(tenders_list)} 筆中挑出 {len(targets)} 筆尚未確認的候選，校驗真實決標方式（主來源：公開資料鏡像）...")
                qstatus("info", f"⚡ 校驗決標方式中… ({len(targets)} 筆候選)")
                stats = core.enrich_actual_award_methods(
                    targets, progress_cb=_on_progress, log=lambda m: qlog(m),
                    cache=cache, cache_path=cache_path,
                    should_stop=stop_event.is_set,
                    use_mirror=USE_MIRROR_SOURCE)
                if stats["blocked"]:
                    qnotice(f"官網詳細頁額度已用盡，本次只確認 {stats['ok']} 筆。已確認的都寫進快取了，下次搜尋會直接套用。")
                    qlog(f"⛔ 校驗提前中止：本次確認 {stats['ok']} 筆，官網詳細頁額度已用盡。已確認的都已寫入快取，下次搜尋會直接套用。")
                else:
                    qlog(f"✅ 校驗完成：本次確認 {stats['ok']}/{stats['total']} 筆，其餘維持「{core.AWARD_SOURCE_ESTIMATED}」。")
            else:
                qlog("ℹ️ 無需校驗的候選（皆已確認或不符條件）。")
        elif not verify:
            qlog(f"ℹ️ 已關閉深度校驗，未快取的標案決標方式全部為「{core.AWARD_SOURCE_ESTIMATED}」。")

        core.finalize_keywords(tenders_list)

        qualified = core.filter_tenders(tenders_list, target_attr, target_award)
        keyword_hits = core.filter_tenders(tenders_list, target_attr, target_award, require_keyword_hit=True)

        qlog(f"🎯 符合【{target_attr} + {target_award}】共 {len(qualified)} 筆，其中命中關鍵字 {len(keyword_hits)} 筆。")

        pending = sum(1 for t in qualified if is_award_pending(t))
        if pending:
            qlog(f"⚠️ 精選中有 {pending} 筆決標方式仍是「公開取得 (待確認)」（🟠）。")
            qnotice(f"精選中有 {pending} 筆決標方式是推估的（🟠）——投標前請開啟詳細連結確認。")

        if stop_event.is_set():
            qnotice("本次搜尋被中斷，清單只涵蓋中斷前抓到的部分標案。")

        qprogress(100)
        was_stopped = stop_event.is_set()
        if was_stopped:
            qstatus("warning", f"⏹ 已停止：共 {len(tenders_list)} 筆標案，符合條件 {len(qualified)} 筆")
        else:
            qstatus("success", f"🎉 搜尋完成！共 {len(tenders_list)} 筆標案，符合條件 {len(qualified)} 筆，命中關鍵字 {len(keyword_hits)} 筆。")

        rel = None
        if tenders_list:
            matched_for_export = qualified if include_misses else keyword_hits
            rel = auto_export_backup(tenders_list, matched_for_export)
            if rel and not rel.startswith("失敗"):
                qlog(f"💾 已自動備份 Excel 至 {rel}")
            elif rel:
                qlog(f"  ⚠️ 自動備份失敗: {rel}")

        by_pk = {t["pk"]: t for t in tenders_list if t.get("pk")}
        q.put(("done", {
            "tenders_all": tenders_list,
            "qualified": qualified,
            "keyword_hits": keyword_hits,
            "by_pk": by_pk,
            "summary": f"共 {len(tenders_list)} 筆標案，符合條件 {len(qualified)} 筆，命中關鍵字 {len(keyword_hits)} 筆",
            "was_stopped": was_stopped,
        }))

    except Exception as e:
        qlog(f"❌ 搜尋過程發生未預期錯誤: {e.__class__.__name__}: {e}")
        try:
            tb_text = traceback.format_exc().rstrip()
            err_path = os.path.join(OUTPUT_DIR, "app_error.log")
            with open(err_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now().isoformat()}] {e.__class__.__name__}: {e}\n{tb_text}\n")
        except Exception:
            pass
        qstatus("error", f"❌ 搜尋失敗: {e.__class__.__name__}: {e}")
        qprogress(0)
        q.put(("failed", f"{e.__class__.__name__}: {e}"))


def run_trickle_thread(q: queue.Queue, stop_event: threading.Event):
    def qlog(msg: str):
        q.put(("log", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"))
    try:
        result = core.trickle_verify(award_cache_path(), pending_queue_path(), batch=core.DEFAULT_TRICKLE_BATCH, log=lambda m: qlog(m), should_stop=stop_event.is_set, use_mirror=USE_MIRROR_SOURCE)
        qlog(f"🔄 補齊批次：picked {result.get('picked',0)}、ok {result.get('ok',0)}、remaining {result.get('remaining',0)}")
        q.put(("trickle_result", result))
    except Exception as e:
        qlog(f"  ⚠️ 補齊過程錯誤: {e.__class__.__name__}: {e}")
        try:
            tb_text = traceback.format_exc().rstrip()
            err_path = os.path.join(OUTPUT_DIR, "app_error.log")
            with open(err_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now().isoformat()}] trickle {e.__class__.__name__}: {e}\n{tb_text}\n")
        except Exception:
            pass
        q.put(("trickle_failed", str(e)))


# ==================== 視覺化看板輔助 ====================

def render_analytics_dashboard(tenders: list, qualified: list, keyword_hits: list):
    """繪製大盤統計分析看板。"""
    if not tenders:
        render_empty_state("📊", "尚未載入分析數據", "請先在左側設定條件並完成一次搜尋，即可即時查看標案市場大盤圖表")
        return

    # 計算統計指標（抽至 ui_logic.kpi_summary，僅排版留在此）
    kpi = kpi_summary(tenders)
    total_budget = kpi["total_budget"]
    avg_budget = kpi["avg_budget"]
    max_amount = kpi["max_amount"]
    max_tender_name = kpi["max_tender_name"]
    confirmed_count = kpi["confirmed_count"]
    confirmed_ratio = kpi["confirmed_ratio"]

    # 頂部 KPI 卡片
    k1, k2, k3, k4 = st.columns(4)
    budget_yi = total_budget / 100000000.0
    k1.metric("💰 總預算規模", f"{budget_yi:.2f} 億元" if budget_yi >= 1 else f"{total_budget/10000:.0f} 萬元")
    k2.metric("📈 平均標案金額", f"{avg_budget/10000:.1f} 萬元")
    k3.metric("🏆 最高單一預算", f"{max_amount/10000:.0f} 萬元", help=max_tender_name)
    k4.metric("🛡️ 官方/鏡像確認率", f"{confirmed_ratio:.1f}%", delta=f"{confirmed_count}/{len(tenders)} 筆")

    st.write("")

    # 第一列圖表：預算級距分佈 & 決標方式佔比
    col_c1, col_c2 = st.columns([3, 2])
    with col_c1:
        st.subheader("📊 標案預算級距分佈")
        tier_df = budget_tier_frame(tenders)
        tier_order = [
            "< 100萬 (公告金額以下)",
            "100萬 ~ 500萬",
            "500萬 ~ 1,000萬",
            "1,000萬 ~ 5,000萬 (查核金額)",
            "5,000萬 ~ 2億",
            "≥ 2億 (巨額採購)",
            "未公開 / 0 元",
        ]
        chart_tier = alt.Chart(tier_df).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6).encode(
            x=alt.X("級距:N", sort=tier_order, title="預算級距", axis=alt.Axis(labelAngle=-25)),
            y=alt.Y("標案筆數:Q", title="標案筆數"),
            color=alt.Color("標案筆數:Q", scale=alt.Scale(scheme="blues"), legend=None),
            tooltip=["級距", "標案筆數"],
        ).properties(height=300)
        st.altair_chart(chart_tier, use_container_width=True)

    with col_c2:
        st.subheader("⚖️ 決標方式構成佔比")
        award_df = award_composition_frame(tenders)
        chart_award = alt.Chart(award_df).mark_arc(innerRadius=50).encode(
            theta=alt.Theta("筆數:Q"),
            color=alt.Color("決標方式類別:N", scale=alt.Scale(scheme="category10")),
            tooltip=["決標方式類別", "筆數"],
        ).properties(height=300)
        st.altair_chart(chart_award, use_container_width=True)

    st.divider()

    # 第二列圖表：Top 10 招標機關 & 關鍵字熱度排行
    col_c3, col_c4 = st.columns(2)
    with col_c3:
        st.subheader("🏛️ Top 10 發包招標機關（依標案筆數）")
        agency_df = top_agencies_frame(tenders, top_n=10)
        if not agency_df.empty:
            chart_agency = alt.Chart(agency_df).mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4).encode(
                x=alt.X("標案筆數:Q", title="標案筆數"),
                y=alt.Y("招標機關:N", sort="-x", title="機關名稱"),
                color=alt.Color("累積預算(萬元):Q", scale=alt.Scale(scheme="tealblues")),
                tooltip=["招標機關", "標案筆數", "累積預算(萬元)"],
            ).properties(height=320)
            st.altair_chart(chart_agency, use_container_width=True)
        else:
            st.info("尚無機關數據")

    with col_c4:
        st.subheader("🏷️ 關鍵字命中排行榜")
        kw_df = keyword_ranking_frame(tenders, top_n=12)
        if not kw_df.empty:
            chart_kw = alt.Chart(kw_df).mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4).encode(
                x=alt.X("命中次數:Q", title="命中標案筆數"),
                y=alt.Y("關鍵字:N", sort="-x", title="關鍵字"),
                color=alt.Color("命中次數:Q", scale=alt.Scale(scheme="purples")),
                tooltip=["關鍵字", "命中次數"],
            ).properties(height=320)
            st.altair_chart(chart_kw, use_container_width=True)
        else:
            st.info("本次搜尋結果皆未命中任何標記關鍵字")

    st.divider()

    # 第三列：截標急迫度統計
    st.subheader("⏳ 截標急迫度時間軸分析")
    urgency_df = urgency_bins_frame(tenders)
    # 與 ui_logic.py:350 urgency_bins 鍵順序一致，勿用未定義的 urgency_bins 變數（曾致 NameError）
    urgency_order = ["🔥 3天內即將截標", "⏳ 4~7天內截標", "📅 8~14天內截標", "🗓️ 14天以上", "⌛ 已截標 / 截止日期未定"]
    chart_urgency = alt.Chart(urgency_df).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6).encode(
        x=alt.X("急迫度分類:N", sort=urgency_order, title="急迫度分類"),
        y=alt.Y("標案數量:Q", title="標案數量"),
        color=alt.Color("急迫度分類:N", scale=alt.Scale(
            domain=urgency_order,
            range=["#EF4444", "#F59E0B", "#3B82F6", "#10B981", "#64748B"]
        ), legend=None),
        tooltip=["急迫度分類", "標案數量"],
    ).properties(height=260)
    st.altair_chart(chart_urgency, use_container_width=True)


# ==================== Streamlit 頁面 ====================

st.set_page_config(
    page_title="政府採購網 - AI/資訊 勞務最低標爬蟲",
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 延後初始化欄位設定（必須在 set_page_config 之後，否則 Cloud 會因 st.* 順序違規導致 healthz 失敗）
TENDER_COLUMN_CONFIG = get_tender_column_config()

init_session_state()

if not st.session_state._settings_restored:
    restored = restore_settings()
    st.session_state._settings_restored = True
    if restored:
        st.session_state.active_attr_target = st.session_state.attr
        st.session_state.active_award_target = st.session_state.award
        st.session_state.active_filter_label = describe_filter(st.session_state.attr, st.session_state.award)

def render_empty_state(icon: str, title: str, body_html: str) -> None:
    """渲染空狀態插圖，使用主題變數確保深淺主題皆可讀。"""
    st.markdown(
        f"""
        <div class="empty-illustration">
            <div style="font-size: 42px;">{icon}</div>
            <div style="font-size: 16px; font-weight: 600; margin: 8px 0;">{title}</div>
            <div style="font-size: 13px; line-height: 1.6;">{body_html}</div>
        </div>
        """,
        unsafe_allow_html=True,  # nosec: static HTML only, no tender data interpolated
    )


# ---------- Header & CSS ----------
st.markdown(
    """
    <style>
    .block-container {padding-top: 1.2rem; padding-bottom: 1rem;}
    /* 卡片化 metric，使用主題變數，淺色/深色皆可讀 */
    [data-testid="stMetric"] {
        background: var(--secondary-background-color);
        border: 1px solid color-mix(in srgb, var(--text-color) 12%, transparent);
        border-radius: 12px;
        padding: 14px 16px;
        box-shadow: 0 2px 8px color-mix(in srgb, var(--text-color) 8%, transparent);
        transition: transform 0.2s ease, border-color 0.2s ease;
    }
    [data-testid="stMetric"]:hover {
        border-color: var(--primary-color);
        transform: translateY(-2px);
    }
    [data-testid="stMetricLabel"] {font-size: 0.85rem; color: color-mix(in srgb, var(--text-color) 65%, transparent); font-weight: 500;}
    [data-testid="stMetricValue"] {font-weight: 700; color: var(--text-color);}
    [data-testid="stMetricDelta"] {font-size: 0.8rem; color: var(--text-color);}
    /* 空狀態 - 使用主題變數，邊框與文字在兩主題下皆清晰 */
    .empty-illustration {
        text-align: center;
        padding: 40px 24px;
        background: var(--secondary-background-color);
        border: 1px dashed color-mix(in srgb, var(--text-color) 20%, transparent);
        border-radius: 14px;
        margin: 18px 0;
        color: var(--text-color);
    }
    .empty-illustration div {color: var(--text-color) !important;}
    /* 表格與按鈕微調，使用主題變數 */
    [data-testid="stDataFrame"] {border-radius: 10px; overflow: hidden; border: 1px solid color-mix(in srgb, var(--text-color) 12%, transparent);}
    .stButton>button {border-radius: 8px; font-weight: 500;}
    .badge-pill {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 600;
        margin-right: 6px;
    }
    .badge-blue {background: var(--primary-color); color: var(--background-color);}
    .badge-amber {background: #D97706; color: #FFFFFF;}
    </style>
    """,
    unsafe_allow_html=True,  # nosec: static CSS only
)

col_title, col_badge = st.columns([5, 1])
with col_title:
    st.title("🏛️ 政府電子採購網 — AI / 資訊 勞務最低標標案爬蟲")
    st.caption("全面掃描該條件下全部標案 · 關鍵字標記與多維度篩選 · 決標方式以公開資料鏡像為主、官網詳細頁備援 · 結果自動快取於 output/")
with col_badge:
    if st.session_state.tenders_all:
        st.success(f"✅ 已載入 {len(st.session_state.tenders_all)} 筆")
    else:
        st.info("就緒")

# ---------- Metrics ----------
if st.session_state.tenders_all:
    _m_total = len(st.session_state.tenders_all)
    _m_qual = len(st.session_state.tenders_qualified)
    _m_hits = len(st.session_state.tenders_keyword_hits)
    _m_pending = sum(1 for t in st.session_state.tenders_qualified if is_award_pending(t))
    _m_rate = f"{_m_hits/_m_qual*100:.1f}%" if _m_qual else "—"
    _m_pending_pct = f"{_m_pending/_m_qual*100:.0f}% 待確認" if _m_qual else None
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("📋 全部標案", f"{_m_total} 筆")
    c2.metric("🏆 精選", f"{_m_qual} 筆", delta=f"命中 {_m_hits} 筆" if _m_qual else None)
    c3.metric("🟠 待確認", f"{_m_pending} 筆", delta=_m_pending_pct, delta_color="inverse")
    c4.metric("🎯 關鍵字命中率", _m_rate)
    c5.metric("⭐ 追蹤中標案", f"{len(st.session_state.watchlist)} 筆")
    st.divider()

# ---------- Sidebar ----------
with st.sidebar:
    st.header("⚙️ 搜尋條件設定")
    st.text_input("標記關鍵字（空格分隔）", key="kw_input", help="僅用於標記命中與篩選，不影響抓取範圍")
    if st.button("↩️ 重設為預設關鍵字", width="stretch"):
        st.session_state.kw_input = _default_keywords_str()
        save_settings()
        st.rerun()

    st.divider()
    st.selectbox("日期模式", options=DATE_MODE_OPTIONS, key="date_mode", help="等標期內由站方忽略日期區間")
    is_range = selected_date_type() == core.DATE_TYPE_RANGE
    st.selectbox(
        "查詢天數",
        options=DEFAULT_DAYS_OPTIONS,
        key="days",
        disabled=not is_range,
        help="僅在「公告日期區間」模式下生效，未登入上限 186 天",
    )
    if not is_range:
        st.caption("ℹ️ 已選「等標期內」，查詢天數由站方忽略")

    st.selectbox("採購性質", options=ATTR_OPTIONS, key="attr")
    st.selectbox("決標方式", options=AWARD_OPTIONS, key="award")

    st.divider()
    st.checkbox("深度校驗決標方式", key="verify", help="開啟時以鏡像/官網校驗真實決標方式，關閉則皆為推估值")
    st.checkbox("包含未命中關鍵字", key="include_misses", help="精選預設只留命中關鍵字者，勾選後全部符合條件者皆顯示")
    st.checkbox("隱藏待確認", key="hide_pending", help="隱藏決標方式仍為推估值的列（🟠）")

    st.divider()

    now_ts = time.time()
    remaining_cooldown = max(0, int(RATE_LIMIT_SECONDS - (now_ts - st.session_state.last_search_ts)))
    search_disabled = st.session_state.is_running or st.session_state.is_trickling or remaining_cooldown > 0
    search_label = "🚀 開始搜尋標案"
    if st.session_state.is_running:
        search_label = "🔄 搜尋中…"
    elif remaining_cooldown > 0:
        search_label = f"⏳ 請 {remaining_cooldown} 秒後再搜尋"

    search_clicked = st.button(search_label, type="primary", width="stretch", disabled=search_disabled)
    if remaining_cooldown > 0 and not st.session_state.is_running:
        st.caption(f"為避免對政府站點造成負擔，搜尋間隔 {RATE_LIMIT_SECONDS} 秒")

    # 補齊按鈕（非阻塞）
    st.markdown("**背景補齊**")
    trickle_disabled = st.session_state.is_running or st.session_state.is_trickling
    trickle_label = "🔄 立即補齊 40 筆決標方式" if not st.session_state.is_trickling else "🔄 補齊中…"
    trickle_clicked = st.button(trickle_label, width="stretch", disabled=trickle_disabled, help="從待確認佇列取 40 筆，以鏡像為主校驗決標方式，結果寫回快取")
    if trickle_clicked and not trickle_disabled:
        tq = queue.Queue()
        tevt = threading.Event()
        st.session_state.trickle_queue = tq
        st.session_state.trickle_stop_event = tevt
        st.session_state.is_trickling = True
        t = threading.Thread(target=run_trickle_thread, args=(tq, tevt), daemon=False)
        t.start()
        st.session_state.trickle_thread = t
        append_log("🔄 已啟動補齊（背景執行，不阻塞頁面）…")
        st.rerun()

    st.divider()
    st.caption("輸出資料夾：`output/`")
    st.caption(f"快取：`{core.AWARD_CACHE_FILENAME}` · 追蹤：`{WATCHLIST_FILENAME}`")
    if st.button("💾 儲存目前搜尋條件", width="stretch"):
        save_settings()
        st.toast("已儲存搜尋條件", icon="✅")

# ---------- 進度與警告列 ----------
# 進度條與狀態已移入 fragment 內部建立，避免跨 fragment 上下文導致 Cloud 靜默崩潰（H3 修復）
notice_placeholder = st.empty()

# ---------- 非阻塞：搜尋結果輪詢 ----------
@st.fragment(run_every=0.8)
def _search_polling_fragment():
    # 內部建立進度元件，避免跨 fragment 上下文導致 Cloud 靜默崩潰（H3 修復）
    _progress = st.empty()
    _status = st.empty()
    if not st.session_state.is_running:
        return
    thread = ss_get("search_thread")
    q = ss_get("search_queue")
    if thread is None and q is None:
        st.session_state.is_running = False
        st.session_state.search_progress = 0
        return
    try:
        _progress.progress(int(ss_get("search_progress", 0)))
    except Exception:
        pass
    status = ss_get("search_status")
    try:
        if status:
            typ, msg = status if isinstance(status, tuple) else ("info", str(status))
            if typ == "info":
                _status.info(msg)
            elif typ == "success":
                _status.success(msg)
            elif typ == "warning":
                _status.warning(msg)
            elif typ == "error":
                _status.error(msg)
            else:
                _status.info(msg)
        else:
            _status.info("🔍 搜尋中… 請稍候，可按「停止搜尋」中斷")
    except Exception:
        pass

    has_done = False
    has_failed = False
    if q is not None:
        while not q.empty():
            try:
                action, payload = q.get_nowait()
                if action == "log":
                    try:
                        with LOG_LOCK:
                            st.session_state.log_lines.append(payload)
                            if len(st.session_state.log_lines) > MAX_LOG_LINES:
                                st.session_state.log_lines = st.session_state.log_lines[-LOG_KEEP:]
                    except Exception:
                        try:
                            st.session_state.log_lines.append(payload)
                        except Exception:
                            pass
                elif action == "progress":
                    st.session_state.search_progress = int(payload)
                elif action == "status":
                    st.session_state.search_status = payload
                elif action == "notice":
                    add_notice(payload)
                elif action == "done":
                    has_done = True
                    data = payload
                    st.session_state.tenders_all = data.get("tenders_all", [])
                    st.session_state.tenders_qualified = data.get("qualified", [])
                    st.session_state.tenders_keyword_hits = data.get("keyword_hits", [])
                    st.session_state.tenders_by_pk = data.get("by_pk", {})
                    st.session_state.last_search_summary = data.get("summary", "")
                    if data.get("was_stopped"):
                        append_log("⏹ 搜尋已停止，顯示中斷前已取得的部分結果。")
                elif action == "failed":
                    has_failed = True
                    st.session_state.search_failed = payload
            except queue.Empty:
                break
            except Exception:
                break
    thread = ss_get("search_thread")
    if thread is not None and not thread.is_alive():
        if has_failed or ss_get("search_failed"):
            append_log(f"❌ 搜尋失敗已結束: {ss_get('search_failed') or '未知錯誤'}")
        elif not has_done:
            # H2 修復：daemon 線程被容器回收或異常退出，未投遞 done，給出明確提示而非無聲跳掉
            append_log("❌ 搜尋線程異常結束，未收到完成訊號，可能為雲端網路超時或容器回收，請縮小查詢範圍或關閉深度校驗重試。")
            st.session_state.search_failed = "線程異常結束"
        st.session_state.is_running = False
        st.session_state.search_thread = None
        st.session_state.search_failed = None
        st.rerun()
    elif thread is None and q is not None and q.empty():
        st.session_state.is_running = False


if st.session_state.is_running:
    _search_polling_fragment()
    if st.button("⏹️ 停止搜尋", width="stretch", key="stop_search_btn"):
        evt = ss_get("stop_event")
        if evt:
            evt.set()
            append_log("⏹ 已要求停止，等待當前請求完成後收尾…")
        st.rerun()


@st.fragment(run_every=0.9)
def _trickle_polling_fragment():
    _t_status = st.empty()
    if not st.session_state.is_trickling:
        return
    tq = ss_get("trickle_queue")
    if tq is not None:
        while not tq.empty():
            try:
                action, payload = tq.get_nowait()
                if action == "log":
                    try:
                        with LOG_LOCK:
                            st.session_state.log_lines.append(payload)
                            if len(st.session_state.log_lines) > MAX_LOG_LINES:
                                st.session_state.log_lines = st.session_state.log_lines[-LOG_KEEP:]
                    except Exception:
                        try:
                            st.session_state.log_lines.append(payload)
                        except Exception:
                            pass
                elif action == "trickle_result":
                    result = payload
                    if result.get("ok"):
                        cache = core.load_award_cache(award_cache_path())
                        applied = core.apply_award_cache(st.session_state.tenders_all, cache)
                        if applied:
                            refresh_datasets()
                            append_log(f"🔄 補齊完成：本輪確認 {result['ok']} 筆，待確認尚餘 {result.get('remaining', 0)} 筆。")
                            add_notice(f"補齊確認 {result['ok']} 筆，待確認尚餘 {result.get('remaining',0)} 筆")
                        else:
                            append_log(f"🔄 補齊完成：本輪確認 {result['ok']} 筆（已寫入快取，下次搜尋生效）。")
                    else:
                        if result.get("picked") == 0:
                            append_log("ℹ️ 待確認佇列是空的——先跑一次搜尋產生佇列，或已全部確認。")
                        else:
                            append_log(f"ℹ️ 本輪無新增確認，待確認尚餘 {result.get('remaining',0)} 筆。")
                elif action == "trickle_failed":
                    append_log(f"  ⚠️ 補齊失敗: {payload}")
            except queue.Empty:
                break
            except Exception:
                break
    t = ss_get("trickle_thread")
    if t is not None and not t.is_alive():
        st.session_state.is_trickling = False
        st.session_state.trickle_thread = None
        st.rerun()
    elif t is None:
        st.session_state.is_trickling = False
    else:
        try:
            _t_status.info("🔄 補齊中…（約 1-2 分鐘，頁面可繼續操作）")
        except Exception:
            pass


if st.session_state.is_trickling:
    _trickle_polling_fragment()
    if st.button("⏹️ 停止補齊", width="stretch", key="stop_trickle_btn"):
        evt = ss_get("trickle_stop_event")
        if evt:
            evt.set()
            append_log("⏹ 已要求停止補齊…")
        st.rerun()


# ---------- 搜尋觸發 ----------
if search_clicked and not st.session_state.is_running and not st.session_state.is_trickling:
    now_ts = time.time()
    if now_ts - st.session_state.last_search_ts < RATE_LIMIT_SECONDS:
        st.warning(f"操作過於頻繁，請 {int(RATE_LIMIT_SECONDS - (now_ts - st.session_state.last_search_ts))} 秒後再試")
        st.stop()
    raw_kws = st.session_state.kw_input.strip()
    raw_kws, warn = validate_keywords(raw_kws)
    if warn:
        st.warning(warn)
        st.session_state.kw_input = raw_kws
    if not raw_kws:
        st.warning("請至少輸入一個關鍵字！")
        st.stop()
    keywords = [k.strip() for k in re.split(r"[\s,]+", raw_kws) if k.strip()]
    keywords = [w[:KEYWORDS_MAX_WORD_LEN] for w in keywords]
    days_val = st.session_state.days.split()[0]
    days = int(days_val) if days_val.isdigit() else 7
    target_attr = st.session_state.attr
    target_award = st.session_state.award
    date_type = selected_date_type()
    verify = bool(st.session_state.verify)
    include_misses = bool(st.session_state.include_misses)

    st.session_state.active_filter_label = describe_filter(target_attr, target_award)
    st.session_state.active_attr_target = target_attr
    st.session_state.active_award_target = target_award
    save_settings()
    clear_notices()
    append_log(f"── 開始搜尋（條件：{st.session_state.active_filter_label} / {st.session_state.date_mode}） ──")

    q = queue.Queue()
    stop_event = threading.Event()
    st.session_state.search_queue = q
    st.session_state.stop_event = stop_event
    st.session_state.search_progress = 0
    st.session_state.search_status = ("info", "🔍 搜尋中… 正在翻頁擷取標案")
    st.session_state.search_failed = None
    st.session_state.is_running = True
    st.session_state.last_search_ts = time.time()

    thread = threading.Thread(
        target=run_search_thread,
        args=(keywords, days, target_attr, target_award, date_type, verify, include_misses, q, stop_event),
        daemon=False,
    )
    thread.start()
    st.session_state.search_thread = thread
    st.rerun()

if st.session_state.notices:
    with notice_placeholder.container():
        for n in st.session_state.notices:
            st.warning(f"⚠️ {n}")

# ---------- Tabs 佈局 ----------
_qualified = st.session_state.tenders_qualified
_keyword_hits = st.session_state.tenders_keyword_hits
_include = st.session_state.include_misses
if _include:
    _matched_title = f"🏆 精選：{st.session_state.active_filter_label} ({len(_qualified)} 筆)"
else:
    misses = len(_qualified) - len(_keyword_hits)
    _matched_title = f"🏆 精選：{st.session_state.active_filter_label}∩關鍵字 ({len(_keyword_hits)} 筆，另 {misses} 筆未命中)"

_all_title = f"📋 所有標案 ({len(st.session_state.tenders_all)} 筆)"
_analytics_title = "📊 數據分析"
_watchlist_title = f"⭐ 追蹤清單 ({len(st.session_state.watchlist)} 筆)"
_log_title = "📝 執行紀錄"

tab_matched, tab_all, tab_analytics, tab_watchlist, tab_logs = st.tabs([
    _matched_title,
    _all_title,
    _analytics_title,
    _watchlist_title,
    _log_title,
])

# ==================== Tab 1: 🏆 精選 ====================
with tab_matched:
    if not st.session_state.tenders_all:
        render_empty_state("🔍", "尚未搜尋", '在左側設定 <b>關鍵字、日期模式、採購性質、決標方式</b> 後<br>按 <b>🚀 開始搜尋標案</b> 即可擷取最新公告<br>💡 首次建議：維持預設「勞務 + 最低標 + 等標期內」直接搜尋，約 1-3 分鐘完成')
    else:
        base_rows = _qualified if st.session_state.include_misses else _keyword_hits

        filt = render_filter_panel("matched", base_rows)
        filtered = build_advanced_display_rows(
            base_rows,
            hide_pending_filter=st.session_state.hide_pending,
            query=filt["query"],
            min_budget_wan=filt["min_budget_wan"],
            max_budget_wan=filt["max_budget_wan"],
            urgency=filt["urgency"],
            award_status=filt["award_status"],
            selected_agencies=filt.get("agencies", []),
        )
        # 數值排序（解決剩餘天數文字排序不準）
        filtered_sorted = sort_tenders(filtered, filt.get("sort", SORT_OPTIONS[0]))

        if not filtered_sorted:
            st.warning("無符合篩選條件的精選標案。請調整搜尋關鍵字或放寬進階篩選條件。")
        else:
            display_rows, pag_cap = paginate_rows(filtered_sorted, "matched", 100)
            caption_base = f"顯示 {len(display_rows)} / {len(filtered_sorted)} 筆（已篩選） / 原始符合條件 {len(_qualified)} 筆"
            if pag_cap:
                caption_base += f" · {pag_cap}"
            render_tender_table(display_rows, "matched", caption_base + " · 點擊欄頭可再排序 · 點擊「詳細連結」開啟官方頁面 · 🟠=推估待確認，⚪=已確認但不符", selectable=True)

        st.divider()
        st.markdown("**匯出**")
        export_rows = filtered_sorted if filtered_sorted else base_rows
        if export_rows:
            excel_fp = f"{rows_fingerprint(st.session_state.tenders_all)}_{rows_fingerprint(export_rows)}"
            excel_bytes = cached_excel_bytes(excel_fp, st.session_state.tenders_all, export_rows)
            csv_fp = rows_fingerprint(export_rows)
            csv_bytes = cached_csv_bytes(csv_fp, export_rows)
            suffix = re.sub(r"[^\w一-鿿]+", "_", f"{st.session_state.active_attr_target}{st.session_state.active_award_target}").strip("_")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            col_dl1, col_dl2 = st.columns(2)
            with col_dl1:
                st.download_button(
                    "📊 下載 Excel（精選 + 全部）",
                    data=excel_bytes,
                    file_name=f"pcc_tenders_{timestamp}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width="stretch",
                )
            with col_dl2:
                st.download_button(
                    "📄 下載 CSV（精選）",
                    data=csv_bytes,
                    file_name=f"pcc_tenders_{timestamp}_{suffix}.csv",
                    mime="text/csv",
                    width="stretch",
                )
            st.caption("💡 同時已自動備份一份 Excel 至 output/ 資料夾（僅保留最新 20 份）")
        else:
            st.caption("無符合條件的精選可匯出（可勾選「包含未命中關鍵字」或放寬決標方式）")


# ==================== Tab 2: 📋 所有標案 ====================
with tab_all:
    if not st.session_state.tenders_all:
        render_empty_state("📋", "尚無資料", "請先在精選分頁完成搜尋")
    else:
        filt_all = render_filter_panel("all", st.session_state.tenders_all)
        filtered_all = build_advanced_display_rows(
            st.session_state.tenders_all,
            hide_pending_filter=False,
            query=filt_all["query"],
            min_budget_wan=filt_all["min_budget_wan"],
            max_budget_wan=filt_all["max_budget_wan"],
            urgency=filt_all["urgency"],
            award_status=filt_all["award_status"],
            selected_agencies=filt_all.get("agencies", []),
        )
        filtered_all_sorted = sort_tenders(filtered_all, filt_all.get("sort", SORT_OPTIONS[0]))

        if not filtered_all_sorted:
            st.warning("無符合篩選條件的標案。")
        else:
            display_rows_all, pag_cap_a = paginate_rows(filtered_all_sorted, "all", 100)
            caption_all = f"顯示 {len(display_rows_all)} / {len(filtered_all_sorted)} 筆（已篩選） / 共 {len(st.session_state.tenders_all)} 筆"
            if pag_cap_a:
                caption_all += f" · {pag_cap_a}"
            render_tender_table(display_rows_all, "all", caption_all + " · 點擊欄頭可再排序 · 點擊「詳細連結」開啟官方頁面", selectable=True)

        st.divider()
        if st.session_state.tenders_all:
            csv_all_bytes = cached_csv_bytes(rows_fingerprint(st.session_state.tenders_all), st.session_state.tenders_all)
            st.download_button(
                "📄 下載 CSV（全部標案）",
                data=csv_all_bytes,
                file_name=f"pcc_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                width="stretch",
            )


# ==================== Tab 3: 📊 數據分析 ====================
with tab_analytics:
    render_analytics_dashboard(
        st.session_state.tenders_all,
        st.session_state.tenders_qualified,
        st.session_state.tenders_keyword_hits,
    )


# ==================== Tab 4: ⭐ 追蹤清單 ====================
with tab_watchlist:
    # 委派至 watchlist_utils.resolve_watchlist_rows（保持舊 _resolve 邏輯，僅搬家）
    try:
        _wl_cache = core.load_award_cache(award_cache_path())
    except Exception:
        _wl_cache = {}
    watchlist_items = wl_utils.resolve_watchlist_rows(
        st.session_state.watchlist, st.session_state.tenders_by_pk, _wl_cache
    )
    if not watchlist_items:
        render_empty_state("⭐", "目前追蹤清單尚無標案", '在「🏆 精選」或「📋 所有標案」勾選表格列並按「加入追蹤」即可將關注的標案加入此清單。<br>追蹤資料會自動保存於 <code>output/watchlist.json</code>，下次開啟程式自動保留。')
    else:
        # 排序控制器（ watchlist 亦支援數值排序，解決剩餘天數文字排序問題）
        col_sort_wl, col_info_wl = st.columns([2, 3])
        with col_sort_wl:
            sort_wl = st.selectbox("↕️ 排序方式", options=SORT_OPTIONS, key="sort_watchlist", help="追蹤清單亦支援數值排序")
        with col_info_wl:
            st.write("")
            st.caption(f"已選「{sort_wl}」· 追蹤清單支援與主表格一致的排序")
        watchlist_sorted = sort_tenders(watchlist_items, sort_wl)
        # 統計已截標（用 get_days_remaining，與篩選器一致）
        expired_count = sum(1 for t in watchlist_sorted if (get_days_remaining(t.get("截止投標", "")) is not None and get_days_remaining(t.get("截止投標", "")) < 0))
        caption_wl = f"📌 共收藏 {len(watchlist_sorted)} 筆標案 · 資料永久保存於 `output/watchlist.json`"
        if expired_count > 0:
            caption_wl += f" · 其中 {expired_count} 筆已截標"
        # 若有快照來源，提醒使用者部分為快照
        snapshot_count = sum(1 for t in watchlist_sorted if t.get("_watchlist_source") == "快照")
        if snapshot_count > 0:
            caption_wl += f" · {snapshot_count} 筆為快照（查無最新資料）"
        # 大追蹤清單分頁（>50 筆時；主表用 paginate_rows 預設 100，追蹤表較矮 cap=500 故用 50 以維持 2 頁內可視性）
        WL_LIMIT = 50
        if len(watchlist_sorted) > WL_LIMIT:
            page_wl = st.session_state.get("page_watchlist", 1)
            page_total_wl = (len(watchlist_sorted) + WL_LIMIT - 1) // WL_LIMIT
            if page_wl > page_total_wl:
                page_wl = 1
                st.session_state["page_watchlist"] = 1
            c_prev_wl, c_info_wl2, c_next_wl = st.columns([1, 3, 1])
            with c_prev_wl:
                if st.button("◀ 上一頁", key="prev_wl", disabled=(page_wl <= 1)):
                    st.session_state["page_watchlist"] = max(1, page_wl - 1)
                    st.rerun()
            with c_info_wl2:
                st.caption(f"第 {page_wl} / {page_total_wl} 頁 · 共 {len(watchlist_sorted)} 筆")
            with c_next_wl:
                if st.button("下一頁 ▶", key="next_wl", disabled=(page_wl >= page_total_wl)):
                    st.session_state["page_watchlist"] = min(page_total_wl, page_wl + 1)
                    st.rerun()
            start_wl = (page_wl - 1) * WL_LIMIT
            display_wl = watchlist_sorted[start_wl:start_wl + WL_LIMIT]
            caption_wl += f" · 第 {page_wl}/{page_total_wl} 頁"
        else:
            display_wl = watchlist_sorted
            if "page_watchlist" in st.session_state:
                st.session_state["page_watchlist"] = 1
        render_tender_table(display_wl, "watchlist", caption_wl, max_rows=15, cap=500)

        col_wl_act1, col_wl_act2, col_wl_act3 = st.columns([2, 1, 1])
        with col_wl_act1:
            to_remove = st.multiselect(
                "選取要移出追蹤的標案：",
                options=list(st.session_state.watchlist.keys()),
                format_func=lambda k: tender_label(st.session_state.watchlist[k].get("snapshot", st.session_state.watchlist[k]) if isinstance(st.session_state.watchlist[k], dict) and "snapshot" in st.session_state.watchlist[k] else st.session_state.watchlist[k]),
                key="wl_remove_sel",
            )
            if st.button("🗑️ 移出選取標案", key="btn_remove_wl_items"):
                if to_remove:
                    wl = st.session_state.watchlist
                    for k in to_remove:
                        if k in wl:
                            del wl[k]
                    st.session_state.watchlist = wl
                    save_watchlist(wl)
                    st.toast("已從追蹤清單移除！", icon="🗑️")
                    st.rerun()

        with col_wl_act2:
            st.write("")
            if st.button("🧹 清空所有追蹤", width="stretch", key="btn_clear_wl"):
                st.session_state.watchlist = {}
                save_watchlist({})
                st.toast("已清空追蹤清單", icon="🧹")
                st.rerun()

        with col_wl_act3:
            st.write("")
            # 匯出完整排序後的清單（非僅當頁）
            csv_wl_bytes = cached_csv_bytes(rows_fingerprint(watchlist_sorted), watchlist_sorted)
            st.download_button(
                "📄 匯出追蹤清單 CSV",
                data=csv_wl_bytes,
                file_name=f"pcc_watchlist_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                width="stretch",
            )


# ==================== Tab 5: 📝 執行紀錄 ====================
with tab_logs:
    if st.session_state.log_lines:
        display_logs = st.session_state.log_lines[-300:]
        st.code("\n".join(display_logs), language="text")
    else:
        st.code("✅ 應用程式已就緒。請在左側設定條件後開始搜尋。\nℹ️ 搜尋會掃描該條件下的全部標案，關鍵字僅用於標記與快速篩選。", language="text")

    col_log1, col_log2 = st.columns([1, 5])
    with col_log1:
        if st.button("🧹 清空紀錄", width="stretch"):
            st.session_state.log_lines = []
            st.rerun()
    with col_log2:
        if st.session_state.log_lines:
            log_text = "\n".join(st.session_state.log_lines)
            st.download_button(
                "⬇️ 下載紀錄 .txt",
                data=log_text.encode("utf-8"),
                file_name=f"pcc_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                mime="text/plain",
                width="stretch",
            )

# ---------- Footer ----------
st.divider()
st.caption(
    "政府採購網標案爬蟲 · 本地 Streamlit 版 · 核心邏輯與 CLI 共用 pcc_core · "
    "決標方式以公開資料鏡像為主、官網詳細頁備援 · "
    "快取、追蹤與佇列位於 output/ · 啟動：`streamlit run app.py`"
)

if len(st.session_state.log_lines) == 0:
    append_log("✅ 應用程式初始化完成。請在左側設定條件後按「開始搜尋標案」。")
    append_log("ℹ️ 搜尋會掃描該條件下的全部標案，關鍵字僅用於標記與快速篩選。")
    if st.session_state._settings_restored and core.load_json_dict(settings_path()):
        append_log(f"⚙️ 已還原上次的搜尋條件（{core.SETTINGS_FILENAME}）。")
