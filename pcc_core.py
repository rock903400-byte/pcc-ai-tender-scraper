# -*- coding: utf-8 -*-
"""
政府電子採購網 (web.pcc.gov.tw) 標案查詢共用核心。

app.py (GUI) 與 crawler.py (CLI) 共用本模組，確保分頁、重試、驗證碼偵測、
表頭欄位對照與決標方式判定等邏輯只有一份實作。
"""

import concurrent.futures
import csv
import math
import random
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


# ==================== 網站端點與常數 ====================

BASE_URL = "https://web.pcc.gov.tw"
BASIC_SEARCH_URL = BASE_URL + "/prkms/tender/common/basic/readTenderBasic"
BASIC_INDEX_URL = BASE_URL + "/prkms/tender/common/basic/indexTenderBasic"
DETAIL_URL = BASE_URL + "/tps/QueryTender/query/searchTenderDetail"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": BASE_URL,
    "Referer": BASIC_INDEX_URL,
}

# 採購性質 -> 網站表單參數
PROCTRG_CATE = {
    "工程": "RAD_PROCTRG_CATE_1",
    "財物": "RAD_PROCTRG_CATE_2",
    "勞務": "RAD_PROCTRG_CATE_3",
}

PAGE_SIZE = 50

# 查詢的日期模式。
#   isSpdt：等標期內（現正可投標）。站方在此模式下【完全忽略】日期區間參數。
#   isDate：公告日期區間。此模式下 tenderStartDate / tenderEndDate 必須送【西元】日期，
#           送民國日期會回 0 筆。
DATE_TYPE_SPDT = "isSpdt"
DATE_TYPE_RANGE = "isDate"

DATE_MODE_SPDT = "等標期內"
DATE_MODE_RANGE = "公告日期區間"
DATE_MODES = {
    DATE_MODE_SPDT: DATE_TYPE_SPDT,
    DATE_MODE_RANGE: DATE_TYPE_RANGE,
}

# 未登入使用者可查詢的公告日期區間上限（取自站方 basicTenderSearch() 的檢核）
MAX_RANGE_DAYS = 186

# 翻頁上限（50 筆/頁）。全面掃描的實測量：勞務標案 7 天約 1,600 筆 / 32 頁、
# 14 天約 2,900 筆 / 58 頁、30 天約 5,600 筆 / 112 頁，故上限訂在 120 頁（6,000 筆）。
DEFAULT_MAX_PAGES = 120

# 詳細頁校驗的預設筆數上限與節流設定
DEFAULT_VERIFY_LIMIT = 300
DETAIL_DELAY_RANGE = (0.3, 0.6)
CAPTCHA_STREAK_LIMIT = 5

# 對政府伺服器的禮貌間隔（秒）
PAGE_DELAY = 0.8

# 決標方式的資料來源，讓使用者一眼看出哪些列只是推估值
AWARD_SOURCE_ESTIMATED = "依招標方式推估"
AWARD_SOURCE_OFFICIAL = "官方詳細頁"

# 匯出報表的欄位順序
PREFERRED_COLS = [
    "完全符合目標", "標案名稱", "招標機關", "預算金額", "決標方式", "招標方式",
    "採購性質", "公告日期", "截止投標", "命中關鍵字", "標案案號", "公告類型",
    "決標方式來源", "詳細連結",
]

# 僅供內部流程使用、不應出現在匯出檔中的鍵
INTERNAL_KEYS = ("pk", "命中關鍵字群")

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


# ==================== 環境設定 ====================

_ipv4_installed = False


def install_ipv4_preference():
    """強制優先使用 IPv4，避免 Windows 下連線政府伺服器逾時。"""
    global _ipv4_installed
    if _ipv4_installed:
        return
    orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_getaddrinfo(*args, **kwargs):
        res = orig_getaddrinfo(*args, **kwargs)
        v4 = [r for r in res if r[0] == socket.AF_INET]
        return v4 or res

    socket.getaddrinfo = _ipv4_getaddrinfo
    _ipv4_installed = True


