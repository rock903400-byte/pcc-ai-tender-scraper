# -*- coding: utf-8 -*-
"""
政府電子採購網 (web.pcc.gov.tw) - AI 與資訊勞務最低標標案爬蟲
專門抓取最新標案公告，並精準過濾「勞務類」與「最低標」之標案。
"""

import argparse
import csv
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.cookiejar import CookieJar
from html.parser import HTMLParser

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

from config import (
    DEFAULT_KEYWORDS,
    TARGET_ATTR,
    TARGET_AWARD_WAY,
    DEFAULT_DAYS,
    OUTPUT_DIR,
    EXPORT_EXCEL,
    EXPORT_CSV,
    EXPORT_JSON,
    NOTIFY_CONFIG,
)

# 強制優先使用 IPv4 避免部分政府網站 IPv6 連線超時
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(*args, **kwargs):
    res = _orig_getaddrinfo(*args, **kwargs)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 or res
socket.getaddrinfo = _ipv4_getaddrinfo

# 網站端點定義
BASE_URL = "https://web.pcc.gov.tw"
BASIC_SEARCH_URL = BASE_URL + "/prkms/tender/common/basic/readTenderBasic"
BASIC_INDEX_URL = BASE_URL + "/prkms/tender/common/basic/indexTenderBasic"
ADV_SEARCH_URL = BASE_URL + "/prkms/tender/common/advanced/readTenderAdvanced"
ADV_INDEX_URL = BASE_URL + "/prkms/tender/common/advanced/indexTenderAdvanced"
DETAIL_URL = BASE_URL + "/prkms/urlSelector/common/tpam"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": BASE_URL,
    "Referer": BASIC_INDEX_URL,
}

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def to_roc_date(date_str: str) -> str:
    """西元 YYYY/MM/DD 或 YYYY-MM-DD 轉民國 YYY/MM/DD"""
    date_str = date_str.strip().replace("-", "/")
    parts = date_str.split("/")
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    return f"{y - 1911}/{m:02d}/{d:02d}"


def to_ad_date(date_str: str) -> str:
    """民國 YYY/MM/DD 轉西元 YYYY/MM/DD"""
    parts = date_str.strip().replace("-", "/").split("/")
    if len(parts) == 3 and int(parts[0]) < 1900:
        return f"{int(parts[0]) + 1911}/{int(parts[1]):02d}/{int(parts[2]):02d}"
    return date_str


def http_post(url: str, data: dict, max_retries: int = 3) -> str:
    """發送 POST 請求並處理重試與編碼"""
    encoded_data = urllib.parse.urlencode(data).encode("utf-8")
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, data=encoded_data, headers=HEADERS)
            with opener.open(req, timeout=40) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == max_retries:
                raise e
            time.sleep(1.5 * attempt)
    return ""


def http_get(url: str, params: dict = None, max_retries: int = 3) -> str:
    """發送 GET 請求"""
    full_url = url
    if params:
        full_url += "?" + urllib.parse.urlencode(params)
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(full_url, headers=HEADERS)
            with opener.open(req, timeout=40) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == max_retries:
                raise e
            time.sleep(1.5 * attempt)
    return ""


class TableListParser(HTMLParser):
    """解析標案搜尋結果列表"""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.cur_row = []
        self.cur_cell = []
        self.cur_href = ""
        self.in_tr = False
        self.in_td = False
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self.in_script = True
        elif tag == "tr" and not self.in_script:
            self.in_tr = True
            self.cur_row = []
        elif tag in ("td", "th") and self.in_tr and not self.in_script:
            self.in_td = True
            self.cur_cell = []
            self.cur_href = a.get("href", "")
        elif tag == "a" and self.in_td:
            href = a.get("href", "")
            if href:
                self.cur_href = href

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
        elif tag in ("td", "th") and self.in_td:
            cell_text = "".join(self.cur_cell).strip()
            self.cur_row.append({"text": cell_text, "href": self.cur_href})
            self.cur_href = ""
            self.in_td = False
        elif tag == "tr" and self.in_tr:
            if self.cur_row:
                self.rows.append(self.cur_row)
            self.in_tr = False

    def handle_data(self, data):
        if self.in_td:
            self.cur_cell.append(data)


