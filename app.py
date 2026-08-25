# -*- coding: utf-8 -*-
"""
政府電子採購網 - AI 與資訊勞務最低標標案爬蟲 GUI 應用程式

使用 ttkbootstrap 建構桌面介面，支援背景執行緒搜尋、官方詳細頁真實決標方式深度校驗、
表格多欄位型態感知排序（金額／日期／字串）、即時進度與 Excel / CSV 匯出。

爬取與解析邏輯共用自 pcc_core，與 CLI 版 crawler.py 為同一份實作。
"""

import os
import queue
import re
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import messagebox, filedialog

import ttkbootstrap as tb
from ttkbootstrap.constants import BOTH, BOTTOM, END, LEFT, RIGHT, X, Y
try:  # ttkbootstrap 1.14+ 的新路徑；舊版仍在 ttkbootstrap.scrolled
    from ttkbootstrap.widgets.scrolled import ScrolledText
except ImportError:  # pragma: no cover - 取決於安裝的 ttkbootstrap 版本
    from ttkbootstrap.scrolled import ScrolledText

import pcc_core as core
from config import DEFAULT_KEYWORDS

core.install_ipv4_preference()

# 表格欄位定義 (欄位 id, 標題, 寬度, 對齊方式, 對應的資料鍵)
COLUMNS_CONFIG = [
    ("seq", "#", 40, "center", None),
    ("pub_date", "公告日期", 95, "center", "公告日期"),
    ("org", "招標機關", 160, "w", "招標機關"),
    ("title", "標案名稱", 330, "w", "標案名稱"),
    ("budget", "預算金額", 110, "e", "預算金額"),
    ("award", "決標方式", 140, "center", "決標方式"),
    ("way", "招標方式", 150, "w", "招標方式"),
    ("award_src", "決標依據", 105, "center", "決標方式來源"),
    ("deadline", "截止投標", 95, "center", "截止投標"),
    ("keyword", "命中關鍵字", 110, "center", "命中關鍵字"),
]

COLUMN_FIELD = {col_id: field for col_id, _, _, _, field in COLUMNS_CONFIG}

# 搜尋階段佔進度條的比例，其餘留給詳細頁校驗
SEARCH_PROGRESS_SHARE = 70