def enable_utf8_console():
    """支援 Windows 終端機 UTF-8 輸出。"""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ==================== 日期轉換 ====================

def to_roc_date(date_str: str) -> str:
    """西元 YYYY/MM/DD 或 YYYY-MM-DD 轉民國 YYY/MM/DD"""
    parts = date_str.strip().replace("-", "/").split("/")
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    return f"{y - 1911}/{m:02d}/{d:02d}"


def to_ad_date(date_str: str) -> str:
    """民國 YYY/MM/DD 轉西元 YYYY/MM/DD；非民國日期原樣回傳。"""
    raw = date_str.strip()
    parts = raw.replace("-", "/").split("/")
    if len(parts) != 3:
        return raw
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2].split()[0])
    except ValueError:
        return raw
    if y >= 1900:
        return raw
    return f"{y + 1911}/{m:02d}/{d:02d}"


def parse_amount(text) -> float:
    """
    把「2,608,500 元」之類的金額字串轉為數值，供排序使用。

    無法解析時回傳 -1.0，讓空白金額在升冪排序中排在最前、降冪時排在最後。
    """
    digits = re.sub(r"[^\d.]", "", str(text))
    try:
        return float(digits) if digits else -1.0
    except ValueError:
        return -1.0


# ==================== HTTP ====================

def http_post(url: str, data: dict, max_retries: int = 3, timeout: int = 30) -> str:
    """發送 POST 請求，失敗時採退避重試。"""
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, data=encoded, headers=HEADERS)
            with opener.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(1.5 * attempt)
    raise last_err


def http_get(url: str, max_retries: int = 2, timeout: int = 12) -> str:
    """發送 GET 請求，失敗時採退避重試。"""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with opener.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(1.0 * attempt)
    raise last_err


# 站方頻率防護頁的特徵字串。搜尋頁是圖形驗證碼，詳細頁則是「撲克牌」驗證碼檢核頁
# （含 <form id="validateForm" action="/tps/validate/check">），兩者都要認得。
CAPTCHA_MARKERS = (
    "圖形驗證碼",
    "請輸入驗證碼",
    "驗證碼檢核",
    "撲克牌",
    'id="validateForm"',
    "/tps/validate/check",
)


def is_captcha_page(html_doc: str) -> bool:
    """判斷回應是否為頻率防護的驗證碼頁。"""
    return any(marker in html_doc for marker in CAPTCHA_MARKERS)


# ==================== HTML 解析 ====================

_SCRIPT_RE = re.compile(r"<script.*?</script>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_ENTITIES = {
    "&emsp;": " ", "&ensp;": " ", "&nbsp;": " ",
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
}


def strip_tags(fragment: str, drop_scripts: bool = True) -> str:
    """移除 HTML 標籤與常見實體，並壓縮空白。"""
    text = _SCRIPT_RE.sub("", fragment) if drop_scripts else fragment
    text = _TAG_RE.sub("", text)
    for ent, rep in _ENTITIES.items():
        text = text.replace(ent, rep)
    return " ".join(text.split())


# 欄位 -> (搜尋結果表頭文字, 表頭缺失時的預設 td 索引)
# 註：「標案案號」與「標案名稱」共用同一個 td，表頭文字為兩者相連。
FIELD_LAYOUT = {
    "org": ("機關名稱", 1),
    "id_name": ("標案案號標案名稱", 2),
    "way": ("招標方式", 4),
    "cate": ("採購性質", 5),
    "pub": ("公告日期", 6),
    "deadline": ("截止投標", 7),
    "budget": ("預算金額", 8),
}

DEFAULT_COLUMN_INDEX = {field: default for field, (_, default) in FIELD_LAYOUT.items()}


