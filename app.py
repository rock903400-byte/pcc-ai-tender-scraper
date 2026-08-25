# -*- coding: utf-8 -*-
"""
政府電子採購網 - AI 與資訊勞務最低標標案爬蟲 GUI 應用程式
使用 ttkbootstrap 建構現代化桌面介面，支援多執行緒搜尋、即時進度、表格展示與 Excel 匯出。
"""

import csv
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.cookiejar import CookieJar
import tkinter as tk
from tkinter import messagebox, filedialog

import ttkbootstrap as tb
from ttkbootstrap.constants import *
from ttkbootstrap.scrolled import ScrolledText

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# 強制優先使用 IPv4
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
    date_str = date_str.strip().replace("-", "/")
    parts = date_str.split("/")
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    return f"{y - 1911}/{m:02d}/{d:02d}"


def to_ad_date(date_str: str) -> str:
    parts = date_str.strip().replace("-", "/").split("/")
    if len(parts) == 3 and int(parts[0]) < 1900:
        return f"{int(parts[0]) + 1911}/{int(parts[1]):02d}/{int(parts[2]):02d}"
    return date_str


def determine_award_method(tender_way: str) -> tuple:
    tender_way = tender_way.strip()
    if "評選" in tender_way or "最有利標" in tender_way or "企劃書" in tender_way:
        return "最有利標 / 評選", False
    elif "公開招標" in tender_way:
        return "最低標 (公開招標)", True
    elif "公開取得" in tender_way:
        return "最低標 (公開取得報價單)", True
    elif "選擇性招標" in tender_way:
        return "最低標 / 選擇性招標", True
    elif "限制性招標" in tender_way:
        return "限制性招標", False
    return tender_way or "未標明", ("最低標" in tender_way)


def parse_tender_rows(html_doc: str, keyword: str) -> list:
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
            "預估決標方式": award_method_desc,
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


