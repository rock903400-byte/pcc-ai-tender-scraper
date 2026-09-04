# -*- coding: utf-8 -*-
"""
匯出相關 helpers（由 app.py 抽離，縮減主檔體積）。

原位於 app.py 的 build_excel_bytes / build_csv_bytes / rows_fingerprint /
tender_key / tender_label / cached_* / auto_export_backup 加上 prune 邏輯，
邏輯一行不改，僅搬家。保持 ui_logic.sanitize_rows 單一來源。
"""
import glob
import io
import os
from datetime import datetime

import pandas as pd

import pcc_core as core
from ui_logic import sanitize_rows


def prune_output_excels(output_dir: str, keep: int = 20):
    """輪替刪除 output 下舊的 pcc_tenders_*.xlsx，保留最新 keep 份。"""
    try:
        pattern = os.path.join(output_dir, "pcc_tenders_*.xlsx")
        files = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p))
        if len(files) > keep:
            for old in files[:-keep]:
                try:
                    os.remove(old)
                except OSError:
                    pass
    except Exception:
        pass


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


def cached_excel_bytes(fingerprint: str, _all_tenders: list, _matched_tenders: list) -> bytes:
    # Flask 版無 Streamlit 快取，fingerprint 僅保留介面相容，直接生成
    return build_excel_bytes(_all_tenders, _matched_tenders)


def cached_csv_bytes(fingerprint: str, _rows: list) -> bytes:
    return build_csv_bytes(_rows)


def auto_export_backup(output_dir: str, all_tenders: list, matched_tenders: list, keep: int = 20):
    if not all_tenders:
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_dir, f"pcc_tenders_{timestamp}.xlsx")
    try:
        all_s = sanitize_rows(all_tenders)
        matched_s = sanitize_rows(matched_tenders)
        core.write_excel_report(path, all_s, matched_s)
        prune_output_excels(output_dir, keep)
        return os.path.join("output", os.path.basename(path))
    except Exception as e:
        return f"失敗: {e}"