def parse_column_index(html_doc: str):
    """
    依搜尋結果表頭文字建立「欄位 -> td 索引」對照表，取代硬編索引。

    回傳 (對照表, 警告訊息清單)。任一欄位無法由表頭定位時會退回預設索引，
    並產生警告，避免網站改版後靜默輸出錯位資料。
    """
    header_cells = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html_doc, re.DOTALL):
        cells = re.findall(r"<th[^>]*>(.*?)</th>", row, re.DOTALL)
        if cells:
            header_cells = [strip_tags(c).replace(" ", "") for c in cells]
            break

    if not header_cells:
        return dict(DEFAULT_COLUMN_INDEX), ["找不到搜尋結果表頭，改用預設欄位索引。"]

    positions = {}
    for idx, text in enumerate(header_cells):
        positions.setdefault(text, idx)

    index_map = {}
    warnings = []
    for field, (header_text, default_idx) in FIELD_LAYOUT.items():
        if header_text in positions:
            index_map[field] = positions[header_text]
        else:
            index_map[field] = default_idx
            warnings.append(
                f"表頭找不到【{header_text}】，該欄改用預設索引 {default_idx}（網站可能已改版）。"
            )
    return index_map, warnings


def _split_id_and_name(cell_html: str):
    """
    從「標案案號／標案名稱」複合儲存格拆出案號、名稱與公告類型註記。

    版面為：案號<span style="color:red">(更正公告)</span><br><a ...>標案名稱</a>
    標案名稱通常由 pageCode2Img() 以 JS 產生，需另行萃取。
    """
    head_raw = re.split(r"<br\s*/?>", cell_html, maxsplit=1)[0]

    note_match = re.search(
        r'<span[^>]*color:\s*red[^>]*>(.*?)</span>', head_raw, re.DOTALL | re.IGNORECASE
    )
    note = strip_tags(note_match.group(1)).strip("（）() ") if note_match else ""
    head_clean = re.sub(
        r'<span[^>]*color:\s*red[^>]*>.*?</span>', "", head_raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    tender_id = strip_tags(head_clean)

    name = ""
    img_match = re.findall(r'pageCode2Img\(["\'](.*?)["\']\)', cell_html)
    if img_match:
        name = img_match[0].strip()
    if not name:
        link_match = re.search(r"<a[^>]*>(.*?)</a>", cell_html, re.DOTALL)
        if link_match:
            name = strip_tags(link_match.group(1))

    return tender_id, name, note


def determine_award_method(tender_way: str, actual_award_str: str = "") -> tuple:
    """
    判定決標方式（最低標 vs 參考最有利標／最有利標／評選）。

    1. 詳細頁的「決標方式」欄位為最高準則。
    2. 否則依招標方式保守推估，待後續詳細頁校驗覆蓋。
    """
    if actual_award_str:
        s = actual_award_str.strip()
        if "參考最有利標" in s or "最有利標" in s or "評審" in s or "評選" in s:
            return s, False
        if "最低標" in s:
            return s, True
        return s, False

    tender_way = tender_way.strip()
    if "評選" in tender_way or "最有利標" in tender_way or "評審" in tender_way:
        return "最有利標 / 評選", False
    if "公開取得" in tender_way:
        return "公開取得 (待確認)", True
    if "公開招標" in tender_way:
        return "最低標 (公開招標)", True
    if "選擇性招標" in tender_way:
        return "最低標 (選擇性招標)", True
    if "限制性招標" in tender_way:
        return "限制性招標", False

    return tender_way or "未標明", False


def parse_tender_rows(html_doc: str, keyword: str, col_index: dict = None) -> list:
    """解析搜尋結果表格列。col_index 省略時自動由表頭推導。"""
    if col_index is None:
        col_index, _ = parse_column_index(html_doc)

    required_len = max(col_index.values()) + 1
    tenders = []

    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html_doc, re.DOTALL):
        pk_match = re.search(r'pk=([^&"\'>\s]+)', row)
        if not pk_match:
            continue
        pk = pk_match.group(1)

        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) < required_len:
            continue

        tender_id, tender_name, notice_type = _split_id_and_name(cells[col_index["id_name"]])
        org_name = strip_tags(cells[col_index["org"]])
        if not tender_id or not org_name:
            continue

        tender_way = strip_tags(cells[col_index["way"]])
        proc_type = strip_tags(cells[col_index["cate"]])
        budget = strip_tags(cells[col_index["budget"]])

        is_service = "勞務" in proc_type
        award_method_desc, is_lowest = determine_award_method(tender_way)

        tenders.append({
            "pk": pk,
            "標案案號": tender_id,
            "標案名稱": tender_name,
            "招標機關": org_name,
            "招標方式": tender_way,
            "採購性質": proc_type,
            "決標方式": award_method_desc,
            "決標方式來源": AWARD_SOURCE_ESTIMATED,
            "預算金額": f"{budget} 元" if budget and not budget.endswith("元") else budget,
            "公告日期": to_ad_date(strip_tags(cells[col_index["pub"]])),
            "截止投標": to_ad_date(strip_tags(cells[col_index["deadline"]])),
            "公告類型": notice_type,
            "是否為勞務類": "是" if is_service else "否",
            "是否為最低標": "是" if is_lowest else "否",
            "完全符合目標": "符合 (勞務+最低標)" if (is_service and is_lowest) else "其他",
            "詳細連結": f"{BASE_URL}/prkms/urlSelector/common/tpam?pk={pk}",
            "搜尋關鍵字": keyword,
        })

    return tenders


