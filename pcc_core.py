# -*- coding: utf-8 -*-
"""
政府電子採購網 (web.pcc.gov.tw) 標案查詢共用核心。

app.py (GUI) 與 crawler.py (CLI) 共用本模組，確保分頁、重試、驗證碼偵測、
表頭欄位對照與決標方式判定等邏輯只有一份實作。
"""

import csv
import json
import math
import os
import random
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from http.cookiejar import CookieJar

import ssl

import pcc_mirror as mirror

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# 政府站憑證在 Python 3.14 + OpenSSL 3.5 嚴格校驗下會因 Missing Subject Key Identifier 被拒
# （Cloud 上 3.14.7 已復現），預設改用 unverified 以避免全量搜尋直接 0 筆。
# 可透過環境變數 PCC_VERIFY_SSL=1 強制開啟驗證（正式環境/除錯用）。
def _build_ssl_context():
    if os.getenv("PCC_VERIFY_SSL", "0") == "1":
        try:
            ctx = ssl.create_default_context()
            if hasattr(ssl, "VERIFY_X509_STRICT"):
                ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            return ctx
        except Exception:
            return None
    try:
        ctx = ssl._create_unverified_context()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx
    except Exception:
        return None


_SSL_CTX = _build_ssl_context()


# ==================== 網站端點與常數 ====================

BASE_URL = "https://web.pcc.gov.tw"
BASIC_SEARCH_URL = BASE_URL + "/prkms/tender/common/basic/readTenderBasic"
BASIC_INDEX_URL = BASE_URL + "/prkms/tender/common/basic/indexTenderBasic"
DETAIL_URL = BASE_URL + "/tps/QueryTender/query/searchTenderDetail"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36",
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

# 決標方式校驗的節流設定。
#
# 官網詳細頁是【額度制而非頻率制】：實測（2026-08）連續請求約 5 筆成功後就會回
# 「驗證碼檢核」頁，把間隔拉到 2 秒也一樣擋，冷卻是分鐘級且為 IP 層級（換 cookie 無效，
# 靜置 15 分鐘也未必解除）。因此主來源改為 pcc_mirror 的公開資料鏡像 API
# （同一批公告、含當日資料、實測約 30–40 筆/分鐘），官網詳細頁降為鏡像查不到時的備援。
# 備援路徑仍維持單執行緒、低額度：撿走免費的那幾筆就收手，被擋不硬啃。
DEFAULT_VERIFY_LIMIT = 60
DETAIL_DELAY_RANGE = (1.0, 2.0)
CAPTCHA_STREAK_LIMIT = 5

# 對政府伺服器的禮貌間隔（秒）
PAGE_DELAY = 0.8

# 決標方式的資料來源，讓使用者一眼看出哪些列只是推估值
AWARD_SOURCE_ESTIMATED = "依招標方式推估"
AWARD_SOURCE_OFFICIAL = "官方詳細頁"
AWARD_SOURCE_MIRROR = "公開資料鏡像"

# 兩種來源都算「已由官方公告確認」——鏡像重新發布的就是同一份公告資料
CONFIRMED_SOURCES = (AWARD_SOURCE_OFFICIAL, AWARD_SOURCE_MIRROR)

# 已確認決標方式的永久快取檔名（放在輸出資料夾，.gitignore 已涵蓋）
AWARD_CACHE_FILENAME = "award_cache.json"

# 快取條目的有效天數。更正公告會改動決標方式，「一次確認、永久相信」遲早會拿舊答案
# 餵給使用者；超過這個天數就當作沒確認過，重新排回待確認佇列。
AWARD_CACHE_TTL_DAYS = 90

# 待確認標案的佇列檔名。背景涓流校驗靠它拿到「還有哪些要查」，
# 不必為了取得清單而重跑一次動輒上百頁的全面掃描。
PENDING_QUEUE_FILENAME = "pending_queue.json"

# GUI 上次使用的搜尋條件（關鍵字、日期模式、採購性質…），下次開啟自動還原
SETTINGS_FILENAME = "ui_settings.json"

