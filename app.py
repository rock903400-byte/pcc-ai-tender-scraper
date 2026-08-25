# -*- coding: utf-8 -*-
"""
政府電子採購網 - AI 與資訊勞務最低標標案爬蟲 GUI 應用程式
使用 ttkbootstrap 建構現代化桌面介面，支援多執行緒搜尋、官方詳細頁真實決標方式深度校驗、
表格多欄位智能排序（金額/日期/字串）、即時進度與 Excel 匯出。
"""

import concurrent.futures
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

# 表格欄位定義 (id, 預設標題, 寬度, 對齊方式)
COLUMNS_CONFIG = [
    ("seq", "#", 40, "center"),
    ("pub_date", "公告日期", 90, "center"),
    ("org", "招標機關", 160, "w"),
    ("title", "標案名稱", 340, "w"),
    ("budget", "預算金額", 110, "e"),
    ("award", "決標方式", 140, "center"),
    ("way", "招標方式", 150, "w"),
    ("deadline", "截止投標", 95, "center"),
    ("keyword", "命中關鍵字", 100, "center"),
]


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


class PCCScraperApp(tb.Window):
    def __init__(self):
        super().__init__(themename="cosmo")
        self.title("政府電子採購網 (PCC) - AI 與資訊勞務最低標標案爬蟲")
        self.geometry("1180x820")
        self.minsize(980, 680)

        self.is_running = False
        self.tenders_all = []
        self.tenders_matched = []
        self.output_dir = os.path.abspath("output")
        os.makedirs(self.output_dir, exist_ok=True)

        # 排序狀態記錄
        self.sort_state_matched = {"col": None, "reverse": False}
        self.sort_state_all = {"col": None, "reverse": False}
        self.filter_entry_matched = None
        self.filter_entry_all = None

        self.setup_ui()

    def setup_ui(self):
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

        control_card = tb.Labelframe(self, text=" ⚙️ 搜尋條件設定 ", padding=15, bootstyle="info")
        control_card.pack(fill=X, padx=15, pady=10)

        kw_frame = tb.Frame(control_card)
        kw_frame.pack(fill=X, pady=(0, 10))

        tb.Label(kw_frame, text="關鍵字群 (空格分隔):", font=("Microsoft JhengHei", 10, "bold")).pack(side=LEFT, padx=(0, 8))
        
        default_kw_str = "AI 人工智慧 機器學習 深度學習 演算法 大數據 智慧化 網站 資訊 資訊系統 軟體 平台 資安 資料庫 網路 雲端"
        self.kw_entry = tb.Entry(kw_frame, font=("Microsoft JhengHei", 10))
        self.kw_entry.insert(0, default_kw_str)
        self.kw_entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))

        reset_btn = tb.Button(kw_frame, text="重設關鍵字", bootstyle="outline-secondary", command=lambda: self.reset_keywords(default_kw_str))
        reset_btn.pack(side=RIGHT)

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

        self.start_btn = tb.Button(filter_row, text="🚀 開始搜尋標案", bootstyle="success", command=self.on_start_scrape)
        self.start_btn.pack(side=RIGHT, padx=5)

        self.export_btn = tb.Button(filter_row, text="💾 匯出 Excel", bootstyle="primary", command=self.on_export_excel, state="disabled")
        self.export_btn.pack(side=RIGHT, padx=5)

        open_folder_btn = tb.Button(filter_row, text="📂 開啟輸出資料夾", bootstyle="outline-info", command=self.open_output_dir)
        open_folder_btn.pack(side=RIGHT, padx=5)

        progress_frame = tb.Frame(self, padding=(15, 0))
        progress_frame.pack(fill=X)

        self.progressbar = tb.Progressbar(progress_frame, mode="determinate", bootstyle="info-striped")
        self.progressbar.pack(fill=X, pady=(0, 5))

        self.notebook = tb.Notebook(self, bootstyle="primary")
        self.notebook.pack(fill=BOTH, expand=True, padx=15, pady=10)

        self.tab_matched = tb.Frame(self.notebook)
        self.notebook.add(self.tab_matched, text=" 🏆 精選：勞務最低標 (0 筆) ")
        self.setup_treeview(self.tab_matched, is_matched=True)

        self.tab_all = tb.Frame(self.notebook)
        self.notebook.add(self.tab_all, text=" 📋 所有搜尋標案 (0 筆) ")
        self.setup_treeview(self.tab_all, is_matched=False)

        self.tab_logs = tb.Frame(self.notebook)
        self.notebook.add(self.tab_logs, text=" 📝 執行紀錄 ")
        self.log_text = ScrolledText(self.tab_logs, height=10, font=("Consolas", 9), autohide=True)
        self.log_text.pack(fill=BOTH, expand=True, padx=10, pady=10)

        bottom_bar = tb.Frame(self, padding=(15, 5), bootstyle="secondary")
        bottom_bar.pack(fill=X, side=BOTTOM)
        self.bottom_status = tb.Label(
            bottom_bar,
            text="提示：點選表格任一欄位標題即可切換【升冪 ▲ / 降冪 ▼】排序；雙擊任意列開啟標案網址。",
            font=("Microsoft JhengHei", 9)
        )
        self.bottom_status.pack(side=LEFT)

        self.log("✅ 應用程式初始化完成。請點擊「開始搜尋標案」開始執行。")

    def setup_treeview(self, parent_frame, is_matched: bool):
        top_filter = tb.Frame(parent_frame, padding=(5, 5))
        top_filter.pack(fill=X)

        tb.Label(top_filter, text="🔍 快速篩選:").pack(side=LEFT, padx=(0, 5))
        filter_entry = tb.Entry(top_filter, width=25)
        filter_entry.pack(side=LEFT, padx=(0, 10))

        if is_matched:
            self.filter_entry_matched = filter_entry
        else:
            self.filter_entry_all = filter_entry

        open_link_btn = tb.Button(
            top_filter,
            text="🔗 開啟選取標案網頁",
            bootstyle="outline-primary",
            command=lambda: self.open_selected_link(tree)
        )
        open_link_btn.pack(side=RIGHT)

        col_ids = [c[0] for c in COLUMNS_CONFIG]
        tree = tb.Treeview(
            parent_frame,
            columns=col_ids,
            show="headings",
            bootstyle="primary",
            selectmode="browse"
        )

        for col_id, col_name, width, align in COLUMNS_CONFIG:
            tree.heading(
                col_id,
                text=col_name,
                anchor=align,
                command=lambda c=col_id: self.on_sort_column(tree, c, is_matched)
            )
            tree.column(col_id, width=width, anchor=align)

        scrollbar_y = tb.Scrollbar(parent_frame, orient="vertical", command=tree.yview)
        scrollbar_x = tb.Scrollbar(parent_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        scrollbar_y.pack(side=RIGHT, fill=Y)
        scrollbar_x.pack(side=BOTTOM, fill=X)
        tree.pack(fill=BOTH, expand=True)

        tree.bind("<Double-1>", lambda event: self.open_selected_link(tree))
        filter_entry.bind("<KeyRelease>", lambda event: self.filter_treeview(tree, filter_entry.get(), is_matched))

        if is_matched:
            self.tree_matched = tree
        else:
            self.tree_all = tree

    def extract_sort_key(self, item: dict, col_id: str):
        """型態感知之排序 Key 萃取器"""
        if col_id == "budget":
            raw_b = str(item.get("預算金額", ""))
            digits = re.sub(r"[^\d.]", "", raw_b)
            return float(digits) if digits else -1.0
        elif col_id == "pub_date":
            return str(item.get("公告日期", ""))
        elif col_id == "deadline":
            return str(item.get("截止投標", ""))
        elif col_id == "org":
            return str(item.get("招標機關", ""))
        elif col_id == "title":
            return str(item.get("標案名稱", ""))
        elif col_id == "award":
            return str(item.get("決標方式", ""))
        elif col_id == "way":
            return str(item.get("招標方式", ""))
        elif col_id == "keyword":
            return str(item.get("命中關鍵字", ""))
        return str(item.get("標案案號", ""))

    def on_sort_column(self, tree, col_id: str, is_matched: bool):
        """點擊欄頭切換升降冪排序"""
        state = self.sort_state_matched if is_matched else self.sort_state_all
        dataset = self.tenders_matched if is_matched else self.tenders_all

        if not dataset:
            return

        if state["col"] == col_id:
            state["reverse"] = not state["reverse"]
        else:
            state["col"] = col_id
            # 預算與日期預設先以降冪（最高/最新）呈現，其餘預設升冪
            state["reverse"] = (col_id in ("budget", "pub_date", "deadline"))

        reverse = state["reverse"]
        dataset.sort(key=lambda t: self.extract_sort_key(t, col_id), reverse=reverse)

        # 更新欄位標題箭頭
        indicator = " ▼" if reverse else " ▲"
        for c_id, c_name, _, _ in COLUMNS_CONFIG:
            if c_id == col_id:
                tree.heading(c_id, text=f"{c_name}{indicator}")
            else:
                tree.heading(c_id, text=c_name)

        # 重新整理顯示內容（維持目前的篩選框文字）
        filter_entry = self.filter_entry_matched if is_matched else self.filter_entry_all
        query = filter_entry.get() if filter_entry else ""
        self.filter_treeview(tree, query, is_matched)

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
        tender_name = item_values[3]
        
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
                    t.get("決標方式", ""),
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

        self.tree_matched.delete(*self.tree_matched.get_children())
        self.tree_all.delete(*self.tree_all.get_children())

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
                    "tenderWay": "",
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

                progress = int((idx / (total_kws + 1)) * 100)
                self.after(0, self.update_progress, progress)
                time.sleep(0.5)

            tenders_list = list(unique_tenders.values())
            
            # 多執行緒校驗官方詳細頁中的真實決標方式
            if tenders_list:
                self.log(f"⚡ 正在平行連線官方詳細頁，校驗真實決標方式 (共 {len(tenders_list)} 筆)...")
                def _enrich(t):
                    pk = t.get("pk")
                    if pk:
                        actual = fetch_actual_award_method(pk)
                        if actual:
                            desc, is_lowest = determine_award_method(t.get("招標方式", ""), actual)
                            t["決標方式"] = desc
                            t["是否為最低標"] = "是" if is_lowest else "否"
                            is_service = (t.get("是否為勞務類") == "是")
                            t["完全符合目標"] = "符合 (勞務+最低標)" if (is_service and is_lowest) else "其他"

                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                    list(executor.map(_enrich, tenders_list))

            for t in tenders_list:
                t["命中關鍵字"] = ", ".join(t.get("命中關鍵字群", []))

            self.tenders_all = tenders_list
            self.tenders_matched = []
            for t in self.tenders_all:
                attr_ok = (target_attr == "不限") or (target_attr in t.get("採購性質", ""))
                if target_award == "最低標":
                    award_ok = (t.get("是否為最低標") == "是")
                elif target_award == "最有利標/評選":
                    award_ok = ("最有利標" in t.get("決標方式", "") or "評選" in t.get("決標方式", "") or "評審" in t.get("決標方式", ""))
                else:
                    award_ok = True

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

        self.notebook.tab(0, text=f" 🏆 精選：勞務最低標 ({len(self.tenders_matched)} 筆) ")
        self.notebook.tab(1, text=f" 📋 所有搜尋標案 ({len(self.tenders_all)} 筆) ")

        self.filter_treeview(self.tree_matched, "", is_matched=True)
        self.filter_treeview(self.tree_all, "", is_matched=False)

        self.log(f"🎉 搜尋全部完成！共撈取 {len(self.tenders_all)} 筆不重複標案，其中精選符合【勞務+最低標】共 {len(self.tenders_matched)} 筆。")
        self.bottom_status.configure(text=f"完成！共找到 {len(self.tenders_all)} 筆標案（精選符合: {len(self.tenders_matched)} 筆）。提示：點擊任一欄位標題可排序。")

        self.auto_export_backup()

    def auto_export_backup(self):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            excel_path = os.path.join(self.output_dir, f"pcc_tenders_{timestamp}.xlsx")
            
            preferred_cols = [
                "完全符合目標", "標案名稱", "招標機關", "預算金額", "決標方式", "招標方式",
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
                "完全符合目標", "標案名稱", "招標機關", "預算金額", "決標方式", "招標方式",
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