# ==================== 分頁 ====================

def parse_total_records(html_doc: str):
    """解析「共有 N 筆資料」；無法解析時回傳 None。"""
    match = re.search(r"共有\s*<span[^>]*>\s*([\d,]+)\s*</span>\s*筆資料", html_doc)
    if not match:
        match = re.search(r"共有\s*([\d,]+)\s*筆資料", html_doc)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def parse_total_pages(html_doc: str, page_size: int = PAGE_SIZE) -> int:
    """由總筆數推算總頁數；無法判定時回傳 1。"""
    total = parse_total_records(html_doc)
    if total is None:
        return 1
    return max(1, math.ceil(total / page_size))


def parse_page_param(html_doc: str):
    """
    取出 displaytag 的分頁參數名稱（形如 d-49738-p）。

    該編號隨頁面而異，必須從實際回應中讀取，不可硬編。
    """
    match = re.search(r"\b(d-\d+-p)=", html_doc)
    return match.group(1) if match else None


def build_search_form(keyword: str, start_date: str, end_date: str, proctrg_cate: str = None,
                      date_type: str = DATE_TYPE_SPDT) -> dict:
    """
    組出搜尋表單參數。

    keyword 傳空字串代表不限標案名稱（全面掃描）；proctrg_cate 為 None 代表不限採購性質。
    日期一律正規化為西元：isDate 模式下站方只接受西元日期，isSpdt 模式則會忽略日期。
    """
    form = {
        "pageSize": str(PAGE_SIZE),
        "firstSearch": "true",
        "searchType": "basic",
        "isBinding": "N",
        "isLogIn": "N",
        "orgName": "",
        "orgId": "",
        "tenderName": keyword or "",
        "tenderId": "",
        "tenderType": "TENDER_DECLARATION",
        "tenderWay": "",
        "dateType": date_type,
        "tenderStartDate": to_ad_date(start_date),
        "tenderEndDate": to_ad_date(end_date),
    }
    if proctrg_cate:
        form["radProctrgCate"] = proctrg_cate
    return form


def clamp_date_range_days(days: int, log=None) -> int:
    """公告日期區間對未登入使用者有 186 天上限，超過時夾住並示警。"""
    if days <= MAX_RANGE_DAYS:
        return days
    if callable(log):
        log(f"  [!] 公告日期區間最多 {MAX_RANGE_DAYS} 天（未登入限制），已由 {days} 天調整為 {MAX_RANGE_DAYS} 天。")
    return MAX_RANGE_DAYS