# 匯出報表的欄位順序
PREFERRED_COLS = [
    "完全符合目標", "標案名稱", "招標機關", "預算金額", "決標方式", "招標方式",
    "採購性質", "公告日期", "截止投標", "命中關鍵字", "標案案號", "公告類型",
    "決標方式來源", "詳細連結",
]

# 僅供內部流程使用、不應出現在匯出檔中的鍵
INTERNAL_KEYS = ("pk", "命中關鍵字群")

import threading as _threading

_thread_local = _threading.local()


def _build_opener():
    if _SSL_CTX is not None:
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()),
            urllib.request.HTTPSHandler(context=_SSL_CTX),
        )
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


# 全域 opener 保留供舊測試 monkeypatch 相容；實際請求走 get_opener() 的 thread-local 實例
opener = _build_opener()


def get_opener():
    """取得當前執行緒獨立的 opener（避免多執行緒共用 CookieJar 產生競態）。"""
    # 測試時 monkeypatch 會對 opener.open 做替換（寫入實例 __dict__），此時必須回傳被 mock 的全域實例
    try:
        if "open" in getattr(opener, "__dict__", {}):
            return opener
    except Exception:
        pass
    if hasattr(_thread_local, "opener"):
        return _thread_local.opener
    _thread_local.opener = _build_opener()
    return _thread_local.opener


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

def http_post(url: str, data: dict, max_retries: int = 3, timeout: int = 15) -> str:
    """發送 POST 請求，失敗時採退避重試。Cloud 環境縮短 timeout 避免 90s 黑洞。"""
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, data=encoded, headers=HEADERS)
            with get_opener().open(req, timeout=timeout) as resp:
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
            with get_opener().open(req, timeout=timeout) as resp:
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


# 結構性特徵：站方換掉文案時，上面那組中文字串比對會【靜默失效】——
# 我們會把驗證碼頁當成「查無資料」，使用者只看到 0 筆而不知道自己被擋了。
# 驗證碼頁一定帶著一個送往 /tps/validate/ 的表單或 validateCode 欄位，改用這個當後盾。
_VALIDATE_FORM_RE = re.compile(
    r"""<form[^>]+action\s*=\s*["']?[^"'>]*/tps/validate/|"""
    r"""<input[^>]+name\s*=\s*["']?validateCode""",
    re.IGNORECASE)


def is_captcha_page(html_doc: str) -> bool:
    """判斷回應是否為頻率防護的驗證碼頁。"""
    if any(marker in html_doc for marker in CAPTCHA_MARKERS):
        return True
    return bool(_VALIDATE_FORM_RE.search(html_doc))


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


# 表格結構的 token。用深度感知的掃描取代 `<tr[^>]*>(.*?)</tr>`：非貪婪 regex 碰到
# 巢狀 table 時，外層列會被【內層的 </tr> 提早切斷】，剩下的欄位全部錯位或整列被丟掉。
_TABLE_TOKEN_RE = re.compile(r"<\s*(/?)\s*(table|tr|td|th)\b[^>]*>", re.IGNORECASE)


