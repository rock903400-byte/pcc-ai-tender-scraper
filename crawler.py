# -*- coding: utf-8 -*-
"""
政府電子採購網 (web.pcc.gov.tw) - AI 與資訊勞務最低標標案爬蟲 (CLI)

抓取最新標案公告，並即時檢索官方詳細頁精準判定「勞務類」與「最低標」
（精確區分參考最有利標與最低標）。核心邏輯共用自 pcc_core。
"""

import argparse
import os
import re
import time
from datetime import datetime, timedelta

import pcc_core as core
from config import (
    DEFAULT_KEYWORDS,
    TARGET_ATTR,
    TARGET_AWARD_WAY,
    DEFAULT_DAYS,
    OUTPUT_DIR,
    EXPORT_EXCEL,
    EXPORT_CSV,
    EXPORT_JSON,
)

core.enable_utf8_console()
core.install_ipv4_preference()


def run_crawler(keywords: list, days: int, target_attr: str, target_award_way: str):
    """執行爬蟲主流程"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    start_roc = core.to_roc_date(start_date.strftime("%Y/%m/%d"))
    end_roc = core.to_roc_date(end_date.strftime("%Y/%m/%d"))
    proctrg_cate = core.PROCTRG_CATE.get(target_attr)

    print("=" * 65)
    print("[*] 政府電子採購網 (PCC) AI/資訊 勞務最低標標案爬蟲")
    print(f"[*] 搜尋區間: 民國 {start_roc} ~ {end_roc} (最近 {days} 天)")
    print(f"[*] 篩選條件: 採購性質=【{target_attr}】 | 決標方式=【{target_award_way}】")
    print(f"[*] 搜尋關鍵字 ({len(keywords)} 組): {', '.join(keywords)}")
    print("=" * 65)

    unique_tenders = {}
    for kw in keywords:
        print(f"\n[SEARCH] 正在搜尋關鍵字：【{kw}】...")
        results = core.search_pcc(kw, start_roc, end_roc, proctrg_cate=proctrg_cate, log=print)
        core.merge_by_tender_id(unique_tenders, results, kw)

    tenders_list = list(unique_tenders.values())
    core.finalize_keywords(tenders_list)

    print(f"\n[INFO] 關鍵字搜尋完畢！共取得 {len(tenders_list)} 筆不重複標案。")
    if not tenders_list:
        print("[INFO] 本次搜尋區間內未發現任何標案。")
        return

    # 執行真實決標方式深度校驗
    core.enrich_actual_award_methods(tenders_list, log=print)

    matched_tenders = core.filter_tenders(tenders_list, target_attr, target_award_way)

    print("\n" + "=" * 65)
    print("[SUMMARY] 執行成果摘要")
    print(f"  • 全部搜尋到標案: {len(tenders_list)} 筆")
    print(f"  • 完全符合【{target_attr} + {target_award_way}】: {len(matched_tenders)} 筆")
    print("=" * 65)

    if matched_tenders:
        print(f"\n🏆 精選【{target_attr} + {target_award_way}】標案清單：")
        for i, m in enumerate(matched_tenders, 1):
            print(f"  {i}. [{m['公告日期']}] {m['招標機關']} - {m['標案名稱']}")
            print(f"     案號: {m['標案案號']} | 預算: {m['預算金額']} | 截止投標: {m['截止投標']}")
            print(f"     招標方式: {m['招標方式']} | 決標方式: {m['決標方式']}")
            print(f"     連結: {m['詳細連結']}\n")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_filename = f"pcc_tenders_{timestamp}"

    if EXPORT_EXCEL:
        if core.HAS_PANDAS:
            excel_path = os.path.join(OUTPUT_DIR, f"{base_filename}.xlsx")
            core.write_excel_report(excel_path, tenders_list, matched_tenders)
            print(f"[SAVE] Excel 報表已輸出: {os.path.abspath(excel_path)}")
        else:
            print("[WARN] 未安裝 pandas，略過 Excel 匯出。")

    if EXPORT_CSV:
        # 決標方式可能含「/」等不能用於檔名的字元
        suffix = re.sub(r"[^\w一-鿿]+", "_", f"{target_attr}{target_award_way}").strip("_")
        csv_path = os.path.join(OUTPUT_DIR, f"{base_filename}_{suffix}.csv")
        written = core.write_csv_report(csv_path, matched_tenders or tenders_list)
        if written:
            print(f"[SAVE] CSV 檔案已輸出: {os.path.abspath(written)}")

    if EXPORT_JSON:
        import json
        json_path = os.path.join(OUTPUT_DIR, f"{base_filename}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"matched": matched_tenders, "all": tenders_list},
                      f, ensure_ascii=False, indent=2)
        print(f"[SAVE] JSON 資料已輸出: {os.path.abspath(json_path)}")

    print("\n[DONE] 標案爬取與篩選作業順利完成！\n")


def main():
    parser = argparse.ArgumentParser(
        description="政府電子採購網 (web.pcc.gov.tw) - AI 與資訊勞務最低標標案爬蟲"
    )
    parser.add_argument("--days", "-d", type=int, default=DEFAULT_DAYS,
                        help=f"查詢天數 (預設: {DEFAULT_DAYS} 天)")
    parser.add_argument("--keywords", "-k", nargs="+", default=DEFAULT_KEYWORDS,
                        help="自訂搜尋關鍵字清單")
    parser.add_argument("--attr", type=str, default=TARGET_ATTR,
                        choices=["勞務", "財物", "工程", "不限"],
                        help="採購性質 (預設: 勞務)")
    parser.add_argument("--award-way", type=str, default=TARGET_AWARD_WAY,
                        choices=["最低標", "最有利標/評選", "不限"],
                        help="決標方式 (預設: 最低標)")

    args = parser.parse_args()
    run_crawler(
        keywords=args.keywords,
        days=args.days,
        target_attr=args.attr,
        target_award_way=args.award_way,
    )


if __name__ == "__main__":
    main()