class PCCScraperApp(tb.Window):
    def __init__(self):
        super().__init__(themename="cosmo")
        self.title("政府電子採購網 (PCC) - AI 與資訊勞務最低標標案爬蟲")
        self.geometry("1180x820")
        self.minsize(980, 680)

        # 狀態變數
        self.is_running = False
        self.cancel_requested = False
        self.tenders_all = []
        self.tenders_matched = []
        self.output_dir = os.path.abspath("output")
        os.makedirs(self.output_dir, exist_ok=True)

        self.setup_ui()

    def setup_ui(self):
        # 1. 頂部標題與狀態列
        header_frame = tb.Frame(self, bootstyle="light", padding=15)
        header_frame.pack(fill=X)

        title_lbl = tb.Label(
            header_frame,
            text="🏛️ 政府電子採購網 - AI / 資訊 勞務最低標標案爬蟲",
            font=("Microsoft JhengHei", 16, "bold"),
            bootstyle="primary"
        )
        title_lbl.pack(side=LEFT)

        self.status_badge = tb.Label(
            header_frame,
            text="就緒",
            bootstyle="inverse-success",
            font=("Microsoft JhengHei", 10, "bold"),
            padding=(10, 4)
        )
        self.status_badge.pack(side=RIGHT)

        # 2. 控制面板 (Card)
        control_card = tb.Labelframe(self, text=" ⚙️ 搜尋條件設定 ", padding=15, bootstyle="info")
        control_card.pack(fill=X, padx=15, pady=10)

        # 第一列：常用關鍵字快速選擇
        kw_frame = tb.Frame(control_card)
        kw_frame.pack(fill=X, pady=(0, 10))

        tb.Label(kw_frame, text="關鍵字群 (空格分隔):", font=("Microsoft JhengHei", 10, "bold")).pack(side=LEFT, padx=(0, 8))
        
        default_kw_str = "AI 人工智慧 機器學習 深度學習 演算法 大數據 智慧化 網站 資訊 資訊系統 軟體 平台 資安 資料庫 網路 雲端"
        self.kw_entry = tb.Entry(kw_frame, font=("Microsoft JhengHei", 10))
        self.kw_entry.insert(0, default_kw_str)
        self.kw_entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))

        reset_btn = tb.Button(kw_frame, text="重設關鍵字", bootstyle="outline-secondary", command=lambda: self.reset_keywords(default_kw_str))
        reset_btn.pack(side=RIGHT)

        # 第二列：篩選與天數設定
        filter_row = tb.Frame(control_card)
        filter_row.pack(fill=X)

        tb.Label(filter_row, text="查詢天數:").pack(side=LEFT, padx=(0, 5))
        self.days_combo = tb.Combobox(filter_row, values=["1 (今日)", "3", "7", "14", "30", "60"], width=8, state="readonly")
        self.days_combo.set("7")
        self.days_combo.pack(side=LEFT, padx=(0, 15))

        tb.Label(filter_row, text="採購性質:").pack(side=LEFT, padx=(0, 5))
        self.attr_combo = tb.Combobox(filter_row, values=["勞務", "不限", "財物", "工程"], width=8, state="readonly")
        self.attr_combo.set("勞務")
        self.attr_combo.pack(side=LEFT, padx=(0, 15))

        tb.Label(filter_row, text="決標方式:").pack(side=LEFT, padx=(0, 5))
        self.award_combo = tb.Combobox(filter_row, values=["最低標", "不限", "最有利標/評選"], width=12, state="readonly")
        self.award_combo.set("最低標")
        self.award_combo.pack(side=LEFT, padx=(0, 20))

        # 操作按鈕群
        self.start_btn = tb.Button(filter_row, text="🚀 開始搜尋標案", bootstyle="success", command=self.on_start_scrape)
        self.start_btn.pack(side=RIGHT, padx=5)

        self.export_btn = tb.Button(filter_row, text="💾 匯出 Excel", bootstyle="primary", command=self.on_export_excel, state="disabled")
        self.export_btn.pack(side=RIGHT, padx=5)

        open_folder_btn = tb.Button(filter_row, text="📂 開啟輸出資料夾", bootstyle="outline-info", command=self.open_output_dir)
        open_folder_btn.pack(side=RIGHT, padx=5)

        # 3. 進度條與即時日誌
        progress_frame = tb.Frame(self, padding=(15, 0))
        progress_frame.pack(fill=X)

        self.progressbar = tb.Progressbar(progress_frame, mode="determinate", bootstyle="info-striped")
        self.progressbar.pack(fill=X, pady=(0, 5))

        # 4. 主要結果區域 (Notebook 分頁)
        self.notebook = tb.Notebook(self, bootstyle="primary")
        self.notebook.pack(fill=BOTH, expand=True, padx=15, pady=10)

        # 分頁 1: 精選勞務最低標
        self.tab_matched = tb.Frame(self.notebook)
        self.notebook.add(self.tab_matched, text=" 🏆 精選：勞務最低標 (0 筆) ")
        self.setup_treeview(self.tab_matched, is_matched=True)

        # 分頁 2: 所有搜尋結果
        self.tab_all = tb.Frame(self.notebook)
        self.notebook.add(self.tab_all, text=" 📋 所有搜尋標案 (0 筆) ")
        self.setup_treeview(self.tab_all, is_matched=False)

        # 分頁 3: 即時日誌
        self.tab_logs = tb.Frame(self.notebook)
        self.notebook.add(self.tab_logs, text=" 📝 執行紀錄 ")
        self.log_text = ScrolledText(self.tab_logs, height=10, font=("Consolas", 9), autohide=True)
        self.log_text.pack(fill=BOTH, expand=True, padx=10, pady=10)

        # 底部狀態列
        bottom_bar = tb.Frame(self, padding=(15, 5), bootstyle="secondary")
        bottom_bar.pack(fill=X, side=BOTTOM)
        self.bottom_status = tb.Label(bottom_bar, text="提示：雙擊表格任意列或點擊右側按鈕即可在瀏覽器開啟標案網址。", font=("Microsoft JhengHei", 9))
        self.bottom_status.pack(side=LEFT)

        self.log("✅ 應用程式初始化完成。請點擊「開始搜尋標案」開始執行。")

    def setup_treeview(self, parent_frame, is_matched: bool):
        # 搜尋篩選列
        top_filter = tb.Frame(parent_frame, padding=(5, 5))
        top_filter.pack(fill=X)

        tb.Label(top_filter, text="🔍 快速篩選:").pack(side=LEFT, padx=(0, 5))
        filter_entry = tb.Entry(top_filter, width=25)
        filter_entry.pack(side=LEFT, padx=(0, 10))

        open_link_btn = tb.Button(
            top_filter,
            text="🔗 開啟選取標案網頁",
            bootstyle="outline-primary",
            command=lambda: self.open_selected_link(tree)
        )
        open_link_btn.pack(side=RIGHT)

        # 表格 Treeview
        columns = ("seq", "pub_date", "org", "title", "budget", "award", "way", "deadline", "keyword")
        tree = tb.Treeview(
            parent_frame,
            columns=columns,
            show="headings",
            bootstyle="primary",
            selectmode="browse"
        )
        
        tree.heading("seq", text="#")
        tree.heading("pub_date", text="公告日期")
        tree.heading("org", text="招標機關")
        tree.heading("title", text="標案名稱")
        tree.heading("budget", text="預算金額")
        tree.heading("award", text="預估決標方式")
        tree.heading("way", text="招標方式")
        tree.heading("deadline", text="截止投標")
        tree.heading("keyword", text="命中關鍵字")

        tree.column("seq", width=40, anchor="center")
        tree.column("pub_date", width=90, anchor="center")
        tree.column("org", width=160, anchor="w")
        tree.column("title", width=340, anchor="w")
        tree.column("budget", width=110, anchor="e")
        tree.column("award", width=120, anchor="center")
        tree.column("way", width=140, anchor="w")
        tree.column("deadline", width=95, anchor="center")
        tree.column("keyword", width=100, anchor="center")

        # 捲軸
        scrollbar_y = tb.Scrollbar(parent_frame, orient="vertical", command=tree.yview)
        scrollbar_x = tb.Scrollbar(parent_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        scrollbar_y.pack(side=RIGHT, fill=Y)
        scrollbar_x.pack(side=BOTTOM, fill=X)
        tree.pack(fill=BOTH, expand=True)

        # 雙擊事件
        tree.bind("<Double-1>", lambda event: self.open_selected_link(tree))

        # 即時過濾綁定
        filter_entry.bind("<KeyRelease>", lambda event: self.filter_treeview(tree, filter_entry.get(), is_matched))

        if is_matched:
            self.tree_matched = tree
        else:
            self.tree_all = tree

    def reset_keywords(self, default_str):
        self.kw_entry.delete(0, END)
        self.kw_entry.insert(0, default_str)

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(END, f"[{timestamp}] {message}\n")
        self.log_text.see(END)

    def open_selected_link(self, tree):
        selected_item = tree.selection()
        if not selected_item:
            messagebox.showinfo("提示", "請先點選欲查看的標案列！")
            return
        item_values = tree.item(selected_item[0], "values")
        # 標案案號或詳細連結
        tender_name = item_values[3]
        
        # 從資料中找出對應的連結
        target_url = None
        for t in self.tenders_all:
            if t.get("標案名稱") == tender_name:
                target_url = t.get("詳細連結")
                break
        
        if target_url:
            webbrowser.open(target_url)
        else:
            messagebox.showwarning("警告", "無法找到該標案的詳細網址！")

    def open_output_dir(self):
        if os.path.exists(self.output_dir):
            if sys.platform == "win32":
                os.startfile(self.output_dir)
            else:
                webbrowser.open(f"file://{self.output_dir}")

    def filter_treeview(self, tree, query: str, is_matched: bool):
        query = query.strip().lower()
        dataset = self.tenders_matched if is_matched else self.tenders_all
        tree.delete(*tree.get_children())

        seq = 1
        for t in dataset:
            row_str = f"{t.get('招標機關', '')} {t.get('標案名稱', '')} {t.get('標案案號', '')} {t.get('命中關鍵字', '')}".lower()
            if not query or query in row_str:
                tree.insert("", END, values=(
                    seq,
                    t.get("公告日期", ""),
                    t.get("招標機關", ""),
                    t.get("標案名稱", ""),
                    t.get("預算金額", ""),
                    t.get("預估決標方式", ""),
                    t.get("招標方式", ""),
                    t.get("截止投標", ""),
                    t.get("命中關鍵字", "")
                ))
                seq += 1

    def on_start_scrape(self):
        if self.is_running:
            return

        raw_kws = self.kw_entry.get().strip()
        if not raw_kws:
            messagebox.showwarning("警告", "請至少輸入一個搜尋關鍵字！")
            return

        keywords = [k.strip() for k in re.split(r"[\s,]+", raw_kws) if k.strip()]
        days_val = self.days_combo.get().split()[0]
        days = int(days_val) if days_val.isdigit() else 7
        target_attr = self.attr_combo.get()
        target_award = self.award_combo.get()

        self.is_running = True
        self.start_btn.configure(text="⏳ 搜尋中...", state="disabled", bootstyle="secondary")
        self.export_btn.configure(state="disabled")
        self.status_badge.configure(text="搜尋中...", bootstyle="inverse-warning")
        self.progressbar.configure(value=0)

        # 清空表格
        self.tree_matched.delete(*self.tree_matched.get_children())
        self.tree_all.delete(*self.tree_all.get_children())

        # 啟動背景工作執行緒
        threading.Thread(
            target=self.run_scrape_thread,
            args=(keywords, days, target_attr, target_award),
            daemon=True
        ).start()

    def run_scrape_thread(self, keywords, days, target_attr, target_award):
        try:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            start_roc = to_roc_date(start_date.strftime("%Y/%m/%d"))
            end_roc = to_roc_date(end_date.strftime("%Y/%m/%d"))

            self.log(f"🚀 開始搜尋：民國 {start_roc} ~ {end_roc} (最近 {days} 天)")
            self.log(f"🔑 關鍵字共 {len(keywords)} 組: {', '.join(keywords)}")

            unique_tenders = {}
            total_kws = len(keywords)

            for idx, kw in enumerate(keywords, start=1):
                self.log(f"🔍 [{idx}/{total_kws}] 正在搜尋：【{kw}】...")
                
                # 發送請求
                form_data = {
                    "pageSize": "50",
                    "firstSearch": "true",
                    "searchType": "basic",
                    "isBinding": "N",
                    "isLogIn": "N",
                    "orgName": "",
                    "orgId": "",
                    "tenderName": kw,
                    "tenderId": "",
                    "tenderType": "TENDER_DECLARATION",
                    "tenderWay": "TENDER_WAY_ALL_DECLARATION",
                    "dateType": "isSpdt",
                    "tenderStartDate": start_roc,
                    "tenderEndDate": end_roc,
                }
                if target_attr == "勞務":
                    form_data["radProctrgCate"] = "RAD_PROCTRG_CATE_3"
                elif target_attr == "財物":
                    form_data["radProctrgCate"] = "RAD_PROCTRG_CATE_2"
                elif target_attr == "工程":
                    form_data["radProctrgCate"] = "RAD_PROCTRG_CATE_1"

                try:
                    data = urllib.parse.urlencode(form_data).encode("utf-8")
                    req = urllib.request.Request(BASIC_SEARCH_URL, data=data, headers=HEADERS)
                    with opener.open(req, timeout=30) as resp:
                        html_doc = resp.read().decode("utf-8", errors="replace")
                    
                    parsed_list = parse_tender_rows(html_doc, kw)
                    for t in parsed_list:
                        tid = t["標案案號"]
                        if tid not in unique_tenders:
                            t["命中關鍵字群"] = [kw]
                            unique_tenders[tid] = t
                        else:
                            if kw not in unique_tenders[tid]["命中關鍵字群"]:
                                unique_tenders[tid]["命中關鍵字群"].append(kw)
                except Exception as e:
                    self.log(f"  ⚠️ 關鍵字【{kw}】搜尋發生例外: {e}")

                # 更新進度條
                progress = int((idx / total_kws) * 100)
                self.after(0, self.update_progress, progress)
                time.sleep(0.6)

            # 整理結果
            self.tenders_all = list(unique_tenders.values())
            for t in self.tenders_all:
                t["命中關鍵字"] = ", ".join(t.get("命中關鍵字群", []))

            # 篩選符合條件標案
            self.tenders_matched = []
            for t in self.tenders_all:
                attr_ok = (target_attr == "不限") or (target_attr in t.get("採購性質", ""))
                award_ok = (target_award == "不限") or (t.get("是否為最低標") == "是" if target_award == "最低標" else "最有利標" in t.get("預估決標方式", ""))
                if attr_ok and award_ok:
                    self.tenders_matched.append(t)

            self.after(0, self.on_scrape_completed)

        except Exception as e:
            self.log(f"❌ 搜尋過程發生未預期錯誤: {e}")
            self.after(0, self.on_scrape_failed, str(e))

    def update_progress(self, val):
        self.progressbar.configure(value=val)

    def on_scrape_completed(self):
        self.is_running = False
        self.start_btn.configure(text="🚀 開始搜尋標案", state="normal", bootstyle="success")
        self.export_btn.configure(state="normal")
        self.status_badge.configure(text="搜尋完成", bootstyle="inverse-success")
        self.progressbar.configure(value=100)

        # 更新分頁標題筆數
        self.notebook.tab(0, text=f" 🏆 精選：勞務最低標 ({len(self.tenders_matched)} 筆) ")
        self.notebook.tab(1, text=f" 📋 所有搜尋標案 ({len(self.tenders_all)} 筆) ")

        # 填入表格資料
        self.filter_treeview(self.tree_matched, "", is_matched=True)
        self.filter_treeview(self.tree_all, "", is_matched=False)

        self.log(f"🎉 搜尋全部完成！共撈取 {len(self.tenders_all)} 筆不重複標案，其中精選符合【勞務+最低標】共 {len(self.tenders_matched)} 筆。")
        self.bottom_status.configure(text=f"完成！共找到 {len(self.tenders_all)} 筆標案（精選符合: {len(self.tenders_matched)} 筆）。")

        # 自動先匯出一份 Excel 備份
        self.auto_export_backup()

    def auto_export_backup(self):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            excel_path = os.path.join(self.output_dir, f"pcc_tenders_{timestamp}.xlsx")
            
            preferred_cols = [
                "完全符合目標", "標案名稱", "招標機關", "預算金額", "預估決標方式", "招標方式",
                "採購性質", "公告日期", "截止投標", "命中關鍵字", "標案案號", "詳細連結"
            ]

            if HAS_PANDAS and self.tenders_all:
                df_all = pd.DataFrame(self.tenders_all)
                df_matched = pd.DataFrame(self.tenders_matched)
                cols_all = [c for c in preferred_cols if c in df_all.columns]
                df_all = df_all[cols_all]

                with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
                    if not df_matched.empty:
                        df_matched = df_matched[[c for c in preferred_cols if c in df_matched.columns]]
                        df_matched.to_excel(writer, sheet_name="精選_勞務最低標", index=False)
                    else:
                        pd.DataFrame([{"說明": "本次搜尋無符合勞務最低標之標案"}]).to_excel(writer, sheet_name="精選_勞務最低標", index=False)
                    df_all.to_excel(writer, sheet_name="所有搜尋標案", index=False)
                self.log(f"💾 自動備份 Excel 已儲存至: {excel_path}")
        except Exception as e:
            self.log(f"  ⚠️ 自動儲存備份失敗: {e}")

    def on_export_excel(self):
        if not self.tenders_all:
            messagebox.showwarning("警告", "目前無任何搜尋資料可供匯出！")
            return

        file_path = filedialog.asksaveasfilename(
            initialdir=self.output_dir,
            initialfile=f"政府採購網_標案搜尋_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
            defaultextension=".xlsx",
            filetypes=[("Excel 活頁簿", "*.xlsx"), ("所有檔案", "*.*")]
        )
        if not file_path:
            return

        try:
            preferred_cols = [
                "完全符合目標", "標案名稱", "招標機關", "預算金額", "預估決標方式", "招標方式",
                "採購性質", "公告日期", "截止投標", "命中關鍵字", "標案案號", "詳細連結"
            ]
            df_all = pd.DataFrame(self.tenders_all)
            df_matched = pd.DataFrame(self.tenders_matched)
            cols_all = [c for c in preferred_cols if c in df_all.columns]
            df_all = df_all[cols_all]

            with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
                if not df_matched.empty:
                    df_matched = df_matched[[c for c in preferred_cols if c in df_matched.columns]]
                    df_matched.to_excel(writer, sheet_name="精選_勞務最低標", index=False)
                else:
                    pd.DataFrame([{"說明": "本次搜尋無符合勞務最低標之標案"}]).to_excel(writer, sheet_name="精選_勞務最低標", index=False)
                df_all.to_excel(writer, sheet_name="所有搜尋標案", index=False)

            messagebox.showinfo("匯出成功", f"標案資料已成功匯出至：\n{file_path}")
        except Exception as e:
            messagebox.showerror("匯出失敗", f"匯出過程發生錯誤：\n{e}")

    def on_scrape_failed(self, err_msg):
        self.is_running = False
        self.start_btn.configure(text="🚀 開始搜尋標案", state="normal", bootstyle="success")
        self.status_badge.configure(text="失敗", bootstyle="inverse-danger")
        messagebox.showerror("錯誤", f"搜尋發生錯誤：\n{err_msg}")


def main():
    app = PCCScraperApp()
    app.mainloop()


if __name__ == "__main__":
    main()