class DetailParser(HTMLParser):
    """解析標案詳細頁面欄位"""
    def __init__(self):
        super().__init__()
        self.pairs = []
        self.cur_label = None
        self.in_cell = False
        self.cell_class = None
        self.buf = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
        elif tag == "td" and not self.in_script:
            cls = dict(attrs).get("class", "").strip()
            if cls in ("tbg_1", "tbg_4", "tbg_6"):
                self.in_cell = True
                self.cell_class = "label"
                self.buf = []
            elif cls in ("tbg_2", "tbg_4R"):
                self.in_cell = True
                self.cell_class = "value"
                self.buf = []

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
        elif tag == "td" and self.in_cell:
            val = " ".join("".join(self.buf).split())
            if self.cell_class == "label":
                self.cur_label = val
            elif self.cell_class == "value" and self.cur_label:
                self.pairs.append((self.cur_label, val))
                self.cur_label = None
            self.in_cell = False

    def handle_data(self, data):
        if self.in_cell:
            self.buf.append(data)


def parse_total_pages(html_content: str) -> int:
    """從頁面解析總頁數"""
    match = re.search(r"第\s*\d+\s*/\s*(\d+)\s*頁", html_content)
    if match:
        return int(match.group(1))
    match = re.search(r"共\s*(\d+)\s*頁", html_content)
    if match:
        return int(match.group(1))
    return 1


def search_pcc(keyword: str, start_date_roc: str, end_date_roc: str, max_pages: int = 10) -> list:
    """根據關鍵字與日期區間搜尋標案清單"""
    form_data = {
        "pageSize": "50",
        "firstSearch": "true",
        "searchType": "basic",
        "isBinding": "N",
        "isLogIn": "N",
        "orgName": "",
        "orgId": "",
        "tenderName": keyword,
        "tenderId": "",
        "tenderType": "TENDER_DECLARATION",
        "tenderWay": "TENDER_WAY_ALL_DECLARATION",
        "dateType": "isSpdt",
        "tenderStartDate": start_date_roc,
        "tenderEndDate": end_date_roc,
        "radProctrgCate": "RAD_PROCTRG_CATE_3",  # 優先在搜尋端設定勞務
    }

    tenders = []
    try:
        html_doc = http_post(BASIC_SEARCH_URL, form_data)
    except Exception as e:
        print(f"  ❌ 搜尋連線失敗 ({keyword}): {e}")
        return []

    if "圖形驗證碼" in html_doc or "請輸入驗證碼" in html_doc:
        print("  ⚠️ 觸發網站頻率防護驗證碼，冷卻 3 秒後重試...")
        time.sleep(3)
        return []

    total_pages = min(parse_total_pages(html_doc), max_pages)
    print(f"  ↳ 關鍵字【{keyword}】共 {total_pages} 頁結果，開始讀取...")

    for page_idx in range(1, total_pages + 1):
        if page_idx > 1:
            form_data["firstSearch"] = "false"
            form_data["pageIndex"] = str(page_idx)
            try:
                time.sleep(1.0)
                html_doc = http_post(BASIC_SEARCH_URL, form_data)
            except Exception as e:
                print(f"    第 {page_idx} 頁抓取失敗: {e}")
                continue

        parser = TableListParser()
        parser.feed(html_doc)

        for row in parser.rows:
            if len(row) < 5:
                continue
            
            # 取得每列的文字與連結
            row_texts = [cell["text"] for cell in row]
            combined_text = " ".join(row_texts)

            if "項次" in combined_text or "機關名稱" in combined_text:
                continue

            # 提取 pk 連結
            pk = ""
            detail_link = ""
            for cell in row:
                href = cell.get("href", "")
                if "pk=" in href or "tpam" in href:
                    detail_link = BASE_URL + href if href.startswith("/") else href
                    pk_match = re.search(r"pk=([^&]+)", href)
                    if pk_match:
                        pk = pk_match.group(1)
                    break

            try:
                org_name = row[1]["text"] if len(row) > 1 else ""
                tender_id = row[2]["text"] if len(row) > 2 else ""
                tender_name = row[3]["text"] if len(row) > 3 else ""
                tender_way = row[4]["text"] if len(row) > 4 else ""
                proc_type = row[5]["text"] if len(row) > 5 else ""
                publish_date = row[6]["text"] if len(row) > 6 else ""
                deadline = row[7]["text"] if len(row) > 7 else ""
                budget = row[8]["text"] if len(row) > 8 else ""

                if not tender_name or not tender_id:
                    continue

                tenders.append({
                    "pk": pk,
                    "標案案號": tender_id.strip(),
                    "標案名稱": tender_name.strip(),
                    "招標機關": org_name.strip(),
                    "招標方式": tender_way.strip(),
                    "採購性質": proc_type.strip(),
                    "公告日期": to_ad_date(publish_date),
                    "截止投標": deadline.strip(),
                    "預算金額": budget.strip(),
                    "詳細連結": detail_link,
                    "搜尋關鍵字": keyword,
                })
            except Exception:
                continue

    return tenders