def search_pcc(keyword: str, start_date: str, end_date: str, proctrg_cate: str = None,
               max_pages: int = DEFAULT_MAX_PAGES, log=None, polite_delay: float = PAGE_DELAY,
               date_type: str = DATE_TYPE_SPDT, progress_cb=None) -> list:
    """
    搜尋標案並自動走訪所有分頁。keyword 傳空字串即為該條件下的全面掃描。

    log 為選用的單參數回呼（CLI 傳 print，GUI 傳執行緒安全的 log）；
    progress_cb(done_pages, total_pages) 為選用的翻頁進度回呼。
    """
    emit = log if callable(log) else (lambda _msg: None)

    form = build_search_form(keyword, start_date, end_date, proctrg_cate, date_type)
    try:
        html_doc = http_post(BASIC_SEARCH_URL, form)
    except Exception as e:
        emit(f"  [!] 搜尋連線失敗 ({keyword}): {e}")
        return []

    if is_captcha_page(html_doc):
        emit("  [!] 觸發網站頻率防護驗證碼，冷卻 2 秒...")
        time.sleep(2)
        return []

    col_index, warnings = parse_column_index(html_doc)
    for warning in warnings:
        emit(f"  [!] {warning}")

    total_records = parse_total_records(html_doc)
    available_pages = parse_total_pages(html_doc)
    total_pages = min(available_pages, max_pages)
    page_param = parse_page_param(html_doc)

    results = parse_tender_rows(html_doc, keyword, col_index)
    label = f"關鍵字【{keyword}】" if keyword else "全面掃描"

    if total_records is not None:
        emit(f"  [+] {label}共 {total_records} 筆 / {available_pages} 頁，開始讀取...")
    else:
        emit(f"  [+] {label}第 1 頁取得 {len(results)} 筆...")

    if available_pages > total_pages:
        skipped = (total_records - total_pages * PAGE_SIZE) if total_records else "未知"
        emit(f"  [!] 頁數超過上限（{available_pages} > {max_pages}），僅取回前 {total_pages} 頁，"
             f"約 {skipped} 筆未取回。可調高 max_pages 或縮小查詢範圍。")

    if progress_cb:
        progress_cb(1, total_pages)

    if total_pages > 1 and not page_param:
        emit("  [!] 找不到分頁參數，僅能取得第 1 頁（網站可能已改版）。")
        return results

    for page_idx in range(2, total_pages + 1):
        time.sleep(polite_delay)
        paged_form = dict(form)
        paged_form[page_param] = str(page_idx)
        try:
            page_html = http_post(BASIC_SEARCH_URL, paged_form)
        except Exception as e:
            emit(f"    第 {page_idx} 頁抓取失敗: {e}")
            continue
        if is_captcha_page(page_html):
            emit(f"    第 {page_idx} 頁觸發驗證碼，停止翻頁（已取得 {len(results)} 筆）。")
            break
        results.extend(parse_tender_rows(page_html, keyword, col_index))
        if progress_cb:
            progress_cb(page_idx, total_pages)

    return results


# ==================== 詳細頁決標方式校驗 ====================

def fetch_award_method_status(pk: str) -> tuple:
    """
    連線官方詳細頁萃取「決標方式」，回傳 (欄位值, 狀態)。

    狀態為 "ok"（取得欄位）、"blocked"（被站方驗證碼防護擋下）或 "error"
    （連線失敗或頁面無此欄位）。呼叫端要能區分 blocked，才不會一路撞牆。
    """
    if not pk:
        return "", "error"
    try:
        html = http_get(f"{DETAIL_URL}?pkPmsMain={pk}")
    except Exception:
        return "", "error"

    if is_captcha_page(html):
        return "", "blocked"

    for pattern in (r"決標方式\s*</t[hd]>\s*<td[^>]*>(.*?)</td>",
                    r"決標方式.*?</td>\s*<td[^>]*>(.*?)</td>"):
        match = re.search(pattern, html, re.DOTALL)
        if match:
            val = strip_tags(match.group(1))
            if val:
                return val, "ok"
    return "", "error"