class PCCScraperApp(tb.Window):
    def __init__(self):
        super().__init__(themename="cosmo")
        self.title("政府電子採購網 (PCC) - AI 與資訊勞務最低標標案爬蟲")
        self.geometry("1180x820")
        self.minsize(980, 680)

        self.is_running = False
        self.tenders_all = []
        # 精選有兩份：符合採購性質＋決標方式者，以及其中還命中關鍵字者。
        # tenders_matched 永遠指向目前顯示中的那一份，排序與快速篩選才不必分兩套。
        self.tenders_qualified = []
        self.tenders_keyword_hits = []
        self.tenders_matched = []
        self.tenders_by_pk = {}
        self.output_dir = os.path.abspath("output")
        os.makedirs(self.output_dir, exist_ok=True)

        # 排序狀態記錄
        self.sort_state_matched = {"col": None, "reverse": False}
        self.sort_state_all = {"col": None, "reverse": False}
        self.filter_entry_matched = None
        self.filter_entry_all = None

        # 背景執行緒 -> 主執行緒的訊息佇列（Tkinter 僅能於主執行緒操作 widget）
        self.ui_queue = queue.Queue()

        # 目前套用的篩選條件，用於分頁標題文字
        self.active_filter_label = "勞務最低標"
        self.default_keywords = " ".join(DEFAULT_KEYWORDS)
        self.setup_ui()
        self.after(100, self._drain_ui_queue)

    # ==================== UI 建構 ====================

    def setup_ui(self):
        header_frame = tb.Frame(self, bootstyle="light", padding=15)
        header_frame.pack(fill=X)

        tb.Label(
            header_frame,
            text="🏛️ 政府電子採購網 - AI / 資訊 勞務最低標標案爬蟲",
            font=("Microsoft JhengHei", 16, "bold"),
            bootstyle="primary",
        ).pack(side=LEFT)

        self.status_badge = tb.Label(
            header_frame,
            text="就緒",
            bootstyle="inverse-success",
            font=("Microsoft JhengHei", 10, "bold"),
            padding=(10, 4),
        )
        self.status_badge.pack(side=RIGHT)

        control_card = tb.Labelframe(self, text=" ⚙️ 搜尋條件設定 ", padding=15, bootstyle="info")
        control_card.pack(fill=X, padx=15, pady=10)

        kw_frame = tb.Frame(control_card)
        kw_frame.pack(fill=X, pady=(0, 10))

        tb.Label(kw_frame, text="標記關鍵字 (空格分隔):",
                 font=("Microsoft JhengHei", 10, "bold")).pack(side=LEFT, padx=(0, 8))

        self.kw_entry = tb.Entry(kw_frame, font=("Microsoft JhengHei", 10))
        self.kw_entry.insert(0, self.default_keywords)
        self.kw_entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))

        tb.Button(kw_frame, text="重設關鍵字", bootstyle="outline-secondary",
                  command=self.reset_keywords).pack(side=RIGHT, padx=(0, 10))

        # 搜尋一律掃全部標案，關鍵字只用於標記與快速篩選，避免關鍵字沒涵蓋到就整筆漏抓
        self.verify_var = tk.BooleanVar(value=True)
        tb.Checkbutton(kw_frame, text="深度校驗決標方式", variable=self.verify_var,
                       bootstyle="round-toggle").pack(side=RIGHT, padx=(0, 15))

        filter_row = tb.Frame(control_card)
        filter_row.pack(fill=X)

        tb.Label(filter_row, text="日期模式:").pack(side=LEFT, padx=(0, 5))
        self.date_mode_combo = tb.Combobox(
            filter_row,
            values=[f"{core.DATE_MODE_SPDT} (現正招標中)", core.DATE_MODE_RANGE],
            width=18, state="readonly")
        self.date_mode_combo.set(f"{core.DATE_MODE_SPDT} (現正招標中)")
        self.date_mode_combo.pack(side=LEFT, padx=(0, 15))
        self.date_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_date_mode_changed())

        tb.Label(filter_row, text="查詢天數:").pack(side=LEFT, padx=(0, 5))
        self.days_combo = tb.Combobox(filter_row, values=["1 (今日)", "3", "7", "14", "30", "60"],
                                      width=8, state="readonly")
        self.days_combo.set("7")
        self.days_combo.pack(side=LEFT, padx=(0, 15))

        tb.Label(filter_row, text="採購性質:").pack(side=LEFT, padx=(0, 5))
        self.attr_combo = tb.Combobox(filter_row, values=["勞務", "不限", "財物", "工程"],
                                      width=8, state="readonly")
        self.attr_combo.set("勞務")
        self.attr_combo.pack(side=LEFT, padx=(0, 15))

        tb.Label(filter_row, text="決標方式:").pack(side=LEFT, padx=(0, 5))
        self.award_combo = tb.Combobox(filter_row, values=["最低標", "不限", "最有利標/評選"],
                                       width=12, state="readonly")
        self.award_combo.set("最低標")
        self.award_combo.pack(side=LEFT, padx=(0, 20))

        self.start_btn = tb.Button(filter_row, text="🚀 開始搜尋標案", bootstyle="success",
                                   command=self.on_start_scrape)
        self.start_btn.pack(side=RIGHT, padx=5)

        self.export_csv_btn = tb.Button(filter_row, text="📄 匯出 CSV", bootstyle="outline-primary",
                                        command=self.on_export_csv, state="disabled")
        self.export_csv_btn.pack(side=RIGHT, padx=5)

        self.export_btn = tb.Button(filter_row, text="💾 匯出 Excel", bootstyle="primary",
                                    command=self.on_export_excel, state="disabled")
        self.export_btn.pack(side=RIGHT, padx=5)

        tb.Button(filter_row, text="📂 開啟輸出資料夾", bootstyle="outline-info",
                  command=self.open_output_dir).pack(side=RIGHT, padx=5)

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
            text="提示：關鍵字只用於標記（命中關鍵字欄），不影響抓取範圍；點選欄位標題可排序，雙擊任意列開啟標案網址。",
            font=("Microsoft JhengHei", 9),
        )
        self.bottom_status.pack(side=LEFT)

        self.on_date_mode_changed()
        self._append_log("✅ 應用程式初始化完成。請點擊「開始搜尋標案」開始執行。")
        self._append_log("ℹ️ 搜尋會掃描該條件下的【全部標案】，關鍵字僅用於標記與快速篩選。")

    def setup_treeview(self, parent_frame, is_matched: bool):
        top_filter = tb.Frame(parent_frame, padding=(5, 5))
        top_filter.pack(fill=X)

        tb.Label(top_filter, text="🔍 快速篩選:").pack(side=LEFT, padx=(0, 5))
        filter_entry = tb.Entry(top_filter, width=25)
        filter_entry.pack(side=LEFT, padx=(0, 10))

        if is_matched:
            self.filter_entry_matched = filter_entry
            # 全面掃描會撈回整批勞務標案（午餐、粉刷、校外教學…），
            # 精選預設只留命中關鍵字者；未命中者沒被丟掉，勾起來即可看回。
            self.include_misses_var = tk.BooleanVar(value=False)
            tb.Checkbutton(top_filter, text="包含未命中關鍵字",
                           variable=self.include_misses_var,
                           command=self.on_include_misses_toggled,
                           bootstyle="round-toggle").pack(side=LEFT, padx=(0, 10))
        else:
            self.filter_entry_all = filter_entry

        col_ids = [c[0] for c in COLUMNS_CONFIG]
        tree = tb.Treeview(parent_frame, columns=col_ids, show="headings",
                           bootstyle="primary", selectmode="browse")

        tb.Button(top_filter, text="🔗 開啟選取標案網頁", bootstyle="outline-primary",
                  command=lambda: self.open_selected_link(tree)).pack(side=RIGHT)

        for col_id, col_name, width, align, _field in COLUMNS_CONFIG:
            tree.heading(col_id, text=col_name, anchor=align,
                         command=lambda c=col_id: self.on_sort_column(tree, c, is_matched))
            tree.column(col_id, width=width, anchor=align)

        scrollbar_y = tb.Scrollbar(parent_frame, orient="vertical", command=tree.yview)
        scrollbar_x = tb.Scrollbar(parent_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        scrollbar_y.pack(side=RIGHT, fill=Y)
        scrollbar_x.pack(side=BOTTOM, fill=X)
        tree.pack(fill=BOTH, expand=True)

        tree.bind("<Double-1>", lambda event: self.open_selected_link(tree))
        filter_entry.bind("<KeyRelease>",
                          lambda event: self.filter_treeview(tree, filter_entry.get(), is_matched))

        if is_matched:
            self.tree_matched = tree
        else:
            self.tree_all = tree

    # ==================== 執行緒安全的 UI 更新 ====================

    def _post(self, action: str, payload=None):
        """由背景執行緒呼叫：把 UI 更新排入佇列，交由主執行緒處理。"""
        self.ui_queue.put((action, payload))

    def _drain_ui_queue(self):
        """主執行緒定期取出佇列訊息並更新 widget。"""
        try:
            while True:
                action, payload = self.ui_queue.get_nowait()
                if action == "log":
                    self._append_log(payload)
                elif action == "progress":
                    self.progressbar.configure(value=payload)
                elif action == "completed":
                    self.on_scrape_completed()
                elif action == "failed":
                    self.on_scrape_failed(payload)
        except queue.Empty:
            pass
        finally:
            try:
                self.after(100, self._drain_ui_queue)
            except tk.TclError:
                pass  # 視窗已關閉，停止輪詢

    def _append_log(self, message: str):
        """僅限主執行緒呼叫。"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(END, f"[{timestamp}] {message}\n")
        self.log_text.see(END)

    def log(self, message: str):
        """可由任意執行緒呼叫。"""
        self._post("log", message)

    # ==================== 排序與篩選 ====================

    def extract_sort_key(self, item: dict, col_id: str):
        """型態感知之排序 Key 萃取器"""
        if col_id == "budget":
            return core.parse_amount(item.get("預算金額", ""))
        field = COLUMN_FIELD.get(col_id)
        if field is None:
            return str(item.get("標案案號", ""))
        return str(item.get(field, ""))

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
            state["reverse"] = col_id in ("budget", "pub_date", "deadline")

        reverse = state["reverse"]
        dataset.sort(key=lambda t: self.extract_sort_key(t, col_id), reverse=reverse)

        indicator = " ▼" if reverse else " ▲"
        for c_id, c_name, _w, _a, _f in COLUMNS_CONFIG:
            tree.heading(c_id, text=f"{c_name}{indicator}" if c_id == col_id else c_name)

        # 重新整理顯示內容（維持目前的篩選框文字）
        filter_entry = self.filter_entry_matched if is_matched else self.filter_entry_all
        self.filter_treeview(tree, filter_entry.get() if filter_entry else "", is_matched)

    def filter_treeview(self, tree, query: str, is_matched: bool):
        query = query.strip().lower()
        dataset = self.tenders_matched if is_matched else self.tenders_all
        tree.delete(*tree.get_children())

        seq = 1
        for t in dataset:
            haystack = " ".join([
                t.get("招標機關", ""), t.get("標案名稱", ""),
                t.get("標案案號", ""), t.get("命中關鍵字", ""),
            ]).lower()
            if query and query not in haystack:
                continue
            # 以 pk 作為 item id，開啟連結時可直接回查，不必比對標案名稱
            tree.insert("", END, iid=t.get("pk", f"row{seq}"), values=(
                seq,
                t.get("公告日期", ""),
                t.get("招標機關", ""),
                t.get("標案名稱", ""),
                t.get("預算金額", ""),
                t.get("決標方式", ""),
                t.get("招標方式", ""),
                t.get("決標方式來源", ""),
                t.get("截止投標", ""),
                t.get("命中關鍵字", "") or "—",
            ))
            seq += 1

    def include_misses(self) -> bool:
        """僅限主執行緒呼叫（Tkinter 變數不可跨執行緒讀取）。"""
        return bool(self.include_misses_var.get())

    def _apply_matched_dataset(self):
        """依核取方塊決定精選分頁顯示哪一份資料，並同步分頁標題。"""
        self.tenders_matched = (self.tenders_qualified if self.include_misses()
                                else self.tenders_keyword_hits)
        self._update_matched_tab_title()

    def on_include_misses_toggled(self):
        """在「條件 ∩ 關鍵字」與「全部符合條件」兩份精選之間切換顯示。"""
        self._apply_matched_dataset()

        # 保留目前的排序方向與快速篩選文字，切換後不必重按一次
        state = self.sort_state_matched
        if state["col"]:
            self.tenders_matched.sort(
                key=lambda t: self.extract_sort_key(t, state["col"]), reverse=state["reverse"])
        query = self.filter_entry_matched.get() if self.filter_entry_matched else ""
        self.filter_treeview(self.tree_matched, query, is_matched=True)

    def _update_matched_tab_title(self):
        """分頁標題把被折疊的筆數也講出來，不讓標案無聲消失。"""
        if self.include_misses():
            text = f" 🏆 精選：{self.active_filter_label} ({len(self.tenders_qualified)} 筆) "
        else:
            misses = len(self.tenders_qualified) - len(self.tenders_keyword_hits)
            text = (f" 🏆 精選：{self.active_filter_label}∩關鍵字 "
                    f"({len(self.tenders_keyword_hits)} 筆，另 {misses} 筆未命中) ")
        self.notebook.tab(0, text=text)

    def on_date_mode_changed(self):
        """等標期內模式下站方會忽略日期區間，因此把「查詢天數」停用以免誤導。"""
        if self.selected_date_type() == core.DATE_TYPE_RANGE:
            self.days_combo.configure(state="readonly")
        else:
            self.days_combo.configure(state="disabled")

    def selected_date_type(self) -> str:
        label = self.date_mode_combo.get()
        return core.DATE_TYPE_RANGE if label.startswith(core.DATE_MODE_RANGE) else core.DATE_TYPE_SPDT

    def reset_keywords(self):
        self.kw_entry.delete(0, END)
        self.kw_entry.insert(0, self.default_keywords)

    def open_selected_link(self, tree):
        selection = tree.selection()
        if not selection:
            messagebox.showinfo("提示", "請先點選欲查看的標案列！")
            return
        tender = self.tenders_by_pk.get(selection[0])
        target_url = tender.get("詳細連結") if tender else None
        if target_url:
            webbrowser.open(target_url)
        else:
            messagebox.showwarning("警告", "無法找到該標案的詳細網址！")

    def open_output_dir(self):
        if not os.path.exists(self.output_dir):
            return
        if sys.platform == "win32":
            os.startfile(self.output_dir)
        else:
            webbrowser.open(f"file://{self.output_dir}")

    # ==================== 搜尋流程 ====================

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
        date_type = self.selected_date_type()
        verify = bool(self.verify_var.get())
        include_misses = self.include_misses()

        self.active_filter_label = self._describe_filter(target_attr, target_award)

        self.is_running = True
        self.start_btn.configure(text="⏳ 搜尋中...", state="disabled", bootstyle="secondary")
        self.export_btn.configure(state="disabled")
        self.export_csv_btn.configure(state="disabled")
        self.status_badge.configure(text="搜尋中...", bootstyle="inverse-warning")
        self.progressbar.configure(value=0)

        self.tree_matched.delete(*self.tree_matched.get_children())
        self.tree_all.delete(*self.tree_all.get_children())

        threading.Thread(
            target=self.run_scrape_thread,
            args=(keywords, days, target_attr, target_award, date_type, verify, include_misses),
            daemon=True,
        ).start()

    @staticmethod
    def _describe_filter(target_attr: str, target_award: str) -> str:
        """把篩選條件組成分頁標題用的短字串。"""
        parts = [p for p in (target_attr, target_award) if p and p != "不限"]
        return "".join(parts) if parts else "全部條件"

    def run_scrape_thread(self, keywords, days, target_attr, target_award, date_type, verify,
                          include_misses=False):
        try:
            if date_type == core.DATE_TYPE_RANGE:
                days = core.clamp_date_range_days(days, log=self.log)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            start_ad = start_date.strftime("%Y/%m/%d")
            end_ad = end_date.strftime("%Y/%m/%d")
            proctrg_cate = core.PROCTRG_CATE.get(target_attr)

            if date_type == core.DATE_TYPE_RANGE:
                self.log(f"🚀 全面掃描【{target_attr}】標案：公告日期 {start_ad} ~ {end_ad} (最近 {days} 天)")
            else:
                self.log(f"🚀 全面掃描【{target_attr}】標案：等標期內（現正招標中，站方會忽略日期區間）")
            self.log(f"🏷️ 標記關鍵字共 {len(keywords)} 組: {', '.join(keywords)}")

            def _on_page(done_pages, total_pages):
                self._post("progress", int(done_pages / max(total_pages, 1) * SEARCH_PROGRESS_SHARE))

            rows = core.search_pcc("", start_ad, end_ad, proctrg_cate=proctrg_cate,
                                   date_type=date_type, log=self.log, progress_cb=_on_page)

            unique_tenders = {}
            core.merge_by_tender_id(unique_tenders, rows, "")
            tenders_list = list(unique_tenders.values())
            core.tag_keywords(tenders_list, keywords)

            hits = sum(1 for t in tenders_list if t.get("命中關鍵字群"))
            self.log(f"📦 掃描完畢，共 {len(tenders_list)} 筆不重複標案（其中 {hits} 筆命中關鍵字）。")

            if verify and tenders_list:
                targets = core.select_rows_for_enrichment(
                    tenders_list, target_attr, target_award,
                    require_keyword_hit=not include_misses)

                def _on_progress(done, total):
                    if done % max(1, total // 50) == 0 or done == total:
                        share = 100 - SEARCH_PROGRESS_SHARE
                        self._post("progress", SEARCH_PROGRESS_SHARE + int(done / total * share))

                if targets:
                    self.log(f"⚡ 從 {len(tenders_list)} 筆中挑出 {len(targets)} 筆候選，"
                             f"連線官方詳細頁校驗真實決標方式...")
                    stats = core.enrich_actual_award_methods(targets, progress_cb=_on_progress,
                                                             log=self.log)
                    self.log(f"✅ 校驗完成：{stats['ok']}/{stats['total']} 筆取得官方決標方式，"
                             f"其餘維持「{core.AWARD_SOURCE_ESTIMATED}」。")
            elif not verify:
                self.log(f"ℹ️ 已關閉深度校驗，決標方式全部為「{core.AWARD_SOURCE_ESTIMATED}」。")

            core.finalize_keywords(tenders_list)

            self.tenders_all = tenders_list
            self.tenders_qualified = core.filter_tenders(tenders_list, target_attr, target_award)
            self.tenders_keyword_hits = core.filter_tenders(
                tenders_list, target_attr, target_award, require_keyword_hit=True)
            self.tenders_matched = (self.tenders_qualified if include_misses
                                    else self.tenders_keyword_hits)
            self.tenders_by_pk = {t["pk"]: t for t in tenders_list}

            self.log(f"🎯 符合【{target_attr} + {target_award}】共 {len(self.tenders_qualified)} 筆，"
                     f"其中命中關鍵字 {len(self.tenders_keyword_hits)} 筆。")

            self._post("completed")

        except Exception as e:
            self.log(f"❌ 搜尋過程發生未預期錯誤: {e}")
            self._post("failed", str(e))

    def on_scrape_completed(self):
        self.is_running = False
        self.start_btn.configure(text="🚀 開始搜尋標案", state="normal", bootstyle="success")
        self.export_btn.configure(state="normal")
        self.export_csv_btn.configure(state="normal")
        self.status_badge.configure(text="搜尋完成", bootstyle="inverse-success")
        self.progressbar.configure(value=100)

        self._apply_matched_dataset()
        self.notebook.tab(1, text=f" 📋 所有搜尋標案 ({len(self.tenders_all)} 筆) ")

        self.filter_treeview(self.tree_matched, "", is_matched=True)
        self.filter_treeview(self.tree_all, "", is_matched=False)

        self._append_log(
            f"🎉 搜尋全部完成！共撈取 {len(self.tenders_all)} 筆不重複標案，"
            f"符合篩選條件 {len(self.tenders_qualified)} 筆，"
            f"其中命中關鍵字 {len(self.tenders_keyword_hits)} 筆。"
        )
        self.bottom_status.configure(
            text=f"完成！共找到 {len(self.tenders_all)} 筆標案（符合條件 {len(self.tenders_qualified)} 筆，"
                 f"命中關鍵字 {len(self.tenders_keyword_hits)} 筆）。"
                 f"未命中者可勾選精選分頁的「包含未命中關鍵字」查看。"
        )
        self.auto_export_backup()

    def on_scrape_failed(self, err_msg):
        self.is_running = False
        self.start_btn.configure(text="🚀 開始搜尋標案", state="normal", bootstyle="success")
        self.status_badge.configure(text="失敗", bootstyle="inverse-danger")
        messagebox.showerror("錯誤", f"搜尋發生錯誤：\n{err_msg}")

    # ==================== 匯出 ====================

    def auto_export_backup(self):
        if not (core.HAS_PANDAS and self.tenders_all):
            return
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            excel_path = os.path.join(self.output_dir, f"pcc_tenders_{timestamp}.xlsx")
            core.write_excel_report(excel_path, self.tenders_all, self.tenders_matched)
            self._append_log(f"💾 自動備份 Excel 已儲存至: {excel_path}")
        except Exception as e:
            self._append_log(f"  ⚠️ 自動儲存備份失敗: {e}")

    def _ask_save_path(self, extension: str, description: str) -> str:
        if not self.tenders_all:
            messagebox.showwarning("警告", "目前無任何搜尋資料可供匯出！")
            return ""
        return filedialog.asksaveasfilename(
            initialdir=self.output_dir,
            initialfile=f"政府採購網_標案搜尋_{datetime.now().strftime('%Y%m%d_%H%M%S')}{extension}",
            defaultextension=extension,
            filetypes=[(description, f"*{extension}"), ("所有檔案", "*.*")],
        )

    def on_export_excel(self):
        file_path = self._ask_save_path(".xlsx", "Excel 活頁簿")
        if not file_path:
            return
        try:
            core.write_excel_report(file_path, self.tenders_all, self.tenders_matched)
            messagebox.showinfo("匯出成功", f"標案資料已成功匯出至：\n{file_path}")
        except Exception as e:
            messagebox.showerror("匯出失敗", f"匯出過程發生錯誤：\n{e}")

    def on_export_csv(self):
        file_path = self._ask_save_path(".csv", "CSV 檔案")
        if not file_path:
            return
        try:
            rows = self.tenders_matched or self.tenders_all
            core.write_csv_report(file_path, rows)
            messagebox.showinfo("匯出成功", f"已匯出 {len(rows)} 筆標案至：\n{file_path}")
        except Exception as e:
            messagebox.showerror("匯出失敗", f"匯出過程發生錯誤：\n{e}")


def main():
    app = PCCScraperApp()
    app.mainloop()


if __name__ == "__main__":
    main()
