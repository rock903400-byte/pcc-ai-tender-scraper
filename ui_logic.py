# -*- coding: utf-8 -*-
"""
前端純邏輯層（不依賴前端框架）

本模組集中所有與前端框架無關的純資料轉換函式，
原先散落在 app.py 內因頂層直接初始化而無法被單元測試，
現抽離至此以便離線測試。

搬移原則：邏輯一行不改，只搬家；唯一例外是 `tenders_to_dataframe`
需將原本隱式依賴的 session 狀態改為顯式參數。
"""

import re
from datetime import date

import pandas as pd

import pcc_core as core

# 與 app.py 保持一致的常數（僅搬移純邏輯所需的子集）
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
    "剩餘天數",
    "命中關鍵字",
    "詳細連結",
]

PENDING_PREFIX = "🟠 "
DISQUALIFIED_PREFIX = "⚪ "

KEYWORDS_MAX_CHARS = 500
KEYWORDS_MAX_WORDS = 100
KEYWORDS_MAX_WORD_LEN = 30


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


def format_remaining_days(deadline_str: str) -> str:
    """依剩餘天數回傳對應 emoji 文字，與篩選器門檻一致。"""
    days = get_days_remaining(deadline_str)
    if days is None:
        return "—"
    if days < 0:
        return "已截標"
    if days <= 3:
        return f"🔥 {days} 天"
    if days <= 7:
        return f"⏳ {days} 天"
    if days <= 14:
        return f"📅 {days} 天"
    return f"🗓️ {days} 天"


def tenders_to_dataframe(tenders: list, active_award_target: str = "最低標") -> pd.DataFrame:
    if not tenders:
        return pd.DataFrame(columns=DISPLAY_COLUMNS)
    data = []
    for idx, t in enumerate(tenders, 1):
        award_display = t.get("決標方式", "")
        source = t.get("決標方式來源", "")
        if is_award_pending(t):
            award_display = f"{PENDING_PREFIX}{award_display}"
        elif core.is_award_confirmed(t):
            matched = core.filter_tenders([t], "不限", active_award_target)
            if not matched:
                award_display = f"{DISQUALIFIED_PREFIX}{award_display}"
        budget_text = t.get("預算金額", "")
        budget_val = core.parse_amount(budget_text)
        budget_num = int(budget_val) if budget_val != -1.0 else 0
        remaining = format_remaining_days(t.get("截止投標", ""))
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
            "剩餘天數": remaining,
            "命中關鍵字": t.get("命中關鍵字", "") or "—",
            "詳細連結": t.get("詳細連結", ""),
            "標案案號": t.get("標案案號", ""),
            "pk": t.get("pk", ""),
        })
    df = pd.DataFrame(data)
    return df


# ==================== 看板聚合（回傳 DataFrame，不畫圖） ====================

def budget_tier_frame(tenders: list) -> pd.DataFrame:
    """依預算級距分箱，級距門檻與排序照抄 render_analytics_dashboard。"""
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
    for t in tenders:
        a = core.parse_amount(t.get("預算金額", ""))
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
    return tier_df


def award_composition_frame(tenders: list) -> pd.DataFrame:
    """依決標方式構成統計，回傳 DataFrame。"""
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
    return award_df


def top_agencies_frame(tenders: list, top_n: int = 10) -> pd.DataFrame:
    """依標案筆數統計 Top N 招標機關，依筆數遞減排序。"""
    agency_stats = {}
    for t in tenders:
        agency = t.get("招標機關", "未知機關")
        budget_val = core.parse_amount(t.get("預算金額", ""))
        amt = budget_val if budget_val > 0 else 0
        if agency not in agency_stats:
            agency_stats[agency] = {"count": 0, "total_amt": 0}
        agency_stats[agency]["count"] += 1
        agency_stats[agency]["total_amt"] += amt
    sorted_agencies = sorted(agency_stats.items(), key=lambda x: x[1]["count"], reverse=True)[:top_n]
    agency_df = pd.DataFrame([
        {
            "招標機關": a[0],
            "標案筆數": a[1]["count"],
            "累積預算(萬元)": round(a[1]["total_amt"] / 10000.0, 1),
        }
        for a in sorted_agencies
    ])
    return agency_df


def keyword_ranking_frame(tenders: list, top_n: int = 12) -> pd.DataFrame:
    """依命中關鍵字統計熱度排行，以 `、` 拆分並忽略 `—`。"""
    kw_counts = {}
    for t in tenders:
        hit_kws = t.get("命中關鍵字", "")
        if hit_kws and hit_kws != "—":
            for kw in hit_kws.split("、"):
                kw = kw.strip()
                if kw:
                    kw_counts[kw] = kw_counts.get(kw, 0) + 1
    if not kw_counts:
        return pd.DataFrame(columns=["關鍵字", "命中次數"])
    sorted_kws = sorted(kw_counts.items(), key=lambda x: x[1], reverse=True)[:top_n]
    kw_df = pd.DataFrame([{"關鍵字": k[0], "命中次數": k[1]} for k in sorted_kws])
    return kw_df


def urgency_bins_frame(tenders: list) -> pd.DataFrame:
    """依截標急迫度分箱，門檻與 emoji 與看板一致。"""
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
    return urgency_df


def kpi_summary(tenders: list) -> dict:
    """回傳 KPI 彙總，唯一行為變更：無有效預算時 max_amount 回 0 而非 -1.0。"""
    amounts = [core.parse_amount(t.get("預算金額", "")) for t in tenders]
    valid_amounts = [a for a in amounts if a > 0]
    total_budget = sum(valid_amounts)
    avg_budget = total_budget / len(valid_amounts) if valid_amounts else 0
    if valid_amounts:
        # 僅在有有效預算時取最大值，避免全為 -1.0 時取到 -1.0
        max_tender = max(tenders, key=lambda t: core.parse_amount(t.get("預算金額", "")))
        max_amount = core.parse_amount(max_tender.get("預算金額", ""))
        # 防呆：若最大值仍為無效（理論上不會，因 valid 非空），則回 0
        if max_amount <= 0:
            max_amount = 0
            max_tender_name = ""
        else:
            max_tender_name = max_tender.get("標案名稱", "")
    else:
        max_tender = None
        max_amount = 0
        max_tender_name = ""
    confirmed_count = sum(1 for t in tenders if core.is_award_confirmed(t))
    confirmed_ratio = confirmed_count / len(tenders) * 100 if tenders else 0
    return {
        "total_budget": total_budget,
        "avg_budget": avg_budget,
        "max_amount": max_amount,
        "max_tender_name": max_tender_name,
        "confirmed_count": confirmed_count,
        "confirmed_ratio": confirmed_ratio,
        "total_count": len(tenders),
    }