def iter_table_rows(html_doc: str) -> list:
    """
    掃出文件中所有表格列，回傳 [{"cells": [儲存格原始 HTML], "tags": ["td"/"th"]}]。

    以堆疊追蹤 table / tr / td 的巢狀深度，因此：
      * 巢狀表格的列不會把外層列切斷，兩者都能各自完整取得；
      * 儲存格保留【原始 HTML】，pageCode2Img()、<span style="color:red"> 等
        後續要再解析的內容不會在這一步被吃掉；
      * 原始碼漏掉 </tr> / </td> 時同層自動收尾，不會整份解析崩掉。
    列依【開始標籤的出現順序】回傳，與畫面上的順序一致。
    """
    rows, row_stack, cell_stack = [], [], []
    table_depth = 0
    order = 0

    def close_cell(end: int):
        cell = cell_stack.pop()
        if row_stack:
            row_stack[-1]["cells"].append(html_doc[cell["start"]:end])
            row_stack[-1]["tags"].append(cell["tag"])

    def close_row():
        row = row_stack.pop()
        if row["cells"]:
            rows.append(row)

    for match in _TABLE_TOKEN_RE.finditer(html_doc):
        closing = match.group(1) == "/"
        tag = match.group(2).lower()

        if tag == "table":
            if closing:
                while cell_stack and cell_stack[-1]["depth"] >= table_depth:
                    close_cell(match.start())
                while row_stack and row_stack[-1]["depth"] >= table_depth:
                    close_row()
                table_depth = max(0, table_depth - 1)
            else:
                table_depth += 1
        elif tag == "tr":
            if closing:
                while cell_stack and cell_stack[-1]["depth"] >= table_depth:
                    close_cell(match.start())
                if row_stack:
                    close_row()
            else:
                while row_stack and row_stack[-1]["depth"] >= table_depth:
                    while cell_stack and cell_stack[-1]["depth"] >= table_depth:
                        close_cell(match.start())
                    close_row()
                order += 1
                row_stack.append({"cells": [], "tags": [], "depth": table_depth,
                                  "order": order})
        else:  # td / th
            if closing:
                if cell_stack:
                    close_cell(match.start())
            else:
                while cell_stack and cell_stack[-1]["depth"] >= table_depth:
                    close_cell(match.start())
                cell_stack.append({"start": match.end(), "tag": tag,
                                   "depth": table_depth})

    while cell_stack:
        close_cell(len(html_doc))
    while row_stack:
        close_row()

    rows.sort(key=lambda row: row["order"])
    return rows


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
    for row in iter_table_rows(html_doc):
        cells = [html for html, tag in zip(row["cells"], row["tags"]) if tag == "th"]
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

    for table_row in iter_table_rows(html_doc):
        cells = [html for html, tag in zip(table_row["cells"], table_row["tags"])
                 if tag == "td"]
        if len(cells) < required_len:
            continue
        row = " ".join(cells)  # pk 連結必定落在某個儲存格內
        pk_match = re.search(r'pk=([^&"\'>\s]+)', row)
        if not pk_match:
            continue
        pk = pk_match.group(1)

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
    """解析「共有 N 筆資料」；無法解析時回傳 None。含寬鬆後備避免正則失效。"""
    match = re.search(r"共有\s*<span[^>]*>\s*([\d,]+)\s*</span>\s*筆資料", html_doc)
    if not match:
        match = re.search(r"共有\s*([\d,]+)\s*筆資料", html_doc)
    if not match:
        # 寬鬆後備：處理壓縮或編碼差異導致的結構變異
        match = re.search(r"共\s*([\d,]+)\s*筆", html_doc)
    if match:
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            return None
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
               date_type: str = DATE_TYPE_SPDT, progress_cb=None, should_stop=None) -> list:
    """
    搜尋標案並自動走訪所有分頁。keyword 傳空字串即為該條件下的全面掃描。

    log 為選用的單參數回呼（CLI 傳 print，GUI 傳執行緒安全的 log）；
    progress_cb(done_pages, total_pages) 為選用的翻頁進度回呼。

    should_stop 為選用的無參數回呼，回傳 True 即停止翻頁並回傳【已取得的部分結果】——
    全面掃描動輒上百頁要跑好幾分鐘，使用者發現條件設錯時必須能中斷，
    而且已經抓到的資料沒有理由丟掉。
    """
    emit = log if callable(log) else (lambda _msg: None)
    cancelled = should_stop if callable(should_stop) else (lambda: False)

    form = build_search_form(keyword, start_date, end_date, proctrg_cate, date_type)
    emit(f"  [TIMING] 首頁請求開始...")
    t0 = time.time()
    try:
        html_doc = http_post(BASIC_SEARCH_URL, form)
        emit(f"  [TIMING] 首頁請求完成 耗時 {time.time()-t0:.1f}s")
    except Exception as e:
        emit(f"  [!] 搜尋連線失敗 ({keyword}): {e} (耗時 {time.time()-t0:.1f}s)")
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
        if cancelled():
            emit(f"  [!] 已取消翻頁，保留已取得的 {len(results)} 筆"
                 f"（共 {total_pages} 頁中的前 {page_idx - 1} 頁）。")
            break
        time.sleep(polite_delay)
        paged_form = dict(form)
        paged_form[page_param] = str(page_idx)
        emit(f"  [TIMING] 第 {page_idx}/{total_pages} 頁請求開始...")
        t_page = time.time()
        try:
            page_html = http_post(BASIC_SEARCH_URL, paged_form)
            emit(f"  [TIMING] 第 {page_idx} 頁完成 耗時 {time.time()-t_page:.1f}s")
        except Exception as e:
            emit(f"    第 {page_idx} 頁抓取失敗: {e} (耗時 {time.time()-t_page:.1f}s)")
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