def fetch_actual_award_method(pk: str) -> str:
    """連線官方詳細頁，萃取真實的「決標方式」欄位值（取不到時回空字串）。"""
    return fetch_award_method_status(pk)[0]


def apply_award_method(tender: dict, actual_award: str):
    """以詳細頁的真實決標方式覆蓋推估值，並同步更新衍生欄位。"""
    desc, is_lowest = determine_award_method(tender.get("招標方式", ""), actual_award)
    tender["決標方式"] = desc
    tender["決標方式來源"] = AWARD_SOURCE_OFFICIAL
    tender["是否為最低標"] = "是" if is_lowest else "否"
    is_service = tender.get("是否為勞務類") == "是"
    tender["完全符合目標"] = "符合 (勞務+最低標)" if (is_service and is_lowest) else "其他"


def select_rows_for_enrichment(tenders: list, target_attr: str = "勞務",
                               target_award: str = "最低標",
                               limit: int = DEFAULT_VERIFY_LIMIT) -> list:
    """
    挑出值得連線詳細頁校驗的標案，避免對整批結果發出上千次請求而被防護擋下。

    只保留「採購性質符合」且「依招標方式推估後仍可能入選」的標案（例如目標是最低標時，
    招標方式已明確屬於評選／限制性者不必再查），並依公告日期由新到舊取前 limit 筆。
    """
    candidates = filter_tenders(tenders, target_attr, target_award)
    candidates.sort(key=lambda t: t.get("公告日期", ""), reverse=True)
    if limit and limit > 0:
        return candidates[:limit]
    return candidates


def enrich_actual_award_methods(tenders: list, max_workers: int = 3,
                                progress_cb=None, log=None) -> dict:
    """
    多執行緒連線官方詳細頁校驗真實決標方式。

    站方對連續查詢會回「驗證碼檢核」頁，因此這裡刻意降低併發、每次請求前隨機間隔，
    並在連續 CAPTCHA_STREAK_LIMIT 筆被擋時提前中止（剩餘筆數維持推估值並明確告知）。

    progress_cb(done, total) 為選用回呼；回傳 {"total", "done", "ok", "blocked"}。
    """
    emit = log if callable(log) else (lambda _msg: None)
    stats = {"total": len(tenders), "done": 0, "ok": 0, "blocked": False}
    if not tenders:
        return stats

    total = len(tenders)
    emit(f"[*] 正在連線官方詳細頁校驗決標方式 (共 {total} 筆標案)...")

    counter = {"done": 0, "ok": 0, "streak": 0}
    lock = threading.Lock()
    stop = threading.Event()

    def _bump():
        with lock:
            counter["done"] += 1
            return counter["done"]

    def _fetch_and_update(tender):
        if stop.is_set():
            done = _bump()
            if progress_cb:
                progress_cb(done, total)
            return

        time.sleep(random.uniform(*DETAIL_DELAY_RANGE))
        actual, status = fetch_award_method_status(tender.get("pk", ""))
        if actual:
            apply_award_method(tender, actual)

        with lock:
            counter["done"] += 1
            if actual:
                counter["ok"] += 1
            if status == "blocked":
                counter["streak"] += 1
                if counter["streak"] >= CAPTCHA_STREAK_LIMIT and not stop.is_set():
                    stop.set()
                    emit(f"  [!] 已被網站驗證碼防護擋下（IP 層級冷卻，重開連線階段無效），中止校驗；"
                         f"剩餘 {total - counter['done']} 筆維持「{AWARD_SOURCE_ESTIMATED}」，"
                         f"可稍後再試。")
            else:
                counter["streak"] = 0
            done = counter["done"]

        if progress_cb:
            progress_cb(done, total)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_fetch_and_update, tenders))

    stats.update(done=counter["done"], ok=counter["ok"], blocked=stop.is_set())

    failed = total - counter["ok"]
    if failed and not stop.is_set():
        emit(f"  [!] 有 {failed} 筆標案無法取得詳細頁決標方式，該筆維持依招標方式的推估值。")
    return stats


