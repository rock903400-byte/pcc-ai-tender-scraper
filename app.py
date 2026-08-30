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

PENDING_PREFIX = "🟠 "
DISQUALIFIED_PREFIX = "⚪ "

OUTPUT_DIR = os.path.abspath("output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

WATCHLIST_FILENAME = "watchlist.json"

DISPLAY_COLUMNS = [
    "#",
    "公告日期",
    "招標機關",
    "標案名稱",
    "預算金額",
    "決標方式",
    "招標方式",
    "決標方式來源",
    "截止投標",
    "命中關鍵字",
    "詳細連結",
]

DISPLAY_COLUMNS_VISIBLE = DISPLAY_COLUMNS

MAX_LOG_LINES = 1000
LOG_KEEP = 500
MAX_OUTPUT_KEEP = 20
RATE_LIMIT_SECONDS = 60
KEYWORDS_MAX_CHARS = 500
KEYWORDS_MAX_WORDS = 100
KEYWORDS_MAX_WORD_LEN = 30


# ==================== 檔案路徑與狀態 ====================

def award_cache_path() -> str:
    return core.award_cache_path(OUTPUT_DIR)


def pending_queue_path() -> str:
    return core.pending_queue_path(OUTPUT_DIR)


def settings_path() -> str:
    return core.settings_path(OUTPUT_DIR)


def watchlist_path() -> str:
    return os.path.join(OUTPUT_DIR, WATCHLIST_FILENAME)


def load_watchlist() -> dict:
    """載入追蹤清單，回傳以 pk 或 標案案號 為 key 的 dict。"""
    try:
        if os.path.exists(watchlist_path()):
            with open(watchlist_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_watchlist(wl: dict) -> bool:
    """儲存追蹤清單。"""
    try:
        with open(watchlist_path(), "w", encoding="utf-8") as f:
            json.dump(wl, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


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
        # 進階過濾狀態
        "budget_range_matched": (0, 2000),
        "urgency_matched": "全部",
        "award_status_matched": "全部",
        "budget_range_all": (0, 2000),
        "urgency_all": "全部",
        "award_status_all": "全部",
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
        "trickle_progress": 0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def describe_filter(target_attr: str, target_award: str) -> str:
    parts = [p for p in (target_attr, target_award) if p and p != "不限"]
    return "".join(parts) if parts else "全部條件"


def is_award_pending(tender: dict) -> bool:
    return (not core.is_award_confirmed(tender)
            and "待確認" in tender.get("決標方式", ""))


def parse_deadline_date(deadline_str: str) -> date:
    """解析截止投標字串為西元 date 物件，失敗回傳 None。"""
    if not deadline_str or not isinstance(deadline_str, str):
        return None
    try:
        clean = deadline_str.strip().split()[0].replace("-", "/")
        parts = clean.split("/")
        if len(parts) == 3:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            if y < 1900:
                y += 1911
            return date(y, m, d)
    except Exception:
        pass
    return None


def get_days_remaining(deadline_str: str) -> int:
    """計算距離截止投標日還剩幾天。過期為負數，無效回傳 None。"""
    d = parse_deadline_date(deadline_str)
    if d is None:
        return None
    return (d - date.today()).days


def append_log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    st.session_state.log_lines.append(f"[{timestamp}] {message}")
    if len(st.session_state.log_lines) > MAX_LOG_LINES:
        st.session_state.log_lines = st.session_state.log_lines[-LOG_KEEP:]


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

def sanitize_excel_value(val):
    """Excel 公式注入防護：= + - @ 開頭前綴單引號。"""
    if not isinstance(val, str):
        return val
    stripped = val.lstrip()
    if stripped.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + val
    return val


def sanitize_rows(rows: list) -> list:
    """對匯出前的每列做公式注入清洗（不改動原始資料）。"""
    sanitized = []
    for row in rows:
        new_row = {}
        for k, v in row.items():
            new_row[k] = sanitize_excel_value(v) if isinstance(v, str) else v
        sanitized.append(new_row)
    return sanitized


def prune_output_excels(keep: int = MAX_OUTPUT_KEEP):
    """輪替刪除 output 下舊的 pcc_tenders_*.xlsx，保留最新 keep 份。"""
    try:
        pattern = os.path.join(OUTPUT_DIR, "pcc_tenders_*.xlsx")
        files = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p))
        if len(files) > keep:
            for old in files[:-keep]:
                try:
                    os.remove(old)
                except OSError:
                    pass
    except Exception:
        pass


def validate_keywords(raw: str) -> tuple:
    """檢查關鍵字長度與數量，超過則截斷並回 (cleaned, warning)。"""
    if len(raw) > KEYWORDS_MAX_CHARS:
        raw = raw[:KEYWORDS_MAX_CHARS]
        return raw, f"關鍵字過長，已截斷至 {KEYWORDS_MAX_CHARS} 字元"
    words = [w for w in re.split(r"[\s,]+", raw.strip()) if w]
    if len(words) > KEYWORDS_MAX_WORDS:
        words = words[:KEYWORDS_MAX_WORDS]
        raw = " ".join(words)
        return raw, f"關鍵字過多，已截斷至 {KEYWORDS_MAX_WORDS} 組"
    for w in words:
        if len(w) > KEYWORDS_MAX_WORD_LEN:
            return raw, f"關鍵字「{w[:10]}…」過長（>{KEYWORDS_MAX_WORD_LEN}），可能影響效能"
    return raw, None


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


def build_advanced_display_rows(
    tenders: list,
    hide_pending_filter: bool = False,
    query: str = "",
    min_budget_wan: int = 0,
    max_budget_wan: int = 0,
    urgency: str = "全部",
    award_status: str = "全部",
    selected_agencies: list = None,
) -> list:
    """依據文字快篩、預算區間、截標急迫度、決標狀態與機關進行多維度過濾。"""
    query = query.strip().lower()
    rows = []
    for t in tenders:
        if hide_pending_filter and is_award_pending(t):
            continue

        # 決標狀態過濾
        if award_status == "✅ 僅已確認 (鏡像/官方)":
            if not core.is_award_confirmed(t):
                continue
        elif award_status == "🟠 僅待確認 (推估)":
            if not is_award_pending(t):
                continue

        # 預算金額過濾（單位：萬元）
        budget_val = core.parse_amount(t.get("預算金額", ""))
        if budget_val != -1.0:
            budget_wan = budget_val / 10000.0
            if min_budget_wan > 0 and budget_wan < min_budget_wan:
                continue
            if max_budget_wan > 0 and budget_wan > max_budget_wan:
                continue
        else:
            if min_budget_wan > 0:
                continue

        # 截標急迫度過濾
        if urgency and urgency != "全部":
            days = get_days_remaining(t.get("截止投標", ""))
            if urgency.startswith("🔥 3天內"):
                if days is None or days < 0 or days > 3:
                    continue
            elif urgency.startswith("⏳ 7天內"):
                if days is None or days < 0 or days > 7:
                    continue
            elif urgency.startswith("📅 充足"):
                if days is None or days < 8 or days > 14:
                    continue
            elif urgency.startswith("🗓️ 充裕"):
                if days is None or days <= 14:
                    continue
            elif urgency.startswith("⌛ 排除已截標"):
                if days is not None and days < 0:
                    continue

        # 機關過濾
        if selected_agencies:
            agency = t.get("招標機關", "")
            if not any(sel in agency for sel in selected_agencies):
                continue

        # 文字搜尋
        if query:
            haystack = " ".join([
                t.get("招標機關", ""), t.get("標案名稱", ""),
                t.get("標案案號", ""), t.get("命中關鍵字", ""),
            ]).lower()
            if query not in haystack:
                continue
        rows.append(t)
    return rows


def tenders_to_dataframe(tenders: list) -> pd.DataFrame:
    if not tenders:
        return pd.DataFrame(columns=DISPLAY_COLUMNS)
    data = []
    for idx, t in enumerate(tenders, 1):
        award_display = t.get("決標方式", "")
        source = t.get("決標方式來源", "")
        if is_award_pending(t):
            award_display = f"{PENDING_PREFIX}{award_display}"
        elif core.is_award_confirmed(t):
            matched = core.filter_tenders([t], "不限", st.session_state.active_award_target)
            if not matched:
                award_display = f"{DISQUALIFIED_PREFIX}{award_display}"
        budget_text = t.get("預算金額", "")
        budget_val = core.parse_amount(budget_text)
        budget_num = int(budget_val) if budget_val != -1.0 else 0
        data.append({
            "#": idx,
            "公告日期": t.get("公告日期", ""),
            "招標機關": t.get("招標機關", ""),
            "標案名稱": t.get("標案名稱", ""),
            "預算金額": budget_num,
            "決標方式": award_display,
            "招標方式": t.get("招標方式", ""),
            "決標方式來源": source,
            "截止投標": t.get("截止投標", ""),
            "命中關鍵字": t.get("命中關鍵字", "") or "—",
            "詳細連結": t.get("詳細連結", ""),
            "標案案號": t.get("標案案號", ""),
            "pk": t.get("pk", ""),
        })
    df = pd.DataFrame(data)
    return df


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


# ==================== 匯出 ====================

def build_excel_bytes(all_tenders: list, matched_tenders: list) -> bytes:
    all_s = sanitize_rows(all_tenders) if all_tenders else []
    matched_s = sanitize_rows(matched_tenders) if matched_tenders else []
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        if matched_s:
            df_matched = pd.DataFrame(matched_s)
            cols = [c for c in core.report_columns(matched_s) if c in df_matched.columns]
            if cols:
                df_matched = df_matched[cols]
            df_matched.to_excel(writer, sheet_name="精選_勞務最低標", index=False)
        else:
            pd.DataFrame([{"說明": "本次搜尋無完全符合篩選條件之標案"}]).to_excel(
                writer, sheet_name="精選_勞務最低標", index=False
            )
        if all_s:
            df_all = pd.DataFrame(all_s)
            cols = [c for c in core.report_columns(all_s) if c in df_all.columns]
            if cols:
                df_all = df_all[cols]
            df_all.to_excel(writer, sheet_name="所有搜尋標案", index=False)
    return output.getvalue()


def build_csv_bytes(rows: list) -> bytes:
    if not rows:
        return b""
    rows_s = sanitize_rows(rows)
    keys = core.report_columns(rows_s)
    output = io.StringIO()
    import csv as csvmod
    writer = csvmod.DictWriter(output, fieldnames=keys, extrasaction="ignore", restval="")
    writer.writeheader()
    for row in rows_s:
        writer.writerow({k: row.get(k, "") for k in keys})
    return output.getvalue().encode("utf-8-sig")


def rows_fingerprint(rows: list) -> str:
    """以 pk（或標案案號）序列產生輕量指紋，用於 cache key。"""
    import hashlib

    h = hashlib.md5()
    h.update(str(len(rows)).encode())
    for r in rows:
        h.update((r.get("pk") or r.get("標案案號") or "").encode("utf-8"))
        h.update(b"|")
        h.update((r.get("決標方式") or "").encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def tender_key(t: dict) -> str:
    """取得標案的穩定識別鍵，優先使用 pk，退回標案案號。"""
    return t.get("pk") or t.get("標案案號") or ""


def tender_label(t: dict) -> str:
    """產生標案在下拉選單中的顯示文字。"""
    return f"{t.get('招標機關','')} - {t.get('標案名稱','')} ({t.get('標案案號','')})"


@st.cache_data(show_spinner=False)
def cached_excel_bytes(fingerprint: str, _all_tenders: list, _matched_tenders: list) -> bytes:
    return build_excel_bytes(_all_tenders, _matched_tenders)


@st.cache_data(show_spinner=False)
def cached_csv_bytes(fingerprint: str, _rows: list) -> bytes:
    return build_csv_bytes(_rows)


def auto_export_backup(all_tenders: list, matched_tenders: list):
    if not all_tenders:
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"pcc_tenders_{timestamp}.xlsx")
    try:
        all_s = sanitize_rows(all_tenders)
        matched_s = sanitize_rows(matched_tenders)
        core.write_excel_report(path, all_s, matched_s)
        prune_output_excels()
        return os.path.join("output", os.path.basename(path))
    except Exception as e:
        return f"失敗: {e}"


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

            def _on_progress(done, total):
                if done % max(1, total // 50) == 0 or done == total:
                    share = 100 - SEARCH_PROGRESS_SHARE
                    qprogress(SEARCH_PROGRESS_SHARE + int(done / total * share))

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
        st.markdown(
            """
            <div class="empty-illustration">
                <div style="font-size: 42px;">📊</div>
                <div style="font-size: 16px; font-weight: 600; margin: 8px 0; color: #E2E8F0;">尚未載入分析數據</div>
                <div style="font-size: 13px; color: #94A3B8;">請先在左側設定條件並完成一次搜尋，即可即時查看標案市場大盤圖表</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    # 計算統計指標
    amounts = [core.parse_amount(t.get("預算金額", "")) for t in tenders]
    valid_amounts = [a for a in amounts if a > 0]
    total_budget = sum(valid_amounts)
    avg_budget = total_budget / len(valid_amounts) if valid_amounts else 0
    max_tender = max(tenders, key=lambda t: core.parse_amount(t.get("預算金額", ""))) if tenders else None
    max_amount = core.parse_amount(max_tender.get("預算金額", "")) if max_tender else 0

    confirmed_count = sum(1 for t in tenders if core.is_award_confirmed(t))
    confirmed_ratio = confirmed_count / len(tenders) * 100 if tenders else 0

    # 頂部 KPI 卡片
    k1, k2, k3, k4 = st.columns(4)
    budget_yi = total_budget / 100000000.0
    k1.metric("💰 總預算規模", f"{budget_yi:.2f} 億元" if budget_yi >= 1 else f"{total_budget/10000:.0f} 萬元")
    k2.metric("📈 平均標案金額", f"{avg_budget/10000:.1f} 萬元")
    k3.metric("🏆 最高單一預算", f"{max_amount/10000:.0f} 萬元", help=max_tender.get("標案名稱", "") if max_tender else "")
    k4.metric("🛡️ 官方/鏡像確認率", f"{confirmed_ratio:.1f}%", delta=f"{confirmed_count}/{len(tenders)} 筆")

    st.write("")

    # 第一列圖表：預算級距分佈 & 決標方式佔比
    col_c1, col_c2 = st.columns([3, 2])
    with col_c1:
        st.subheader("📊 標案預算級距分佈")
        tier_order = [
            "< 100萬 (公告金額以下)",
            "100萬 ~ 500萬",
            "500萬 ~ 1,000萬",
            "1,000萬 ~ 5,000萬 (查核金額)",
            "5,000萬 ~ 2億",
            "≥ 2億 (巨額採購)",
            "未公開 / 0 元",
        ]
        tier_counts = {t: 0 for t in tier_order}
        for a in amounts:
            if a <= 0:
                tier_counts["未公開 / 0 元"] += 1
            elif a < 1000000:
                tier_counts["< 100萬 (公告金額以下)"] += 1
            elif a < 5000000:
                tier_counts["100萬 ~ 500萬"] += 1
            elif a < 10000000:
                tier_counts["500萬 ~ 1,000萬"] += 1
            elif a < 50000000:
                tier_counts["1,000萬 ~ 5,000萬 (查核金額)"] += 1
            elif a < 200000000:
                tier_counts["5,000萬 ~ 2億"] += 1
            else:
                tier_counts["≥ 2億 (巨額採購)"] += 1

        tier_df = pd.DataFrame({
            "級距": list(tier_counts.keys()),
            "標案筆數": list(tier_counts.values()),
        })
        chart_tier = alt.Chart(tier_df).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6).encode(
            x=alt.X("級距:N", sort=tier_order, title="預算級距", axis=alt.Axis(labelAngle=-25)),
            y=alt.Y("標案筆數:Q", title="標案筆數"),
            color=alt.Color("標案筆數:Q", scale=alt.Scale(scheme="blues"), legend=None),
            tooltip=["級距", "標案筆數"],
        ).properties(height=300)
        st.altair_chart(chart_tier, use_container_width=True)

    with col_c2:
        st.subheader("⚖️ 決標方式構成佔比")
        award_summary = {}
        for t in tenders:
            raw_award = t.get("決標方式", "未提供")
            if is_award_pending(t):
                key = "推估待確認 (🟠)"
            elif "最低標" in raw_award:
                key = "最低標"
            elif "最有利標" in raw_award or "評選" in raw_award:
                key = "最有利標/評選"
            else:
                key = "其他方式"
            award_summary[key] = award_summary.get(key, 0) + 1

        award_df = pd.DataFrame({
            "決標方式類別": list(award_summary.keys()),
            "筆數": list(award_summary.values()),
        })
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
        agency_stats = {}
        for t in tenders:
            agency = t.get("招標機關", "未知機關")
            budget_val = core.parse_amount(t.get("預算金額", ""))
            amt = budget_val if budget_val > 0 else 0
            if agency not in agency_stats:
                agency_stats[agency] = {"count": 0, "total_amt": 0}
            agency_stats[agency]["count"] += 1
            agency_stats[agency]["total_amt"] += amt

        sorted_agencies = sorted(agency_stats.items(), key=lambda x: x[1]["count"], reverse=True)[:10]
        if sorted_agencies:
            agency_df = pd.DataFrame([
                {
                    "招標機關": a[0],
                    "標案筆數": a[1]["count"],
                    "累積預算(萬元)": round(a[1]["total_amt"] / 10000.0, 1),
                }
                for a in sorted_agencies
            ])
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
        kw_counts = {}
        for t in tenders:
            hit_kws = t.get("命中關鍵字", "")
            if hit_kws and hit_kws != "—":
                for kw in hit_kws.split("、"):
                    kw = kw.strip()
                    if kw:
                        kw_counts[kw] = kw_counts.get(kw, 0) + 1

        if kw_counts:
            sorted_kws = sorted(kw_counts.items(), key=lambda x: x[1], reverse=True)[:12]
            kw_df = pd.DataFrame([{"關鍵字": k[0], "命中次數": k[1]} for k in sorted_kws])
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
    urgency_bins = {
        "🔥 3天內即將截標": 0,
        "⏳ 4~7天內截標": 0,
        "📅 8~14天內截標": 0,
        "🗓️ 14天以上": 0,
        "⌛ 已截標 / 截止日期未定": 0,
    }
    for t in tenders:
        days = get_days_remaining(t.get("截止投標", ""))
        if days is None:
            urgency_bins["⌛ 已截標 / 截止日期未定"] += 1
        elif days < 0:
            urgency_bins["⌛ 已截標 / 截止日期未定"] += 1
        elif days <= 3:
            urgency_bins["🔥 3天內即將截標"] += 1
        elif days <= 7:
            urgency_bins["⏳ 4~7天內截標"] += 1
        elif days <= 14:
            urgency_bins["📅 8~14天內截標"] += 1
        else:
            urgency_bins["🗓️ 14天以上"] += 1

    urgency_df = pd.DataFrame({
        "急迫度分類": list(urgency_bins.keys()),
        "標案數量": list(urgency_bins.values()),
    })
    chart_urgency = alt.Chart(urgency_df).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6).encode(
        x=alt.X("急迫度分類:N", sort=list(urgency_bins.keys()), title="急迫度分類"),
        y=alt.Y("標案數量:Q", title="標案數量"),
        color=alt.Color("急迫度分類:N", scale=alt.Scale(
            domain=list(urgency_bins.keys()),
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

init_session_state()

if not st.session_state._settings_restored:
    restored = restore_settings()
    st.session_state._settings_restored = True
    if restored:
        st.session_state.active_attr_target = st.session_state.attr
        st.session_state.active_award_target = st.session_state.award
        st.session_state.active_filter_label = describe_filter(st.session_state.attr, st.session_state.award)

# ---------- Header & CSS ----------
st.markdown(
    """
    <style>
    .block-container {padding-top: 1.2rem; padding-bottom: 1rem;}
    /* 卡片化 metric 與漸層效果 */
    [data-testid="stMetric"] {
        background: linear-gradient(145deg, #1E293B, #0F172A);
        border: 1px solid #334155;
        border-radius: 12px;
        padding: 14px 16px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.35);
        transition: transform 0.2s ease, border-color 0.2s ease;
    }
    [data-testid="stMetric"]:hover {
        border-color: #38BDF8;
        transform: translateY(-2px);
    }
    [data-testid="stMetricLabel"] {font-size: 0.85rem; color: #94A3B8; font-weight: 500;}
    [data-testid="stMetricValue"] {font-weight: 700; color: #F8FAFC;}
    [data-testid="stMetricDelta"] {font-size: 0.8rem;}
    /* 空狀態 - 深底精緻風格 */
    .empty-illustration {
        text-align: center;
        padding: 40px 24px;
        background: #1E293B;
        border: 1px dashed #475569;
        border-radius: 14px;
        margin: 18px 0;
    }
    .empty-illustration div {color: #CBD5E1 !important;}
    /* 表格與按鈕微調 */
    [data-testid="stDataFrame"] {border-radius: 10px; overflow: hidden; border: 1px solid #334155;}
    .stButton>button {border-radius: 8px; font-weight: 500;}
    .badge-pill {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 600;
        margin-right: 6px;
    }
    .badge-blue {background: #0284C7; color: #FFFFFF;}
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
        t = threading.Thread(target=run_trickle_thread, args=(tq, tevt), daemon=True)
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
progress_placeholder = st.empty()
status_placeholder = st.empty()
notice_placeholder = st.empty()

# ---------- 非阻塞：搜尋結果輪詢 ----------
@st.fragment(run_every=0.8)
def _search_polling_fragment():
    if not st.session_state.is_running:
        return
    thread = ss_get("search_thread")
    q = ss_get("search_queue")
    if thread is None and q is None:
        st.session_state.is_running = False
        st.session_state.search_progress = 0
        return
    progress_placeholder.progress(int(ss_get("search_progress", 0)))
    status = ss_get("search_status")
    if status:
        typ, msg = status if isinstance(status, tuple) else ("info", str(status))
        if typ == "info":
            status_placeholder.info(msg)
        elif typ == "success":
            status_placeholder.success(msg)
        elif typ == "warning":
            status_placeholder.warning(msg)
        elif typ == "error":
            status_placeholder.error(msg)
        else:
            status_placeholder.info(msg)
    else:
        status_placeholder.info("🔍 搜尋中… 請稍候，可按「停止搜尋」中斷")

    if q is not None:
        while not q.empty():
            try:
                action, payload = q.get_nowait()
                if action == "log":
                    st.session_state.log_lines.append(payload)
                    if len(st.session_state.log_lines) > MAX_LOG_LINES:
                        st.session_state.log_lines = st.session_state.log_lines[-LOG_KEEP:]
                elif action == "progress":
                    st.session_state.search_progress = int(payload)
                elif action == "status":
                    st.session_state.search_status = payload
                elif action == "notice":
                    add_notice(payload)
                elif action == "done":
                    data = payload
                    st.session_state.tenders_all = data.get("tenders_all", [])
                    st.session_state.tenders_qualified = data.get("qualified", [])
                    st.session_state.tenders_keyword_hits = data.get("keyword_hits", [])
                    st.session_state.tenders_by_pk = data.get("by_pk", {})
                    st.session_state.last_search_summary = data.get("summary", "")
                    if data.get("was_stopped"):
                        append_log("⏹ 搜尋已停止，顯示中斷前已取得的部分結果。")
                elif action == "failed":
                    st.session_state.search_failed = payload
            except queue.Empty:
                break
    thread = ss_get("search_thread")
    if thread is not None and not thread.is_alive():
        if ss_get("search_failed"):
            append_log(f"❌ 搜尋失敗已結束: {st.session_state.search_failed}")
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
    if not st.session_state.is_trickling:
        return
    tq = ss_get("trickle_queue")
    if tq is not None:
        while not tq.empty():
            try:
                action, payload = tq.get_nowait()
                if action == "log":
                    st.session_state.log_lines.append(payload)
                    if len(st.session_state.log_lines) > MAX_LOG_LINES:
                        st.session_state.log_lines = st.session_state.log_lines[-LOG_KEEP:]
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
    t = ss_get("trickle_thread")
    if t is not None and not t.is_alive():
        st.session_state.is_trickling = False
        st.session_state.trickle_thread = None
        st.rerun()
    elif t is None:
        st.session_state.is_trickling = False
    else:
        status_placeholder.info("🔄 補齊中…（約 1-2 分鐘，頁面可繼續操作）")


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
        daemon=True,
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
        st.markdown(
            """
            <div class="empty-illustration">
                <div style="font-size: 42px;">🔍</div>
                <div style="font-size: 16px; font-weight: 600; margin: 8px 0; color: #E2E8F0;">尚未搜尋</div>
                <div style="font-size: 13px; color: #94A3B8; line-height: 1.6;">
                    在左側設定 <b style="color:#E2E8F0;">關鍵字、日期模式、採購性質、決標方式</b> 後<br>
                    按 <b style="color:#5B8DEF;">🚀 開始搜尋標案</b> 即可擷取最新公告<br>
                    <span style="color:#7DD3FC;">💡 首次建議：維持預設「勞務 + 最低標 + 等標期內」直接搜尋，約 1-3 分鐘完成</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        base_rows = _qualified if st.session_state.include_misses else _keyword_hits

        col_q, col_exp = st.columns([3, 1])
        with col_q:
            q = st.text_input("🔍 快速搜尋（機關 / 案名 / 案號 / 關鍵字）", key="filter_matched", placeholder="輸入關鍵字即時過濾…")
        with col_exp:
            st.write("")
            st.caption("👇 可展開下方進行預算、急迫度多維度篩選")

        # 進階過濾面板
        with st.expander("🎛️ 進階條件篩選器 (預算金額 / 截標急迫度 / 決標狀態)", expanded=False):
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                budget_range = st.slider(
                    "預算金額範圍 (萬元)",
                    min_value=0,
                    max_value=2000,
                    value=(0, 2000),
                    step=50,
                    key="budget_slider_matched",
                    help="設為 0 代表不設下限/上限；超過 2,000 萬的標案在上限為 2,000 時仍會納入",
                )
            with fc2:
                urgency_sel = st.selectbox("截止投標急迫度", options=URGENCY_OPTIONS, key="urgency_sel_matched")
            with fc3:
                award_status_sel = st.selectbox("決標校驗狀態", options=AWARD_STATUS_FILTER_OPTIONS, key="award_sel_matched")

        min_b = budget_range[0] if budget_range[0] > 0 else 0
        max_b = budget_range[1] if budget_range[1] < 2000 else 0

        filtered = build_advanced_display_rows(
            base_rows,
            hide_pending_filter=st.session_state.hide_pending,
            query=q,
            min_budget_wan=min_b,
            max_budget_wan=max_b,
            urgency=urgency_sel,
            award_status=award_status_sel,
        )

        if not filtered:
            st.warning("無符合篩選條件的精選標案。請調整搜尋關鍵字或放寬進階篩選條件。")
        else:
            df = tenders_to_dataframe(filtered)
            display_df = df[DISPLAY_COLUMNS] if set(DISPLAY_COLUMNS).issubset(df.columns) else df
            st.dataframe(
                display_df,
                width="stretch",
                hide_index=True,
                height=min(580, 80 + 35 * min(len(display_df), 18)),
                column_config={
                    "#": st.column_config.NumberColumn("#", width="small"),
                    "公告日期": st.column_config.TextColumn("公告日期", width="small"),
                    "招標機關": st.column_config.TextColumn("招標機關", width="medium"),
                    "標案名稱": st.column_config.TextColumn("標案名稱", width="large"),
                    "預算金額": st.column_config.NumberColumn("預算金額", width="small", help="點擊此欄可按金額正確排序（數值排序）", format="%d 元"),
                    "決標方式": st.column_config.TextColumn("決標方式", width="medium", help="🟠=推估待確認，⚪=已確認但不符目前決標篩選"),
                    "招標方式": st.column_config.TextColumn("招標方式", width="medium"),
                    "決標方式來源": st.column_config.TextColumn("決標依據", width="small"),
                    "截止投標": st.column_config.TextColumn("截止投標", width="small"),
                    "命中關鍵字": st.column_config.TextColumn("命中關鍵字", width="small"),
                    "詳細連結": st.column_config.LinkColumn("詳細連結", display_text="🔗 開啟", width="small", help="點擊開啟官方公告頁面"),
                },
            )
            st.caption(f"顯示 {len(filtered)} / {len(base_rows)} 筆（原始符合條件 {len(_qualified)} 筆） · 點擊欄頭排序（含預算數值排序） · 點擊「詳細連結」開啟官方頁面 · 🟠=推估待確認，⚪=已確認但不符")

        # 快速收藏操作區
        if filtered:
            with st.expander("⭐ 標案收藏與追蹤操作", expanded=False):
                col_bm1, col_bm2 = st.columns([4, 1])
                option_map = {tender_key(t): t for t in filtered if tender_key(t)}
                missing_count = len(filtered) - len(option_map)
                with col_bm1:
                    selected_ids = st.multiselect(
                        "選取要加入追蹤的標案：",
                        options=list(option_map.keys()),
                        format_func=lambda k: tender_label(option_map[k]),
                        placeholder="請選擇一筆或多筆標案…",
                        key="bm_select_matched",
                    )
                    if missing_count > 0:
                        st.caption(f"有 {missing_count} 筆標案缺少案號，無法加入追蹤")
                with col_bm2:
                    st.write("")
                    if st.button("➕ 加入追蹤", width="stretch", key="btn_add_bm_matched"):
                        if selected_ids:
                            wl = st.session_state.watchlist
                            added_count = 0
                            for key_id in selected_ids:
                                tender_obj = option_map[key_id]
                                if key_id not in wl:
                                    wl[key_id] = tender_obj
                                    added_count += 1
                            st.session_state.watchlist = wl
                            save_watchlist(wl)
                            st.toast(f"已加入 {added_count} 筆標案至追蹤清單！", icon="⭐")
                            st.rerun()

        st.divider()
        st.markdown("**匯出**")
        export_rows = filtered if filtered else base_rows
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
        st.markdown(
            """
            <div class="empty-illustration">
                <div style="font-size: 42px;">📋</div>
                <div style="font-size: 14px; color: #94A3B8; margin-top: 6px;">尚無資料，請先在精選分頁完成搜尋</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        col_q2, col_exp2 = st.columns([3, 1])
        with col_q2:
            q2 = st.text_input("🔍 快速搜尋（機關 / 案名 / 案號 / 關鍵字）", key="filter_all", placeholder="輸入關鍵字即時過濾…")
        with col_exp2:
            st.write("")
            st.caption("👇 可展開下方進行進階篩選")

        with st.expander("🎛️ 進階條件篩選器 (預算金額 / 截標急迫度 / 決標狀態)", expanded=False):
            fc1_a, fc2_a, fc3_a = st.columns(3)
            with fc1_a:
                budget_range_all = st.slider(
                    "預算金額範圍 (萬元)",
                    min_value=0,
                    max_value=2000,
                    value=(0, 2000),
                    step=50,
                    key="budget_slider_all",
                )
            with fc2_a:
                urgency_all_sel = st.selectbox("截止投標急迫度", options=URGENCY_OPTIONS, key="urgency_sel_all")
            with fc3_a:
                award_status_all_sel = st.selectbox("決標校驗狀態", options=AWARD_STATUS_FILTER_OPTIONS, key="award_sel_all")

        min_b_all = budget_range_all[0] if budget_range_all[0] > 0 else 0
        max_b_all = budget_range_all[1] if budget_range_all[1] < 2000 else 0

        filtered_all = build_advanced_display_rows(
            st.session_state.tenders_all,
            hide_pending_filter=False,
            query=q2,
            min_budget_wan=min_b_all,
            max_budget_wan=max_b_all,
            urgency=urgency_all_sel,
            award_status=award_status_all_sel,
        )

        if not filtered_all:
            st.warning("無符合篩選條件的標案。")
        else:
            df_all = tenders_to_dataframe(filtered_all)
            display_all = df_all[DISPLAY_COLUMNS] if set(DISPLAY_COLUMNS).issubset(df_all.columns) else df_all
            st.dataframe(
                display_all,
                width="stretch",
                hide_index=True,
                height=min(580, 80 + 35 * min(len(display_all), 18)),
                column_config={
                    "#": st.column_config.NumberColumn("#", width="small"),
                    "公告日期": st.column_config.TextColumn("公告日期", width="small"),
                    "招標機關": st.column_config.TextColumn("招標機關", width="medium"),
                    "標案名稱": st.column_config.TextColumn("標案名稱", width="large"),
                    "預算金額": st.column_config.NumberColumn("預算金額", width="small", help="點擊此欄可按金額正確排序（數值排序）", format="%d 元"),
                    "決標方式": st.column_config.TextColumn("決標方式", width="medium"),
                    "招標方式": st.column_config.TextColumn("招標方式", width="medium"),
                    "決標方式來源": st.column_config.TextColumn("決標依據", width="small"),
                    "截止投標": st.column_config.TextColumn("截止投標", width="small"),
                    "命中關鍵字": st.column_config.TextColumn("命中關鍵字", width="small"),
                    "詳細連結": st.column_config.LinkColumn("詳細連結", display_text="🔗 開啟", width="small"),
                },
            )
            st.caption(f"顯示 {len(filtered_all)} / {len(st.session_state.tenders_all)} 筆 · 點擊欄頭排序（含預算數值排序） · 點擊「詳細連結」開啟官方頁面")

        if filtered_all:
            with st.expander("⭐ 標案收藏與追蹤操作", expanded=False):
                col_bm1_a, col_bm2_a = st.columns([4, 1])
                option_map_all = {tender_key(t): t for t in filtered_all if tender_key(t)}
                missing_count_all = len(filtered_all) - len(option_map_all)
                with col_bm1_a:
                    selected_ids_all = st.multiselect(
                        "選取要加入追蹤的標案：",
                        options=list(option_map_all.keys()),
                        format_func=lambda k: tender_label(option_map_all[k]),
                        placeholder="請選擇一筆或多筆標案…",
                        key="bm_select_all",
                    )
                    if missing_count_all > 0:
                        st.caption(f"有 {missing_count_all} 筆標案缺少案號，無法加入追蹤")
                with col_bm2_a:
                    st.write("")
                    if st.button("➕ 加入追蹤", width="stretch", key="btn_add_bm_all"):
                        if selected_ids_all:
                            wl = st.session_state.watchlist
                            added_count = 0
                            for key_id in selected_ids_all:
                                tender_obj = option_map_all[key_id]
                                if key_id not in wl:
                                    wl[key_id] = tender_obj
                                    added_count += 1
                            st.session_state.watchlist = wl
                            save_watchlist(wl)
                            st.toast(f"已加入 {added_count} 筆標案至追蹤清單！", icon="⭐")
                            st.rerun()

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
    watchlist_items = list(st.session_state.watchlist.values())
    if not watchlist_items:
        st.markdown(
            """
            <div class="empty-illustration">
                <div style="font-size: 42px;">⭐</div>
                <div style="font-size: 16px; font-weight: 600; margin: 8px 0; color: #E2E8F0;">目前追蹤清單尚無標案</div>
                <div style="font-size: 13px; color: #94A3B8;">
                    在「🏆 精選」或「📋 所有標案」展開「標案收藏與追蹤操作」即可將關注的標案加入此清單。<br>
                    追蹤資料會自動保存於 <code>output/watchlist.json</code>，下次開啟程式自動保留。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.caption(f"📌 共收藏 {len(watchlist_items)} 筆標案 · 資料永久保存於 `output/watchlist.json`")
        df_wl = tenders_to_dataframe(watchlist_items)
        display_wl = df_wl[DISPLAY_COLUMNS] if set(DISPLAY_COLUMNS).issubset(df_wl.columns) else df_wl
        st.dataframe(
            display_wl,
            width="stretch",
            hide_index=True,
            height=min(500, 80 + 35 * min(len(display_wl), 15)),
            column_config={
                "#": st.column_config.NumberColumn("#", width="small"),
                "公告日期": st.column_config.TextColumn("公告日期", width="small"),
                "招標機關": st.column_config.TextColumn("招標機關", width="medium"),
                "標案名稱": st.column_config.TextColumn("標案名稱", width="large"),
                "預算金額": st.column_config.NumberColumn("預算金額", width="small", format="%d 元"),
                "決標方式": st.column_config.TextColumn("決標方式", width="medium"),
                "招標方式": st.column_config.TextColumn("招標方式", width="medium"),
                "決標方式來源": st.column_config.TextColumn("決標依據", width="small"),
                "截止投標": st.column_config.TextColumn("截止投標", width="small"),
                "命中關鍵字": st.column_config.TextColumn("命中關鍵字", width="small"),
                "詳細連結": st.column_config.LinkColumn("詳細連結", display_text="🔗 開啟", width="small"),
            },
        )

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
            csv_wl_bytes = cached_csv_bytes(rows_fingerprint(watchlist_items), watchlist_items)
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
