# -*- coding: utf-8 -*-
"""
政府電子採購網 (web.pcc.gov.tw) - AI 與資訊勞務最低標標案爬蟲
專門抓取最新標案公告，並即時檢索官方詳細頁精準判定「勞務類」與「最低標」（精確區分參考最有利標與最低標）。
"""

import argparse
import concurrent.futures
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

# 支援 Windows 終端機 UTF-8 輸出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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

# 強制優先使用 IPv4 避免 Windows 下連線政府伺服器超時
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(*args, **kwargs):
    res = _orig_getaddrinfo(*args, **kwargs)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 or res
socket.getaddrinfo = _ipv4_getaddrinfo

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
            with opener.open(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == max_retries:
                raise e
            time.sleep(1.5 * attempt)
    return ""


def parse_total_pages(html_content: str) -> int:
    """從頁面解析總頁數"""
    match = re.search(r"第\s*\d+\s*/\s*(\d+)\s*頁", html_content)
    if match:
        return int(match.group(1))
    match = re.search(r"共\s*(\d+)\s*頁", html_content)
    if match:
        return int(match.group(1))
    return 1


def fetch_actual_award_method(pk: str) -> str:
    """向官方詳細頁發送請求，精準萃取真實『決標方式』"""
    if not pk:
        return ""
    url = f"{DETAIL_URL}?pkPmsMain={pk}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with opener.open(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        
        match = re.search(r'決標方式\s*</t[hd]>\s*<td[^>]*>(.*?)</td>', html, re.DOTALL)
        if match:
            val = " ".join(re.sub(r'<[^>]+>', '', match.group(1)).split())
            if val:
                return val

        match2 = re.search(r'決標方式.*?</td>\s*<td[^>]*>(.*?)</td>', html, re.DOTALL)
        if match2:
            val = " ".join(re.sub(r'<[^>]+>', '', match2.group(1)).split())
            if val:
                return val
    except Exception:
        pass
    return ""


def determine_award_method(tender_way: str, actual_award_str: str = "") -> tuple:
    """
    精確判定決標方式（最低標 vs 參考最有利標/最有利標/評選）
    1. 若已有詳細頁中的決標方式欄位，以真實欄位為最高準則！
    2. 否則安全依據招標方式推估
    """
    if actual_award_str:
        s = actual_award_str.strip()
        if "參考最有利標" in s or "最有利標" in s or "評審" in s or "評選" in s:
            return s, False
        elif "最低標" in s:
            return s, True
        return s, ("最低標" in s)

    tender_way = tender_way.strip()
    if "評選" in tender_way or "最有利標" in tender_way or "評審" in tender_way:
        return "最有利標 / 評選", False
    elif "公開取得" in tender_way:
        return "公開取得 (待確認)", True
    elif "公開招標" in tender_way:
        return "最低標 (公開招標)", True
    elif "選擇性招標" in tender_way:
        return "最低標 (選擇性招標)", True
    elif "限制性招標" in tender_way:
        return "限制性招標", False

    return tender_way or "未標明", ("最低標" in tender_way)


def parse_tender_rows(html_doc: str, keyword: str) -> list:
    """精準解析採購網搜尋結果表格列"""
    tenders = []
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html_doc, re.DOTALL)

    for r in rows:
        pk_match = re.search(r'pk=([^&"\'>\s]+)', r)
        if not pk_match:
            continue
        pk = pk_match.group(1)
        detail_link = f"{BASE_URL}/prkms/urlSelector/common/tpam?pk={pk}"

        tender_name = ""
        img_match = re.findall(r'pageCode2Img\(["\'](.*?)["\']\)', r)
        if img_match:
            tender_name = img_match[0].strip()

        cells = re.findall(r'<td[^>]*>(.*?)</td>', r, re.DOTALL)
        cleaned_cells = []
        for c in cells:
            no_script = re.sub(r'<script.*?</script>', '', c, flags=re.DOTALL)
            text = " ".join(re.sub(r'<[^>]+>', '', no_script).split())
            cleaned_cells.append(text)

        if len(cleaned_cells) < 8:
            continue

        org_name = cleaned_cells[1]
        tender_id = cleaned_cells[2]
        tender_way = cleaned_cells[4] if len(cleaned_cells) > 4 else ""
        proc_type = cleaned_cells[5] if len(cleaned_cells) > 5 else ""
        pub_date = cleaned_cells[6] if len(cleaned_cells) > 6 else ""
        deadline = cleaned_cells[7] if len(cleaned_cells) > 7 else ""
        budget = cleaned_cells[8] if len(cleaned_cells) > 8 else ""

        if not tender_name and len(cleaned_cells) > 3:
            tender_name = cleaned_cells[3]

        if not tender_id or not org_name:
            continue

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
            "預算金額": budget + " 元" if budget and not budget.endswith("元") else budget,
            "公告日期": to_ad_date(pub_date),
            "截止投標": deadline,
            "是否為勞務類": "是" if is_service else "否",
            "是否為最低標": "是" if is_lowest else "否",
            "完全符合目標": "符合 (勞務+最低標)" if (is_service and is_lowest) else "其他",
            "詳細連結": detail_link,
            "搜尋關鍵字": keyword,
        })

    return tenders


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
        "tenderWay": "",
        "dateType": "isSpdt",
        "tenderStartDate": start_date_roc,
        "tenderEndDate": end_date_roc,
        "radProctrgCate": "RAD_PROCTRG_CATE_3",
    }

    try:
        html_doc = http_post(BASIC_SEARCH_URL, form_data)
    except Exception as e:
        print(f"  [!] 搜尋連線失敗 ({keyword}): {e}")
        return []

    if "圖形驗證碼" in html_doc or "請輸入驗證碼" in html_doc:
        print("  [!] 觸發網站頻率防護驗證碼，冷卻 2 秒...")
        time.sleep(2)
        return []

    total_pages = min(parse_total_pages(html_doc), max_pages)
    print(f"  [+] 關鍵字【{keyword}】共 {total_pages} 頁結果，開始讀取...")

    all_tenders = []
    all_tenders.extend(parse_tender_rows(html_doc, keyword))

    for page_idx in range(2, total_pages + 1):
        form_data["firstSearch"] = "false"
        form_data["pageIndex"] = str(page_idx)
        try:
            time.sleep(0.8)
            html_p = http_post(BASIC_SEARCH_URL, form_data)
            all_tenders.extend(parse_tender_rows(html_p, keyword))
        except Exception as e:
            print(f"    第 {page_idx} 頁抓取失敗: {e}")
            continue

    return all_tenders


