# -*- coding: utf-8 -*-
"""
政府電子採購網 (web.pcc.gov.tw) - AI 與資訊勞務最低標標案爬蟲 (CLI)

抓取最新標案公告，並以公開資料鏡像（官網詳細頁備援）精準判定「勞務類」與「最低標」
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
    USE_MIRROR_SOURCE,
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
                include_keyword_misses: bool = False):
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

    core.finalize_keywords(tenders_list)

    if not tenders_list:
        print("[INFO] 本次搜尋條件下未發現任何標案。")
        return

    # 先套快取：確認過的標案不該再查一次，鏡像與官網的請求都省下來
    cache_path = core.award_cache_path(OUTPUT_DIR)
    cache = core.load_award_cache(cache_path)
    applied = core.apply_award_cache(tenders_list, cache)
    if applied:
        print(f"\n[CACHE] 由 {cache_path} 套用 {applied} 筆先前已確認的官方決標方式（不必重查）。")

    # 待確認清單落地：背景涓流（--verify-only）靠它拿到「還有哪些要查」，
    # 不必為了取得清單而重跑一次動輒上百頁的全面掃描。
    queue_path = core.pending_queue_path(OUTPUT_DIR)
    pending_rows = core.select_rows_for_enrichment(
        tenders_list, target_attr, target_award_way, limit=0,
        require_keyword_hit=not include_keyword_misses)
    if core.save_pending_queue(pending_rows, queue_path):
        print(f"[QUEUE] 待確認清單已更新: {queue_path}（{len(pending_rows)} 筆）")
    else:
        print(f"[WARN] 無法寫入待確認清單 {queue_path}，背景涓流校驗會沒有東西可撿。")

    # 只校驗有機會入選、且尚未確認的標案：資料來源再寬鬆，也沒理由為不會入選的列打上千次請求
    if verify:
        targets = core.select_rows_for_enrichment(tenders_list, target_attr, target_award_way,
                                                  limit=verify_limit,
                                                  require_keyword_hit=not include_keyword_misses)
        if targets:
            print(f"\n[VERIFY] 從 {len(tenders_list)} 筆中挑出 {len(targets)} 筆尚未確認的候選，"
                  f"校驗決標方式（主來源：公開資料鏡像，官網詳細頁備援）...")
            stats = core.enrich_actual_award_methods(targets, log=print,
                                                     cache=cache, cache_path=cache_path,
                                                     use_mirror=USE_MIRROR_SOURCE)
            if stats["blocked"]:
                print(f"[VERIFY] 本次確認 {stats['ok']} 筆後官網詳細頁額度用盡，已中止。"
                      f"確認結果已寫入 {cache_path}，下次執行會直接套用。")
            else:
                print(f"[VERIFY] {stats['ok']}/{stats['total']} 筆取得官方決標方式"
                      f"（鏡像 {stats['mirror_ok']} 筆、官網詳細頁 {stats['official_ok']} 筆），"
                      f"其餘維持「{core.AWARD_SOURCE_ESTIMATED}」。")
    else:
        print(f"\n[VERIFY] 已略過深度校驗，未快取的標案決標方式全部為"
              f"「{core.AWARD_SOURCE_ESTIMATED}」。")

    # 精選 = 採購性質 ∩ 決標方式 ∩ 命中關鍵字；未命中者不會被丟掉，仍在「所有搜尋標案」。
    qualified = core.filter_tenders(tenders_list, target_attr, target_award_way)
    keyword_hits = core.filter_tenders(tenders_list, target_attr, target_award_way,
                                       require_keyword_hit=True)
    matched_tenders = qualified if include_keyword_misses else keyword_hits

    print("\n" + "=" * 65)
    print("[SUMMARY] 執行成果摘要")
    print(f"  • 全部搜尋到標案: {len(tenders_list)} 筆")
    pending = sum(1 for t in qualified
                  if not core.is_award_confirmed(t) and "待確認" in t.get("決標方式", ""))
    print(f"  • 符合【{target_attr} + {target_award_way}】: {len(qualified)} 筆"
          f"（其中命中關鍵字 {len(keyword_hits)} 筆）")
    print(f"  • 精選清單: {len(matched_tenders)} 筆"
          + ("（含未命中關鍵字者）" if include_keyword_misses else "（條件 ∩ 關鍵字）"))
    if pending:
        print(f"  • ⚠️ 其中 {pending} 筆決標方式仍為「公開取得 (待確認)」——搜尋結果頁看不出決標方式，"
              f"實測這類標案有相當比例其實是最有利標；")
        print(f"       每次執行都會自動補進 {cache_path}（--verify-only 可持續補完），"
              f"要立刻確認請開該筆的詳細連結自行查看。")
    print("=" * 65)

    if matched_tenders:
        label = "" if include_keyword_misses else " ∩ 關鍵字"
        print(f"\n🏆 精選【{target_attr} + {target_award_way}{label}】標案清單：")
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


def run_trickle(rounds: int = 1, interval: int = core.DEFAULT_TRICKLE_INTERVAL_SECONDS):
    """
    只跑背景涓流校驗，不做搜尋。

    主來源改成沒有額度限制的公開資料鏡像後，每輪能撿 DEFAULT_TRICKLE_BATCH 筆。
    把這個模式掛進 Windows 工作排程器每 15 分鐘跑一次，一天約 96 輪，
    確認速度遠遠追得上新標案的產生速度。
    """
    cache_path = core.award_cache_path(OUTPUT_DIR)
    queue_path = core.pending_queue_path(OUTPUT_DIR)
    print("=" * 65)
    print("[*] 背景涓流校驗（不搜尋，只補決標方式）")
    print(f"[*] 快取: {cache_path}")
    print(f"[*] 佇列: {queue_path}")
    print("=" * 65)

    for round_no in range(1, max(1, rounds) + 1):
        result = core.trickle_verify(cache_path, queue_path, log=print,
                                     use_mirror=USE_MIRROR_SOURCE)
        if not result["picked"]:
            print("[DONE] 待確認佇列是空的——先跑一次搜尋產生佇列，或是已經全部確認完了。")
            return
        print(f"[ROUND {round_no}] 撿 {result['picked']} 筆、確認 {result['ok']} 筆、"
              f"待確認尚餘 {result['remaining']} 筆"
              + ("（官網詳細頁額度已用盡）" if result["blocked"] else ""))
        if round_no < rounds:
            print(f"[WAIT] 等待 {interval} 秒再跑下一輪...")
            time.sleep(interval)

    print('\n[DONE] 涓流校驗結束。\n')


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
    parser.add_argument("--include-keyword-misses", action="store_true",
                        help="精選清單改為納入未命中關鍵字的標案 (預設只收命中者)")

    parser.add_argument("--verify-only", action="store_true",
                        help="不搜尋，只讀待確認佇列跑背景涓流校驗 (適合每 15 分鐘排程一次)")
    parser.add_argument("--rounds", type=int, default=1,
                        help="--verify-only 時要跑幾輪 (預設 1；多輪之間會等 --interval 秒)")
    parser.add_argument("--interval", type=int, default=core.DEFAULT_TRICKLE_INTERVAL_SECONDS,
                        help=f"多輪涓流之間的等待秒數 (預設 {core.DEFAULT_TRICKLE_INTERVAL_SECONDS})")

    args = parser.parse_args()

    if args.verify_only:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        run_trickle(rounds=args.rounds, interval=args.interval)
        return

    run_crawler(
        keywords=args.keywords,
        days=args.days,
        target_attr=args.attr,
        target_award_way=args.award_way,
        date_mode=args.date_mode,
        verify=not args.no_verify,
        verify_limit=args.verify_limit,
        include_keyword_misses=args.include_keyword_misses,
    )


if __name__ == "__main__":
    main()
