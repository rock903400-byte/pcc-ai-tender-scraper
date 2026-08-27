# -*- coding: utf-8 -*-
"""
公開資料鏡像來源（g0v / openfun「標案瀏覽」API）。

官網詳細頁 (searchTenderDetail) 對「決標方式」這一個欄位設了 IP 層級的額度制，
連續約 5 筆就回驗證碼檢核頁、冷卻分鐘級（見 pcc_core 的節流註解），
待確認清單因此永遠補不完。

本模組改由鏡像 API 取同一個欄位——它以政府公開資料重新發布同一批公告，含當日資料：

    GET /api/listbydate?date=YYYYMMDD          單次回傳當日全部公告（實測 2,392 筆／日）
    GET /api/tender?unit_id=…&job_number=…     回傳該案所有公告，detail 內含「招標資料:決標方式」
    GET /api/searchbytitle?query=…             以標案名稱反查（day index 撲空時的備援）

實測（2026-08）：間隔 1.5 秒約 85% 成功，偶發 429 下一筆即恢復，
吞吐約 30–40 筆/分鐘，比官網的 5 筆/輪高兩個數量級。

刻意與 pcc_core.fetch_award_method_status 對齊回傳格式 (值, 狀態)，
狀態同為 "ok" / "blocked" / "error"，呼叫端才能把兩個來源當成可互換的零件。
"""

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request


# ==================== 端點與節流 ====================

MIRROR_BASE = "https://pcc-api.openfun.app"
LIST_BY_DATE_PATH = "/api/listbydate"
TENDER_PATH = "/api/tender"
SEARCH_BY_TITLE_PATH = "/api/searchbytitle"

# 鏡像站對預設的 urllib User-Agent 直接回 403，一定要帶瀏覽器 UA。
# 不共用 core.HEADERS：那組帶著 Origin / Referer / Content-Type，是給官網表單用的。
MIRROR_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# 對方是志工維運的免費服務，請求間隔寧可保守一點
MIRROR_DELAY_RANGE = (1.5, 2.2)

# 429 的退避秒數（實測是軟限制：下一筆通常就恢復，不需要分鐘級冷卻）
MIRROR_RETRY_BACKOFF = (2.0, 5.0)

# 連續這麼多筆拿不到就判定鏡像此刻不可靠，整輪改走官網
MIRROR_FAIL_STREAK = 5

# detail 內的鍵名。「無法決標公告:招標方式」之類的同名尾綴不能誤收，故用全等比對。
AWARD_DETAIL_KEY = "招標資料:決標方式"

TIMEOUT = 20


# ==================== HTTP ====================

def fetch_json(path: str, params: dict) -> tuple:
    """
    向鏡像 API 取 JSON，回傳 (物件, 狀態)。

    狀態為 "ok"（拿到 JSON）、"blocked"（429，退避重試後仍被限流）或
    "error"（連線失敗／回傳非 JSON）。兩者要分開，呼叫端才知道該不該稍後重試。
    """
    url = f"{MIRROR_BASE}{path}?{urllib.parse.urlencode(params)}"
    attempts = len(MIRROR_RETRY_BACKOFF) + 1
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=MIRROR_HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as err:
            if err.code == 429 and attempt < attempts - 1:
                time.sleep(MIRROR_RETRY_BACKOFF[attempt])
                continue
            return None, ("blocked" if err.code == 429 else "error")
        except Exception:
            return None, "error"

        try:
            return json.loads(raw), "ok"
        except ValueError:
            # 查無此端點時對方回的是 HTML 錯誤頁而非 404，只能靠解析失敗認出來
            return None, "error"
    return None, "blocked"


# ==================== 當日索引 ====================

def to_mirror_day(pub_date: str) -> str:
    """把「YYYY/MM/DD」（西元，parse_tender_rows 存的格式）轉成鏡像用的 YYYYMMDD。"""
    digits = "".join(ch for ch in str(pub_date) if ch.isdigit())
    return digits if len(digits) == 8 else ""


def _index_entry(record) -> dict:
    """把一筆 listbydate / searchbytitle 記錄壓成索引條目；欄位不全時回 None。"""
    if not isinstance(record, dict):
        return None
    job_number = str(record.get("job_number") or "").strip()
    unit_id = str(record.get("unit_id") or "").strip()
    if not job_number or not unit_id:
        return None
    brief = record.get("brief") or {}
    return {
        "job_number": job_number,
        "unit_id": unit_id,
        "unit_name": str(record.get("unit_name") or "").strip(),
        "title": str(brief.get("title") or "").strip(),
        "type": str(brief.get("type") or "").strip(),
    }