def enrich_actual_award_methods(tenders: list, max_workers: int = 6):
    """使用多執行緒平行連線官方詳細頁，精準確認真實決標方式"""
    if not tenders:
        return
    
    print(f"[*] 正在連線官方詳細頁精準校驗決標方式 (共 {len(tenders)} 筆標案)...")
    
    def _fetch_and_update(t):
        pk = t.get("pk")
        if not pk:
            return
        actual_award = fetch_actual_award_method(pk)
        if actual_award:
            award_desc, is_lowest = determine_award_method(t.get("招標方式", ""), actual_award)
            t["決標方式"] = award_desc
            t["是否為最低標"] = "是" if is_lowest else "否"
            is_service = (t.get("是否為勞務類") == "是")
            t["完全符合目標"] = "符合 (勞務+最低標)" if (is_service and is_lowest) else "其他"

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_fetch_and_update, tenders))


def run_crawler(keywords: list, days: int, target_attr: str, target_award_way: str):
    """執行爬蟲主流程"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    
    start_roc = to_roc_date(start_date.strftime("%Y/%m/%d"))
    end_roc = to_roc_date(end_date.strftime("%Y/%m/%d"))

    print("=" * 65)
    print("[*] 政府電子採購網 (PCC) AI/資訊 勞務最低標標案爬蟲")
    print(f"[*] 搜尋區間: 民國 {start_roc} ~ {end_roc} (最近 {days} 天)")
    print(f"[*] 篩選條件: 採購性質=【{target_attr}】 | 決標方式=【{target_award_way}】")
    print(f"[*] 搜尋關鍵字 ({len(keywords)} 組): {', '.join(keywords)}")
    print("=" * 65)

    unique_tenders = {}
    for kw in keywords:
        print(f"\n[SEARCH] 正在搜尋關鍵字：【{kw}】...")
        results = search_pcc(kw, start_roc, end_roc)
        for t in results:
            tid = t["標案案號"]
            if tid not in unique_tenders:
                t["命中關鍵字群"] = [kw]
                unique_tenders[tid] = t
            else:
                if kw not in unique_tenders[tid]["命中關鍵字群"]:
                    unique_tenders[tid]["命中關鍵字群"].append(kw)
        time.sleep(0.6)

    tenders_list = list(unique_tenders.values())
    for t in tenders_list:
        t["命中關鍵字"] = ", ".join(t.get("命中關鍵字群", []))

    total_found = len(tenders_list)
    print(f"\n[INFO] 關鍵字搜尋完畢！共取得 {total_found} 筆不重複標案。")

    if total_found == 0:
        print("[INFO] 本次搜尋區間內未發現任何標案。")
        return

    # 執行真實決標方式深度校驗
    enrich_actual_award_methods(tenders_list)

    matched_tenders = [
        t for t in tenders_list
        if t.get("是否為勞務類") == "是" and t.get("是否為最低標") == "是"
    ]

    print("\n" + "=" * 65)
    print("[SUMMARY] 執行成果摘要")
    print(f"  • 全部搜尋到標案: {len(tenders_list)} 筆")
    print(f"  • 完全符合【{target_attr} + {target_award_way}】: {len(matched_tenders)} 筆")
    print("=" * 65)

    if matched_tenders:
        print("\n🏆 精選【勞務 + 最低標】標案清單：")
        for i, m in enumerate(matched_tenders, 1):
            print(f"  {i}. [{m['公告日期']}] {m['招標機關']} - {m['標案名稱']}")
            print(f"     案號: {m['標案案號']} | 預算: {m['預算金額']} | 截止投標: {m['截止投標']}")
            print(f"     招標方式: {m['招標方式']} | 決標方式: {m['決標方式']}")
            print(f"     連結: {m['詳細連結']}\n")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_filename = f"pcc_tenders_{timestamp}"

    if EXPORT_EXCEL and HAS_PANDAS:
        excel_path = os.path.join(OUTPUT_DIR, f"{base_filename}.xlsx")
        df_all = pd.DataFrame(tenders_list)
        df_matched = pd.DataFrame(matched_tenders)
        
        preferred_cols = [
            "完全符合目標", "標案名稱", "招標機關", "預算金額", "決標方式", "招標方式",
            "採購性質", "公告日期", "截止投標", "命中關鍵字", "標案案號", "詳細連結"
        ]
        
        cols_all = [c for c in preferred_cols if c in df_all.columns]
        df_all = df_all[cols_all] if not df_all.empty else df_all
        
        if not df_matched.empty:
            df_matched = df_matched[[c for c in preferred_cols if c in df_matched.columns]]

        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            if not df_matched.empty:
                df_matched.to_excel(writer, sheet_name="精選_勞務最低標", index=False)
            else:
                pd.DataFrame([{"說明": "本次搜尋無完全符合勞務+最低標之標案"}]).to_excel(writer, sheet_name="精選_勞務最低標", index=False)
            df_all.to_excel(writer, sheet_name="所有搜尋標案", index=False)
        print(f"[SAVE] Excel 報表已輸出: {os.path.abspath(excel_path)}")

    if EXPORT_CSV:
        csv_path = os.path.join(OUTPUT_DIR, f"{base_filename}_勞務最低標.csv")
        export_data = matched_tenders if matched_tenders else tenders_list
        if export_data:
            keys = [k for k in export_data[0].keys() if k not in ("命中關鍵字群", "pk")]
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(export_data)
            print(f"[SAVE] CSV 檔案已輸出: {os.path.abspath(csv_path)}")

    if EXPORT_JSON:
        json_path = os.path.join(OUTPUT_DIR, f"{base_filename}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"matched": matched_tenders, "all": tenders_list}, f, ensure_ascii=False, indent=2)
        print(f"[SAVE] JSON 資料已輸出: {os.path.abspath(json_path)}")

    print("\n[DONE] 標案爬取與篩選作業順利完成！\n")


def main():
    parser = argparse.ArgumentParser(
        description="政府電子採購網 (web.pcc.gov.tw) - AI 與資訊勞務最低標標案爬蟲"
    )
    parser.add_argument("--days", "-d", type=int, default=DEFAULT_DAYS, help=f"查詢天數 (預設: {DEFAULT_DAYS} 天)")
    parser.add_argument("--keywords", "-k", nargs="+", default=DEFAULT_KEYWORDS, help="自訂搜尋關鍵字清單")
    parser.add_argument("--attr", type=str, default=TARGET_ATTR, help="採購性質 (預設: 勞務)")
    parser.add_argument("--award-way", type=str, default=TARGET_AWARD_WAY, help="決標方式 (預設: 最低標)")

    args = parser.parse_args()
    run_crawler(
        keywords=args.keywords,
        days=args.days,
        target_attr=args.attr,
        target_award_way=args.award_way,
    )


if __name__ == "__main__":
    main()