# ==================== 去重與篩選 ====================

def merge_by_tender_id(unique_tenders: dict, rows: list, keyword: str = ""):
    """
    以標案案號去重（同一標案的原公告與更正公告會是不同列），就地更新 unique_tenders。

    keyword 為空字串時只去重、不累積關鍵字——全面掃描模式改由 tag_keywords 標記。
    """
    for tender in rows:
        tid = tender["標案案號"]
        existing = unique_tenders.get(tid)
        if existing is None:
            tender["命中關鍵字群"] = [keyword] if keyword else []
            unique_tenders[tid] = tender
        elif keyword and keyword not in existing["命中關鍵字群"]:
            existing["命中關鍵字群"].append(keyword)


def tag_keywords(tenders: list, keywords: list) -> list:
    """
    以本地子字串比對標記命中的關鍵字（大小寫不敏感）。

    全面掃描模式下關鍵字只用於標記與快速篩選，沒命中的標案一樣保留——
    「桃園醫院人力資源E指通計畫採購案」正是不含任何預設關鍵字卻完全符合條件的例子。
    """
    normalized = [(k, k.lower()) for k in (keywords or []) if k and k.strip()]
    for tender in tenders:
        name = tender.get("標案名稱", "").lower()
        tender["命中關鍵字群"] = [kw for kw, low in normalized if low in name]
    return tenders


def finalize_keywords(tenders: list):
    """把命中關鍵字群攤平為顯示用字串。"""
    for tender in tenders:
        tender["命中關鍵字"] = ", ".join(tender.get("命中關鍵字群", []))


def filter_tenders(tenders: list, target_attr: str = "勞務", target_award: str = "最低標") -> list:
    """依採購性質與決標方式篩選。兩者皆可傳「不限」。"""
    matched = []
    for tender in tenders:
        attr_ok = target_attr in ("不限", "", None) or target_attr in tender.get("採購性質", "")
        if target_award == "最低標":
            award_ok = tender.get("是否為最低標") == "是"
        elif target_award in ("最有利標/評選", "最有利標", "評選"):
            award = tender.get("決標方式", "")
            award_ok = "最有利標" in award or "評選" in award or "評審" in award
        else:
            award_ok = True
        if attr_ok and award_ok:
            matched.append(tender)
    return matched


# ==================== 報表輸出 ====================

def _ordered_frame(tenders: list):
    df = pd.DataFrame(tenders)
    cols = [c for c in PREFERRED_COLS if c in df.columns]
    return df[cols] if cols else df


def write_excel_report(path: str, all_tenders: list, matched_tenders: list) -> str:
    """輸出雙工作表 Excel（精選 + 全部）。回傳實際寫出的路徑。"""
    if not HAS_PANDAS:
        raise RuntimeError("未安裝 pandas，無法輸出 Excel 報表。")

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        if matched_tenders:
            _ordered_frame(matched_tenders).to_excel(writer, sheet_name="精選_勞務最低標", index=False)
        else:
            pd.DataFrame([{"說明": "本次搜尋無完全符合篩選條件之標案"}]).to_excel(
                writer, sheet_name="精選_勞務最低標", index=False)
        if all_tenders:
            _ordered_frame(all_tenders).to_excel(writer, sheet_name="所有搜尋標案", index=False)
    return path


def write_csv_report(path: str, rows: list) -> str:
    """輸出 CSV（UTF-8 BOM，Excel 可直接開啟）。回傳實際寫出的路徑。"""
    if not rows:
        return ""
    keys = [k for k in rows[0].keys() if k not in INTERNAL_KEYS]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path