def fetch_tender_detail(pk: str, detail_url: str = "") -> dict:
    """抓取單一標案之詳細頁面，取得決標方式與完整欄位"""
    url = detail_url if detail_url else f"{DETAIL_URL}?pk={pk}"
    fields = {}
    try:
        html_doc = http_get(url)
        parser = DetailParser()
        parser.feed(html_doc)
        for label, val in parser.pairs:
            cleaned_label = re.sub(r"\s+", "", label).rstrip(":")
            fields[cleaned_label] = val
    except Exception as e:
        fields["錯誤"] = str(e)
    return fields


def evaluate_filter(tender: dict, target_attr: str, target_award_way: str) -> tuple:
    """評估標案是否符合 採購性質(勞務) 與 決標方式(最低標)"""
    attr_val = tender.get("採購性質", "")
    award_val = tender.get("決標方式", "")
    full_text = f"{tender.get('標案名稱', '')} {attr_val} {award_val} {tender.get('附加說明', '')}"

    # 1. 勞務類別檢驗
    is_service = False
    if target_attr:
        if target_attr in attr_val or target_attr in full_text:
            is_service = True
    else:
        is_service = True

    # 2. 最低標檢驗
    is_lowest_price = False
    if target_award_way:
        if target_award_way in award_val or target_award_way in full_text:
            is_lowest_price = True
        elif "最低標" in award_val:
            is_lowest_price = True
    else:
        is_lowest_price = True

    is_matched = is_service and is_lowest_price
    return is_matched, is_service, is_lowest_price


