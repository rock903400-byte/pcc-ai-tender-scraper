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


def run_crawler(keywords: list, days: int, target_attr: str, target_award_way: str,
                date_mode: str = core.DATE_MODE_SPDT, verify: bool = True,
                verify_limit: int = core.DEFAULT_VERIFY_LIMIT,
                only_keyword_hits: bool = False):
    """執行爬蟲主流程（全面掃描該條件下的標案，關鍵字僅用於標記）"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    date_type = core.DATE_MODES.get(date_mode, core.DATE_TYPE_SPDT)
    if date_type == core.DATE_TYPE_RANGE:
        days = core.clamp_date_range_days(days, log=print)

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    start_ad = start_date.strftime("%Y/%m/%d")
    end_ad = end_date.strftime("%Y/%m/%d")
    proctrg_cate = core.PROCTRG_CATE.get(target_attr)

    print("=" * 65)
    print("[*] 政府電子採購網 (PCC) AI/資訊 勞務最低標標案爬蟲")
    if date_type == core.DATE_TYPE_RANGE:
        print(f"[*] 搜尋區間: 公告日期 {start_ad} ~ {end_ad} (最近 {days} 天)")
    else:
        print("[*] 搜尋區間: 等標期內（現正招標中，站方會忽略日期區間）")
    print(f"[*] 篩選條件: 採購性質=【{target_attr}】 | 決標方式=【{target_award_way}】")
    print(f"[*] 標記關鍵字 ({len(keywords)} 組): {', '.join(keywords)}")
    print("=" * 65)

    print("\n[SEARCH] 正在全面掃描標案...")
    rows = core.search_pcc("", start_ad, end_ad, proctrg_cate=proctrg_cate,
                           date_type=date_type, log=print)

    unique_tenders = {}
    core.merge_by_tender_id(unique_tenders, rows, "")
    tenders_list = list(unique_tenders.values())
    core.tag_keywords(tenders_list, keywords)

    hits = sum(1 for t in tenders_list if t.get("命中關鍵字群"))
    print(f"\n[INFO] 掃描完畢！共取得 {len(tenders_list)} 筆不重複標案"
          f"（其中 {hits} 筆命中關鍵字）。")

    if only_keyword_hits:
        tenders_list = [t for t in tenders_list if t.get("命中關鍵字群")]
        print(f"[INFO] 已套用 --only-keyword-hits，保留 {len(tenders_list)} 筆命中關鍵字的標案。")

    core.finalize_keywords(tenders_list)

    if not tenders_list:
        print("[INFO] 本次搜尋條件下未發現任何標案。")
        return

    # 只校驗有機會入選的標案，避免上千次請求被站方驗證碼防護擋下
    if verify:
        targets = core.select_rows_for_enrichment(tenders_list, target_attr, target_award_way,
                                                  limit=verify_limit)
        if targets:
            print(f"\n[VERIFY] 從 {len(tenders_list)} 筆中挑出 {len(targets)} 筆候選，"
                  f"連線官方詳細頁校驗決標方式...")
            stats = core.enrich_actual_award_methods(targets, log=print)
            print(f"[VERIFY] {stats['ok']}/{stats['total']} 筆取得官方決標方式，"
                  f"其餘維持「{core.AWARD_SOURCE_ESTIMATED}」。")
    else:
        print(f"\n[VERIFY] 已略過深度校驗，決標方式全部為「{core.AWARD_SOURCE_ESTIMATED}」。")

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
                        help="自訂標記關鍵字清單 (僅用於標記命中，不影響抓取範圍)")
    parser.add_argument("--attr", type=str, default=TARGET_ATTR,
                        choices=["勞務", "財物", "工程", "不限"],
                        help="採購性質 (預設: 勞務)")
    parser.add_argument("--award-way", type=str, default=TARGET_AWARD_WAY,
                        choices=["最低標", "最有利標/評選", "不限"],
                        help="決標方式 (預設: 最低標)")

    parser.add_argument("--date-mode", type=str, default=core.DATE_MODE_SPDT,
                        choices=[core.DATE_MODE_SPDT, core.DATE_MODE_RANGE],
                        help=f"日期模式 (預設: {core.DATE_MODE_SPDT}；此模式下站方會忽略 --days)")
    parser.add_argument("--no-verify", action="store_true",
                        help="略過官方詳細頁的決標方式深度校驗（快，但決標方式僅為推估值）")
    parser.add_argument("--verify-limit", type=int, default=core.DEFAULT_VERIFY_LIMIT,
                        help=f"深度校驗的筆數上限 (預設: {core.DEFAULT_VERIFY_LIMIT}，0 表示不限)")
    parser.add_argument("--only-keyword-hits", action="store_true",
                        help="只保留標案名稱命中關鍵字的標案 (預設全部保留)")

    args = parser.parse_args()
    run_crawler(
        keywords=args.keywords,
        days=args.days,
        target_attr=args.attr,
        target_award_way=args.award_way,
        date_mode=args.date_mode,
        verify=not args.no_verify,
        verify_limit=args.verify_limit,
        only_keyword_hits=args.only_keyword_hits,
    )


if __name__ == "__main__":
    main()