def apply_award_method(tender: dict, actual_award: str,
                       source: str = AWARD_SOURCE_OFFICIAL):
    """以公告上的真實決標方式覆蓋推估值，並同步更新衍生欄位。"""
    desc, is_lowest = determine_award_method(tender.get("招標方式", ""), actual_award)
    tender["決標方式"] = desc
    tender["決標方式來源"] = source
    tender["是否為最低標"] = "是" if is_lowest else "否"
    is_service = tender.get("是否為勞務類") == "是"
    tender["完全符合目標"] = "符合 (勞務+最低標)" if (is_service and is_lowest) else "其他"


def is_award_confirmed(tender: dict) -> bool:
    """該筆的決標方式是否已由公告確認（官網詳細頁或公開資料鏡像，而非依招標方式推估）。"""
    return tender.get("決標方式來源") in CONFIRMED_SOURCES


# ==================== 已確認決標方式的永久快取 ====================
#
# 站方每輪只給約 5 次詳細頁額度，單次執行不可能校驗完整批標案，因此把每一筆確認過的
# 結果落地保存：下次執行先套快取，額度就只花在還沒確認過的標案上，確認結果逐次累積。
# key 用「標案案號」而非 pk——案號跨次執行穩定，pk 會隨更正公告改變。

def load_json_dict(path: str) -> dict:
    """讀取 JSON dict；檔案不存在或內容毀損時回空 dict，絕不讓呼叫端因此中斷。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_json_dict(data: dict, path: str) -> bool:
    """
    先寫暫存檔再 os.replace，避免寫到一半被中斷而毀掉既有檔案。回傳是否寫入成功。

    寫檔失敗必須讓呼叫端知道：決標方式的累積策略成敗全繫於這個寫入，
    先前這裡靜默吞掉 OSError，磁碟滿了或資料夾唯讀時使用者會以為一切正常。
    """
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def award_cache_path(output_dir: str) -> str:
    """快取檔的完整路徑。"""
    return os.path.join(output_dir, AWARD_CACHE_FILENAME)


def settings_path(output_dir: str) -> str:
    """GUI 設定檔的完整路徑（與快取同一個資料夾，.gitignore 已涵蓋）。"""
    return os.path.join(output_dir, SETTINGS_FILENAME)


def pending_queue_path(output_dir: str) -> str:
    """待確認佇列檔的完整路徑。"""
    return os.path.join(output_dir, PENDING_QUEUE_FILENAME)


def load_award_cache(path: str) -> dict:
    """讀取已確認決標方式的快取。"""
    return load_json_dict(path)


def save_award_cache(cache: dict, path: str) -> bool:
    """把快取原子性地寫回磁碟；回傳是否寫入成功。"""
    return save_json_dict(cache, path)


def save_pending_queue(tenders: list, path: str) -> bool:
    """
    把還沒確認決標方式的標案落地，供背景涓流校驗取用。

    只存涓流需要的欄位，不是整份搜尋結果的備份——那是 Excel 報表的職責：
    pk 用來連官網詳細頁（備援路徑），案號用來寫回快取，
    公告日期 ＋ 機關 ＋ 名稱是鏡像 API 定位同一案的鑰匙（案號會跨機關重複）。
    """
    queue = {}
    for position, tender in enumerate(tenders):
        tender_id = tender.get("標案案號")
        pk = tender.get("pk")
        if not tender_id or not pk:
            continue
        queue[tender_id] = {
            "pk": pk,
            "標案名稱": tender.get("標案名稱", ""),
            "招標機關": tender.get("招標機關", ""),
            "招標方式": tender.get("招標方式", ""),
            "公告日期": tender.get("公告日期", ""),
            # 呼叫端給的順序就是優先順序（公告日期新→舊）。JSON 以案號排序存放，
            # 不記下來的話取件會變成字母序，先去確認那些早就過了等標期的舊案。
            "order": position,
            "queued_at": date.today().isoformat(),
        }
    return save_json_dict(queue, path)


def load_pending_queue(path: str, cache: dict = None) -> list:
    """
    讀回待確認佇列，並剔除同時期已被快取確認過的案號。

    回傳 enrich/trickle 可直接吃的 dict 清單（含 pk 與案號），
    順序依存檔時記下的優先序——額度稀缺，要先花在還能投標的新案子上。
    """
    queue = load_json_dict(path)
    rows = []
    for tender_id, entry in queue.items():
        if not isinstance(entry, dict):
            continue
        if cache and cached_award(cache.get(tender_id)) is not None:
            continue  # 已經確認過，不必再排隊
        pk = entry.get("pk")
        if not pk:
            continue
        rows.append({
            "pk": pk,
            "標案案號": tender_id,
            "標案名稱": entry.get("標案名稱", ""),
            "招標機關": entry.get("招標機關", ""),
            "招標方式": entry.get("招標方式", ""),
            "採購性質": entry.get("採購性質", ""),
            "公告日期": entry.get("公告日期", ""),
            "決標方式來源": AWARD_SOURCE_ESTIMATED,
            "order": entry.get("order", 0),
        })
    rows.sort(key=lambda row: row["order"])
    return rows


def remember_award(cache: dict, tender: dict, actual_award: str,
                   source: str = AWARD_SOURCE_OFFICIAL):
    """把一筆已確認的決標方式記進快取（就地更新 cache）。"""
    tender_id = tender.get("標案案號")
    if not tender_id or not actual_award:
        return
    cache[tender_id] = {
        "決標方式": actual_award,
        "決標方式來源": source,
        "pk": tender.get("pk", ""),
        "verified_at": date.today().isoformat(),
    }


def _entry_age_days(entry) -> int:
    """快取條目距今幾天；沒有 verified_at（早期版本寫的）時回傳 0 視為仍新鮮。"""
    if not isinstance(entry, dict):
        return 0
    stamp = entry.get("verified_at")
    if not stamp:
        return 0
    try:
        verified = date.fromisoformat(str(stamp))
    except ValueError:
        return 0
    return (date.today() - verified).days


def cached_award(entry, ttl_days: int = AWARD_CACHE_TTL_DAYS):
    """
    取出快取條目中仍在有效期內的決標方式；已過期或無值時回傳 None。

    條目可能是 {"決標方式": ..., "verified_at": ...} 或早期版本的純字串，兩者都要吃。
    """
    if not entry:
        return None
    actual = entry.get("決標方式") if isinstance(entry, dict) else entry
    if not actual:
        return None
    if ttl_days and ttl_days > 0 and _entry_age_days(entry) > ttl_days:
        return None
    return actual


def cached_source(entry) -> str:
    """快取條目的來源標籤。舊版條目沒記來源，一律當成官方詳細頁（當時的唯一來源）。"""
    if isinstance(entry, dict):
        source = entry.get("決標方式來源")
        if source in CONFIRMED_SOURCES:
            return source
    return AWARD_SOURCE_OFFICIAL


def prune_award_cache(cache: dict, ttl_days: int = AWARD_CACHE_TTL_DAYS) -> int:
    """就地刪掉已過期的快取條目，回傳刪除筆數（讓檔案不會無限長大）。"""
    stale = [k for k, v in cache.items() if cached_award(v, ttl_days) is None]
    for key in stale:
        del cache[key]
    return len(stale)


def apply_award_cache(tenders: list, cache: dict,
                      ttl_days: int = AWARD_CACHE_TTL_DAYS) -> int:
    """
    把快取中已確認的決標方式套回標案清單，回傳實際套用筆數。

    超過 ttl_days 的條目視同沒確認過：更正公告會改動決標方式，過期的答案
    比「待確認」更危險——後者至少畫成橘色提醒使用者自己去看。
    """
    if not cache:
        return 0
    applied = 0
    for tender in tenders:
        entry = cache.get(tender.get("標案案號", ""))
        actual = cached_award(entry, ttl_days)
        if not actual:
            continue
        apply_award_method(tender, actual, cached_source(entry))
        applied += 1
    return applied


def select_rows_for_enrichment(tenders: list, target_attr: str = "勞務",
                               target_award: str = "最低標",
                               limit: int = DEFAULT_VERIFY_LIMIT,
                               require_keyword_hit: bool = False) -> list:
    """
    挑出值得連線詳細頁校驗的標案，避免對整批結果發出上千次請求而被防護擋下。

    只保留「採購性質符合」且「依招標方式推估後仍可能入選」的標案（例如目標是最低標時，
    招標方式已明確屬於評選／限制性者不必再查），並依公告日期由新到舊取前 limit 筆。
    require_keyword_hit 與精選清單的定義一致，好讓有限的校驗次數花在使用者真的會看的標案上。
    已由快取確認過的標案會被跳過——額度稀缺，不該花在已知答案上。
    """
    candidates = [t for t in filter_tenders(tenders, target_attr, target_award, require_keyword_hit)
                  if not is_award_confirmed(t)]
    candidates.sort(key=lambda t: t.get("公告日期", ""), reverse=True)
    if limit and limit > 0:
        return candidates[:limit]
    return candidates


def enrich_actual_award_methods(tenders: list, progress_cb=None, log=None,
                                cache: dict = None, cache_path: str = None,
                                should_stop=None, use_mirror: bool = True) -> dict:
    """
    校驗真實決標方式：【先問公開資料鏡像，鏡像查無此案才退回官網詳細頁】。

    官網詳細頁是 IP 層級的額度制（約 5 筆就回驗證碼檢核頁、冷卻分鐘級），
    單靠它一次執行本來就補不完清單。鏡像 API 供的是同一批公告資料、沒有這個額度，
    所以絕大多數標案根本不必碰官網——額度只花在鏡像真的沒有的那幾筆。

    兩條路徑的失敗語意不同，處理方式也不同：
      * 鏡像 blocked（429 限流）—— 下一輪就會有，【不】退回官網，免得白燒官網額度。
      * 鏡像 error（查無此案／缺欄位）—— 重試也一樣，才退回官網詳細頁。
      * 鏡像連續 MIRROR_FAIL_STREAK 筆失敗 —— 判定此刻不可靠，整輪改走官網。

    官網備援路徑維持原本的保守策略：逐筆循序、請求前隨機間隔，
    連續 CAPTCHA_STREAK_LIMIT 筆被擋即中止。刻意不並行：站方限的是額度不是頻率，
    開執行緒只會更快把額度燒完，卻換不到更多資料。

    傳入 cache 與 cache_path 時，每成功一筆就立刻落地保存，確認結果跨次執行累積。

    should_stop 為選用的無參數回呼，回傳 True 即提前收手（使用者按下停止）。

    progress_cb(done, total) 為選用回呼；
    回傳 {"total", "done", "ok", "blocked", "mirror_ok", "official_ok"}。
    """
    emit = log if callable(log) else (lambda _msg: None)
    cancelled = should_stop if callable(should_stop) else (lambda: False)
    total = len(tenders)
    stats = {"total": total, "done": 0, "ok": 0, "blocked": False,
             "mirror_ok": 0, "official_ok": 0}
    if not tenders:
        return stats

    source_desc = "公開資料鏡像（官網詳細頁備援）" if use_mirror else "官網詳細頁"
    emit(f"[*] 正在校驗決標方式 (共 {total} 筆標案，來源：{source_desc})...")

    done = ok = mirror_ok = official_ok = blocked_count = streak = 0
    save_failed = False
    cancelled_early = False

    # {YYYYMMDD: 當日索引}：同一個公告日期只會打一次 listbydate，
    # 一次請求就涵蓋當天全部標案，這是鏡像路徑省請求的關鍵。
    index_cache = {}
    mirror_live = use_mirror
    mirror_fail_streak = 0

    for tender in tenders:
        if cancelled():
            # 使用者主動喊停不是「被站方擋下」，兩者混為一談會讓 GUI 跳出
            # 「官網額度已用盡」這種與事實不符的提示。
            cancelled_early = True
            break

        actual, status, source = "", "", ""

        if mirror_live:
            mirror.polite_delay()
            actual, status = mirror.fetch_award_method_status(tender, index_cache)
            if actual:
                source = AWARD_SOURCE_MIRROR
                mirror_ok += 1
                mirror_fail_streak = 0
            elif status == "error":
                # 只有 error（查無此案）才算「鏡像幫不上忙」。被限流不算：
                # 因為限流而整輪改走官網，等於把暫時性的問題換成永久性的額度損失。
                mirror_fail_streak += 1
                if mirror_fail_streak >= mirror.MIRROR_FAIL_STREAK:
                    mirror_live = False
                    emit(f"  [!] 公開資料鏡像連續 {mirror.MIRROR_FAIL_STREAK} 筆查不到，"
                         f"本輪改用官網詳細頁（額度有限）。")

        # 鏡像被限流是暫時的，下一輪就有；此時退回官網只是白燒那 5 筆額度
        fall_back = not actual and status != "blocked"
        if fall_back:
            time.sleep(random.uniform(*DETAIL_DELAY_RANGE))
            actual, status = fetch_award_method_status(tender.get("pk", ""))
            if actual:
                source = AWARD_SOURCE_OFFICIAL
                official_ok += 1

        done += 1

        if actual:
            apply_award_method(tender, actual, source)
            ok += 1
            if cache is not None:
                remember_award(cache, tender, actual, source)
                if cache_path and not save_award_cache(cache, cache_path) and not save_failed:
                    # 整個累積策略成敗就繫於這個寫入，失敗必須講出來
                    save_failed = True
                    emit(f"  [!] 無法寫入決標方式快取（{cache_path}）——"
                         f"本輪確認的結果不會保留到下次執行，請檢查該資料夾是否可寫入。")

        if status == "blocked":
            blocked_count += 1
            # 官網的額度是連續被擋才算用盡；鏡像限流不該觸發官網那條中止規則
            streak = streak + 1 if fall_back else streak
        elif fall_back:
            streak = 0

        if progress_cb:
            progress_cb(done, total)

        if streak >= CAPTCHA_STREAK_LIMIT:
            stats["blocked"] = True
            emit(f"  [!] 官網詳細頁額度已用盡（連續 {CAPTCHA_STREAK_LIMIT} 筆被驗證碼防護擋下，"
                 f"IP 層級冷卻），停止校驗；剩餘 {total - done} 筆維持"
                 f"「{AWARD_SOURCE_ESTIMATED}」，已確認的都已寫入快取。")
            break

    stats.update(done=done, ok=ok, mirror_ok=mirror_ok, official_ok=official_ok)

    if ok and use_mirror:
        emit(f"  [+] 本輪確認 {ok} 筆（鏡像 {mirror_ok} 筆、官網詳細頁 {official_ok} 筆）。")

    if not stats["blocked"] and not cancelled_early:
        # 被擋下與「公告本來就沒有該欄位」是兩回事：前者稍後重試就能拿到，
        # 後者重試幾次都一樣。混為一談會讓使用者不知道到底該不該再試一次。
        missing = done - ok - blocked_count
        if blocked_count:
            emit(f"  [!] 有 {blocked_count} 筆被限流擋下，維持「{AWARD_SOURCE_ESTIMATED}」，"
                 f"稍後重試即可。")
        if missing > 0:
            emit(f"  [!] 有 {missing} 筆標案的公告沒有決標方式欄位，維持依招標方式的推估值。")
    return stats


# ==================== 背景涓流校驗 ====================
#
# 主來源換成沒有額度限制的公開資料鏡像後，每輪能撿的量不再被卡在 5 筆。
# 以全掃勞務類為例：待確認約 1,098 筆、每天新增約 157 筆；
# 每 15 分鐘一輪、每輪 40 筆，一天 96 輪 ≈ 3,800 筆，待確認清單會實際收斂。
# 間隔維持 15 分鐘：鏡像是志工維運的免費服務，沒有理由把它當成自己的資料庫來刷。

DEFAULT_TRICKLE_INTERVAL_SECONDS = 900
DEFAULT_TRICKLE_BATCH = 40


def trickle_verify(cache_path: str, queue_path: str, batch: int = DEFAULT_TRICKLE_BATCH,
                   log=None, should_stop=None, use_mirror: bool = True) -> dict:
    """
    跑【一輪】背景校驗：從待確認佇列取 batch 筆查公告，成果寫回快取與佇列。

    回傳 {"picked", "ok", "blocked", "remaining"}。呼叫端只要照固定間隔重複呼叫，
    確認結果就會自己累積——不需要使用者做任何事，也不必重跑全面掃描。
    """
    emit = log if callable(log) else (lambda _msg: None)
    cache = load_award_cache(cache_path)
    pending = load_pending_queue(queue_path, cache)
    result = {"picked": 0, "ok": 0, "blocked": False, "remaining": len(pending)}
    if not pending:
        return result

    batch_rows = pending[:max(1, batch)]
    result["picked"] = len(batch_rows)
    stats = enrich_actual_award_methods(batch_rows, log=log, cache=cache,
                                        cache_path=cache_path, should_stop=should_stop,
                                        use_mirror=use_mirror)
    result["ok"] = stats["ok"]
    result["blocked"] = stats["blocked"]

    # 確認過的從佇列移除；沒確認到的留著等下一輪，佇列才不會愈積愈長
    remaining = [row for row in pending if cached_award(cache.get(row["標案案號"])) is None]
    result["remaining"] = len(remaining)
    save_pending_queue(remaining, queue_path)

    if stats["ok"]:
        emit(f"[+] 本輪確認 {stats['ok']} 筆決標方式，待確認尚餘 {len(remaining)} 筆。")
    return result


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


def has_keyword_hit(tender: dict) -> bool:
    """該標案名稱是否命中任何標記關鍵字（finalize_keywords 前後都適用）。"""
    return bool(tender.get("命中關鍵字群") or tender.get("命中關鍵字"))


def filter_tenders(tenders: list, target_attr: str = "勞務", target_award: str = "最低標",
                   require_keyword_hit: bool = False) -> list:
    """
    依採購性質與決標方式篩選；採購性質與決標方式皆可傳「不限」。

    require_keyword_hit 為 True 時再加上「標案名稱命中關鍵字」這個條件——
    全面掃描會撈回整批勞務標案（含午餐、粉刷、校外教學等），精選清單需要這一層才有意義。
    """
    matched = []
    for tender in tenders:
        if require_keyword_hit and not has_keyword_hit(tender):
            continue
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

def report_columns(rows: list) -> list:
    """
    決定報表欄位順序：PREFERRED_COLS 先，其餘實際存在的欄位照首次出現順序接在後面。

    Excel 與 CSV 必須共用這一份，否則同一批資料兩種輸出的欄序會不一樣
    （這正是先前的實際狀況），而且尾巴的欄位會被靜默丟掉。
    """
    seen = []
    for row in rows:
        for key in row:
            if key not in INTERNAL_KEYS and key not in seen:
                seen.append(key)
    preferred = [c for c in PREFERRED_COLS if c in seen]
    return preferred + [c for c in seen if c not in preferred]


def _ordered_frame(tenders: list):
    df = pd.DataFrame(tenders)
    cols = [c for c in report_columns(tenders) if c in df.columns]
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
    """
    輸出 CSV（UTF-8 BOM，Excel 可直接開啟）。回傳實際寫出的路徑。

    欄位順序與 Excel 報表共用 report_columns()，並掃過所有列取欄位聯集——
    先前只看 rows[0] 的鍵，欄序與 Excel 不同，後面列才出現的欄位還會被靜默丟掉。
    """
    if not rows:
        return ""
    keys = report_columns(rows)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore",
                               restval="")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in keys})
    return path