def run_crawler(keywords: list, days: int, target_attr: str, target_award_way: str, fetch_details: bool = True):
    """執行爬蟲主流程"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    
    start_roc = to_roc_date(start_date.strftime("%Y/%m/%d"))
    end_roc = to_roc_date(end_date.strftime("%Y/%m/%d"))

    print("=" * 60)
    print("🚀 政府電子採購網 (PCC) AI/資訊 勞務最低標爬蟲")
    print(f"📅 搜尋區間: 民國 {start_roc} ~ {end_roc} (最近 {days} 天)")
    print(f"🎯 篩選條件: 採購性質=【{target_attr or '不限'}】 | 決標方式=【{target_award_way or '不限'}】")
    print(f"🔑 搜尋關鍵字 ({len(keywords)} 組): {', '.join(keywords)}")
    print("=" * 60)

    # 1. 遍歷關鍵字抓取清單並去重
    unique_tenders = {}
    for kw in keywords:
        print(f"\n🔍 正在搜尋關鍵字：【{kw}】...")
        results = search_pcc(kw, start_roc, end_roc)
        for t in results:
            tid = t["標案案號"]
            if tid not in unique_tenders:
                t["命中關鍵字群"] = [kw]
                unique_tenders[tid] = t
            else:
                if kw not in unique_tenders[tid]["命中關鍵字群"]:
                    unique_tenders[tid]["命中關鍵字群"].append(kw)
        time.sleep(1.2)

    total_found = len(unique_tenders)
    print(f"\n📊 關鍵字搜尋完畢！共取得 {total_found} 筆不重複標案。")

    if total_found == 0:
        print("ℹ️ 本次搜尋區間內未發現任何標案。")
        return

    # 2. 逐筆抓取詳情以確認「決標方式」與更多規格
    tenders_list = list(unique_tenders.values())
    if fetch_details:
        print(f"\n🔎 正在解析標案詳細規格 (確認是否為『{target_award_way}』)...")
        for idx, t in enumerate(tenders_list, start=1):
            pk = t.get("pk")
            sys.stdout.write(f"\r  [{idx}/{total_found}] 正在獲取: {t['標案名稱'][:22]}...")
            sys.stdout.flush()

            if pk or t.get("詳細連結"):
                detail_info = fetch_tender_detail(pk, t.get("詳細連結"))
                if detail_info:
                    t["決標方式"] = detail_info.get("決標方式", t.get("決標方式", "未標明"))
                    if "採購性質" in detail_info:
                        t["採購性質"] = detail_info["採購性質"]
                    if "預算金額" in detail_info:
                        t["預算金額"] = detail_info["預算金額"]
                    t["開標時間"] = detail_info.get("開標時間", "")
                    t["履約地點"] = detail_info.get("履約地點", "")
                    t["聯絡人"] = detail_info.get("聯絡人", "")
                    t["聯絡電話"] = detail_info.get("聯絡電話", "")

            # 評估條件
            is_matched, is_service, is_lowest = evaluate_filter(t, target_attr, target_award_way)
            t["是否為勞務類"] = "是" if is_service else "否"
            t["是否為最低標"] = "是" if is_lowest else "否"
            t["完全符合目標"] = "✅ 符合" if is_matched else "❌ 不符"
            t["命中關鍵字"] = ", ".join(t["命中關鍵字群"])
            time.sleep(0.6)  # 禮貌爬蟲間隔
        print("\n  ✅ 詳細規格解析完成！")
    else:
        for t in tenders_list:
            is_matched, is_service, is_lowest = evaluate_filter(t, target_attr, target_award_way)
            t["是否為勞務類"] = "是" if is_service else "否"
            t["是否為最低標"] = "是" if is_lowest else "未知(未抓詳情)"
            t["完全符合目標"] = "待確認"
            t["命中關鍵字"] = ", ".join(t["命中關鍵字群"])

    # 3. 統計與排序
    matched_tenders = [t for t in tenders_list if t.get("完全符合目標") == "✅ 符合"]
    
    print("\n" + "=" * 60)
    print("📈 執行成果摘要")
    print(f"  • 全部搜尋到標案: {len(tenders_list)} 筆")
    print(f"  • 完全符合【{target_attr} + {target_award_way}】: {len(matched_tenders)} 筆")
    print("=" * 60)

    # 4. 匯出成果
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_filename = f"pcc_tenders_{timestamp}"

    # 輸出 Excel
    if EXPORT_EXCEL and HAS_PANDAS:
        excel_path = os.path.join(OUTPUT_DIR, f"{base_filename}.xlsx")
        df_all = pd.DataFrame(tenders_list)
        df_matched = pd.DataFrame(matched_tenders)
        
        # 調整欄位顯示順序
        preferred_cols = [
            "完全符合目標", "標案名稱", "招標機關", "預算金額", "決標方式", "採購性質",
            "公告日期", "截止投標", "開標時間", "命中關鍵字", "標案案號", "招標方式",
            "履約地點", "聯絡人", "聯絡電話", "詳細連結"
        ]
        cols_all = [c for c in preferred_cols if c in df_all.columns] + [c for c in df_all.columns if c not in preferred_cols and c not in ("命中關鍵字群", "pk")]
        df_all = df_all[cols_all] if not df_all.empty else df_all
        
        if not df_matched.empty:
            df_matched = df_matched[[c for c in preferred_cols if c in df_matched.columns]]

        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            if not df_matched.empty:
                df_matched.to_excel(writer, sheet_name="精選_勞務最低標", index=False)
            else:
                pd.DataFrame([{"說明": "本次搜尋無完全符合勞務+最低標之標案"}]).to_excel(writer, sheet_name="精選_勞務最低標", index=False)
            df_all.to_excel(writer, sheet_name="所有搜尋標案", index=False)
        print(f"💾 Excel 報表已輸出: {os.path.abspath(excel_path)}")

    # 輸出 CSV
    if EXPORT_CSV:
        csv_path = os.path.join(OUTPUT_DIR, f"{base_filename}_勞務最低標.csv")
        export_data = matched_tenders if matched_tenders else tenders_list
        if export_data:
            keys = [k for k in export_data[0].keys() if k not in ("命中關鍵字群", "pk")]
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(export_data)
            print(f"💾 CSV 檔案已輸出: {os.path.abspath(csv_path)}")

    # 輸出 JSON
    if EXPORT_JSON:
        json_path = os.path.join(OUTPUT_DIR, f"{base_filename}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"matched": matched_tenders, "all": tenders_list}, f, ensure_ascii=False, indent=2)
        print(f"💾 JSON 資料已輸出: {os.path.abspath(json_path)}")

    print("\n🎉 標案爬取與篩選作業順利完成！\n")


def main():
    parser = argparse.ArgumentParser(
        description="政府電子採購網 (web.pcc.gov.tw) - AI 與資訊勞務最低標標案爬蟲"
    )
    parser.add_argument("--days", "-d", type=int, default=DEFAULT_DAYS, help=f"查詢天數 (預設: {DEFAULT_DAYS} 天)")
    parser.add_argument("--keywords", "-k", nargs="+", default=DEFAULT_KEYWORDS, help="自訂搜尋關鍵字清單")
    parser.add_argument("--attr", type=str, default=TARGET_ATTR, help="採購性質 (預設: 勞務)")
    parser.add_argument("--award-way", type=str, default=TARGET_AWARD_WAY, help="決標方式 (預設: 最低標)")
    parser.add_argument("--no-detail", action="store_true", help="不抓取詳細頁面 (僅根據列表篩選，速度較快)")

    args = parser.parse_args()
    run_crawler(
        keywords=args.keywords,
        days=args.days,
        target_attr=args.attr,
        target_award_way=args.award_way,
        fetch_details=not args.no_detail,
    )


if __name__ == "__main__":
    main()