def build_index(payload) -> dict:
    """把 API 回應的 records 壓成 {標案案號: [索引條目, ...]}。"""
    index = {}
    if not isinstance(payload, dict):
        return index
    for record in payload.get("records") or []:
        entry = _index_entry(record)
        if entry:
            index.setdefault(entry["job_number"], []).append(entry)
    return index


def fetch_day_index(day: str) -> dict:
    """
    取某日全部公告並建成索引。

    一天只要一次請求就涵蓋當天所有標案，是本模組能省下大量請求的關鍵。
    取不到時回空 dict（呼叫端會退回以標案名稱反查）。
    """
    if not day:
        return {}
    payload, status = fetch_json(LIST_BY_DATE_PATH, {"date": day})
    return build_index(payload) if status == "ok" else {}


def resolve_unit_id(index: dict, tender: dict) -> str:
    """
    由索引找出該標案的 unit_id；無法唯一鎖定時回空字串。

    案號會跨機關重複（例如同日就有兩個機關都用「115-004」），所以先以案號取候選，
    再用招標機關名、標案名稱消歧義。寧可回空讓呼叫端退回反查，也不要張冠李戴——
    把別人的決標方式寫進快取，使用者是看不出來的。
    """
    candidates = index.get(str(tender.get("標案案號") or "").strip()) or []
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]["unit_id"]

    org = str(tender.get("招標機關") or "").strip()
    if org:
        matched = [c for c in candidates if c["unit_name"] == org]
        if len(matched) == 1:
            return matched[0]["unit_id"]
        if matched:
            candidates = matched

    title = str(tender.get("標案名稱") or "").strip()
    if title:
        matched = [c for c in candidates if c["title"] == title]
        if len(matched) == 1:
            return matched[0]["unit_id"]

    # 剩下的候選若全指向同一個 unit_id（同機關的原公告＋更正公告），仍可安全採用
    unit_ids = {c["unit_id"] for c in candidates}
    return unit_ids.pop() if len(unit_ids) == 1 else ""


def search_unit_id(tender: dict) -> tuple:
    """以標案名稱反查 unit_id，回傳 (unit_id, 狀態)。day index 撲空時的備援。"""
    title = str(tender.get("標案名稱") or "").strip()
    if not title:
        return "", "error"
    payload, status = fetch_json(SEARCH_BY_TITLE_PATH, {"query": title, "page": 1})
    if status != "ok":
        return "", status
    return resolve_unit_id(build_index(payload), tender), "ok"


# ==================== 決標方式 ====================

def extract_award_method(payload) -> str:
    """
    從 /api/tender 的回應取出決標方式。

    同一案會有原公告、更正公告、無法決標公告等多筆 records，取【最新的招標公告】：
    更正公告會改動決標方式，拿舊的那筆等於給錯答案。
    """
    if not isinstance(payload, dict):
        return ""
    best_date = -1
    best_value = ""
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            continue
        detail = record.get("detail") or {}
        value = str(detail.get(AWARD_DETAIL_KEY) or "").strip() if isinstance(detail, dict) else ""
        if not value:
            continue
        try:
            record_date = int(record.get("date") or 0)
        except (TypeError, ValueError):
            record_date = 0
        if record_date >= best_date:
            best_date = record_date
            best_value = value
    return best_value


def fetch_award_method_status(tender: dict, index_cache: dict = None) -> tuple:
    """
    以鏡像 API 取一筆標案的決標方式，回傳 (值, 狀態)。

    狀態與 pcc_core.fetch_award_method_status 同義：
      ok      —— 取到欄位
      blocked —— 被鏡像站限流（429），稍後重試就有
      error   —— 查無此案、缺欄位或連線失敗，重試也一樣，該退回官網詳細頁

    index_cache 為 {YYYYMMDD: day_index} 的呼叫端快取，同一天只會打一次 listbydate。
    """
    unit_id = ""
    if index_cache is not None:
        day = to_mirror_day(tender.get("公告日期", ""))
        if day:
            if day not in index_cache:
                index_cache[day] = fetch_day_index(day)
            unit_id = resolve_unit_id(index_cache[day], tender)

    if not unit_id:
        unit_id, status = search_unit_id(tender)
        if status == "blocked":
            return "", "blocked"
    if not unit_id:
        return "", "error"

    job_number = str(tender.get("標案案號") or "").strip()
    payload, status = fetch_json(TENDER_PATH,
                                 {"unit_id": unit_id, "job_number": job_number})
    if status != "ok":
        return "", status

    value = extract_award_method(payload)
    return (value, "ok") if value else ("", "error")


def polite_delay():
    """對免費鏡像服務的請求間隔。抽成函式，測試才好把它換掉。"""
    time.sleep(random.uniform(*MIRROR_DELAY_RANGE))
