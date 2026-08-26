# -*- coding: utf-8 -*-
"""
政府電子採購網 - AI 與資訊勞務最低標標案爬蟲 GUI 應用程式

使用 ttkbootstrap 建構桌面介面，支援背景執行緒搜尋、官方詳細頁決標方式校驗、
表格多欄位型態感知排序（金額／日期／字串）、即時進度與 Excel / CSV 匯出。

官網對詳細頁採額度制（每輪約 5 筆就會要求驗證碼），一次搜尋校驗不完整份清單。
因此每輪免費確認到的那幾筆都會寫入 output/award_cache.json，下次搜尋自動套用——
不需要使用者做任何事，確認結果會跨次執行慢慢累積；還沒確認的列在畫面上明確標示。

爬取與解析邏輯共用自 pcc_core，與 CLI 版 crawler.py 為同一份實作。
"""

import os
import queue
import re
import sys
import threading
import traceback
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

# 列的標示色：決標方式尚未經官方詳細頁確認 / 已確認但不符篩選條件
PENDING_COLOR = "#c25e00"
DISQUALIFIED_COLOR = "#9aa0a6"


class PCCScraperApp(tb.Window):
    def __init__(self):
        super().__init__(themename="cosmo")
        self.title("政府電子採購網 (PCC) - AI 與資訊勞務最低標標案爬蟲")
        self.geometry("1180x820")
        self.minsize(980, 680)

        self.is_running = False
        # 使用者按下「停止搜尋」時設起來，由 pcc_core 的 should_stop 回呼讀取。
        # 全面掃描動輒 120 頁要跑好幾分鐘，條件設錯不該只能等它跑完或強制關閉。
        self.cancel_event = threading.Event()
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
        self._tooltip = None
        self._tooltip_row = None

        # 背景執行緒 -> 主執行緒的訊息佇列（Tkinter 僅能於主執行緒操作 widget）
        self.ui_queue = queue.Queue()

        # 目前套用的篩選條件，用於分頁標題文字與「已確認但不符條件」的判定
        self.active_filter_label = "勞務最低標"
        self.active_award_target = "最低標"
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

        # 條件與動作分兩列：四個下拉與四顆按鈕擠同一列時，
        # 視窗一縮到最小寬度就會擠成一團，「開始搜尋」也和次要按鈕視覺等重。
        filter_row = tb.Frame(control_card)
        filter_row.pack(fill=X, pady=(0, 10))

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

        action_row = tb.Frame(control_card)
        action_row.pack(fill=X)

        # 主要動作獨立一顆、加大、置左；次要動作一律 outline 靠右，視覺上分層
        self.start_btn = tb.Button(action_row, text="開始搜尋標案  (F5)", bootstyle="success",
                                   width=18, command=self.on_start_or_cancel)
        self.start_btn.pack(side=LEFT, ipady=4)

        tb.Button(action_row, text="開啟輸出資料夾", bootstyle="outline-secondary",
                  command=self.open_output_dir).pack(side=RIGHT, padx=(5, 0))

        self.export_csv_btn = tb.Button(action_row, text="匯出 CSV", bootstyle="outline-primary",
                                        command=self.on_export_csv, state="disabled")
        self.export_csv_btn.pack(side=RIGHT, padx=5)

        self.export_btn = tb.Button(action_row, text="匯出 Excel  (Ctrl+E)",
                                    bootstyle="outline-primary",
                                    command=self.on_export_excel, state="disabled")
        self.export_btn.pack(side=RIGHT, padx=5)

        progress_frame = tb.Frame(self, padding=(15, 0))
        progress_frame.pack(fill=X)

        self.progressbar = tb.Progressbar(progress_frame, mode="determinate", bootstyle="info-striped")
        self.progressbar.pack(fill=X, pady=(0, 5))

        # 常駐警告列：待確認筆數、被驗證碼擋下、翻頁被截斷這類訊息本來只寫進
        # 「執行紀錄」分頁，但使用者九成時間都在看「精選」分頁，等於看不到。
        # 沒有警告時整條收起來，不佔版面。
        self.notice_frame = tb.Frame(self, padding=(15, 0))
        self.notice_label = tb.Label(self.notice_frame, text="", bootstyle="inverse-warning",
                                     font=("Microsoft JhengHei", 9), padding=(10, 5),
                                     anchor="w", wraplength=1100, justify=LEFT)
        self.notice_label.pack(fill=X)
        self.notices = []

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
            text="F5 搜尋／停止　·　Esc 停止或清除篩選　·　Ctrl+F 跳到篩選框　·　Ctrl+E 匯出 Excel"
                 "　·　滑過任一列看完整內容，雙擊開啟官方頁面",
            font=("Microsoft JhengHei", 9),
        )
        self.bottom_status.pack(side=LEFT)

        restored = self.restore_settings()
        self.on_date_mode_changed()
        self.bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._append_log("✅ 應用程式初始化完成。請點擊「開始搜尋標案」開始執行。")
        self._append_log("ℹ️ 搜尋會掃描該條件下的【全部標案】，關鍵字僅用於標記與快速篩選。")
        if restored:
            self._append_log(f"⚙️ 已還原上次的搜尋條件（{core.SETTINGS_FILENAME}）。")

    # ==================== 表格 tooltip ====================

    def on_tree_hover(self, tree, event):
        row_id = tree.identify_row(event.y)
        if not row_id or row_id not in self.tenders_by_pk:
            self.hide_tooltip()
            return
        if row_id == self._tooltip_row:
            return
        self._tooltip_row = row_id
        self.show_tooltip(self.tooltip_text(self.tenders_by_pk[row_id]),
                          event.x_root + 16, event.y_root + 18)

    def tooltip_text(self, tender: dict) -> str:
        """把被欄寬截掉、以及使用者投標前真的需要知道的資訊湊成一段。"""
        source = tender.get("決標方式來源", "")
        award = tender.get("決標方式", "")
        if self.is_award_pending(tender):
            award = f"{award}  ← 推估值，官網尚未確認"
        lines = [
            tender.get("標案名稱", ""),
            "",
            f"機關：{tender.get('招標機關', '')}",
            f"案號：{tender.get('標案案號', '')}",
            f"預算：{tender.get('預算金額', '')}",
            f"招標方式：{tender.get('招標方式', '')}",
            f"決標方式：{award}（{source}）",
            f"截止投標：{tender.get('截止投標', '')}",
        ]
        keywords = tender.get("命中關鍵字", "")
        if keywords:
            lines.append(f"命中關鍵字：{keywords}")
        lines += ["", "雙擊開啟官方頁面"]
        return "\n".join(lines)

    def show_tooltip(self, text: str, x: int, y: int):
        if self._tooltip is None:
            self._tooltip = tk.Toplevel(self)
            self._tooltip.wm_overrideredirect(True)
            self._tooltip.attributes("-topmost", True)
            self._tooltip_label = tk.Label(
                self._tooltip, justify="left", anchor="w",
                background="#ffffe0", foreground="#222222",
                relief="solid", borderwidth=1, padx=8, pady=6,
                font=("Microsoft JhengHei", 9), wraplength=460)
            self._tooltip_label.pack()
        self._tooltip_label.configure(text=text)
        self._tooltip.wm_geometry(f"+{x}+{y}")
        self._tooltip.deiconify()

    def hide_tooltip(self):
        self._tooltip_row = None
        if self._tooltip is not None:
            self._tooltip.withdraw()

    def bind_shortcuts(self):
        """
        每天要用的工具沒有快捷鍵很吃虧。綁在最上層視窗，任何分頁都有效。

        Entry 內的 Ctrl+F / Esc 也要能用，因此回呼一律回傳 "break"
        阻止事件再往預設處理跑。
        """
        def _wrap(callback):
            def _handler(_event=None):
                callback()
                return "break"
            return _handler

        self.bind("<F5>", _wrap(self.on_start_or_cancel))
        self.bind("<Escape>", _wrap(self.on_escape))
        self.bind("<Control-f>", _wrap(self.focus_filter_entry))
        self.bind("<Control-F>", _wrap(self.focus_filter_entry))
        self.bind("<Control-e>", _wrap(self.on_export_excel))
        self.bind("<Control-E>", _wrap(self.on_export_excel))
        # 關鍵字打完直接 Enter 就開始，不必再把手移到滑鼠
        self.kw_entry.bind("<Return>", _wrap(self.on_start_scrape))

    def active_filter_entry(self):
        """目前顯示中的那個分頁的快速篩選框（執行紀錄分頁沒有）。"""
        index = self.notebook.index(self.notebook.select())
        return {0: self.filter_entry_matched, 1: self.filter_entry_all}.get(index)

    def focus_filter_entry(self):
        entry = self.active_filter_entry()
        if entry is not None:
            entry.focus_set()
            entry.select_range(0, END)

    def on_escape(self):
        """搜尋中就是停止；否則清掉目前分頁的快速篩選。"""
        if self.is_running:
            self.on_cancel_scrape()
            return
        entry = self.active_filter_entry()
        if entry is not None and entry.get():
            entry.delete(0, END)
            is_matched = self.notebook.index(self.notebook.select()) == 0
            tree = self.tree_matched if is_matched else self.tree_all
            self.filter_treeview(tree, "", is_matched=is_matched)

    # ==================== 使用者設定的存讀 ====================
    #
    # 這是每天要用的工具，每次開啟都把關鍵字與篩選條件重打一次很煩，
    # 因此把上次用的條件記在輸出資料夾裡，下次開啟直接還原。

    def settings_path(self) -> str:
        return core.settings_path(self.output_dir)

    def current_settings(self) -> dict:
        """僅限主執行緒呼叫（Tkinter 變數不可跨執行緒讀取）。"""
        return {
            "keywords": self.kw_entry.get(),
            "date_mode": self.date_mode_combo.get(),
            "days": self.days_combo.get(),
            "attr": self.attr_combo.get(),
            "award": self.award_combo.get(),
            "verify": bool(self.verify_var.get()),
            "include_misses": bool(self.include_misses_var.get()),
            "hide_pending": bool(self.hide_pending_var.get()),
        }

    def restore_settings(self) -> bool:
        """
        套用上次存下的搜尋條件；回傳是否真的還原了東西。

        下拉選單一律檢查值仍在選項內才套用——選項改版或設定檔被手改過時，
        寧可退回預設，也不要讓 Combobox 停在一個送出去會查不到東西的值。
        """
        saved = core.load_json_dict(self.settings_path())
        if not saved:
            return False

        keywords = saved.get("keywords")
        if isinstance(keywords, str) and keywords.strip():
            self.kw_entry.delete(0, END)
            self.kw_entry.insert(0, keywords)

        for key, combo in (("date_mode", self.date_mode_combo), ("days", self.days_combo),
                           ("attr", self.attr_combo), ("award", self.award_combo)):
            value = saved.get(key)
            if value in combo.cget("values"):
                combo.set(value)

        for key, var in (("verify", self.verify_var),
                         ("include_misses", self.include_misses_var),
                         ("hide_pending", self.hide_pending_var)):
            if isinstance(saved.get(key), bool):
                var.set(saved[key])

        self._apply_matched_dataset()
        return True

    def save_settings(self):
        """存檔失敗不該影響使用者做事，因此只記一行 log。"""
        try:
            core.save_json_dict(self.current_settings(), self.settings_path())
        except Exception as e:
            self._append_log(f"  ⚠️ 儲存搜尋條件失敗: {e}")

    def on_close(self):
        self.save_settings()
        self.destroy()

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
            # 「公開取得」的決標方式在搜尋結果頁看不出來，未校驗前只是推估；
            # 這個開關讓使用者一鍵把還沒確認的那些收起來，只看比較可信的列。
            self.hide_pending_var = tk.BooleanVar(value=False)
            tb.Checkbutton(top_filter, text="隱藏待確認",
                           variable=self.hide_pending_var,
                           command=self.on_hide_pending_toggled,
                           bootstyle="round-toggle").pack(side=LEFT, padx=(0, 10))
        else:
            self.filter_entry_all = filter_entry

        col_ids = [c[0] for c in COLUMNS_CONFIG]
        tree = tb.Treeview(parent_frame, columns=col_ids, show="headings",
                           bootstyle="primary", selectmode="browse")
        tree.tag_configure("unconfirmed", foreground=PENDING_COLOR)
        tree.tag_configure("disqualified", foreground=DISQUALIFIED_COLOR)

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

        # 欄寬固定，長標案名一定會被截斷；滑過去顯示完整內容，免得非開瀏覽器不可
        tree.bind("<Motion>", lambda event: self.on_tree_hover(tree, event))
        tree.bind("<Leave>", lambda _event: self.hide_tooltip())
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
                elif action == "notice":
                    self.add_notice(payload)
        except queue.Empty:
            pass
        finally:
            try:
                self.after(100, self._drain_ui_queue)
            except tk.TclError:
                pass  # 視窗已關閉，停止輪詢

    # ==================== 常駐警告列 ====================

    def add_notice(self, message: str):
        """僅限主執行緒：把一則需要使用者看到的警告釘在畫面上。"""
        if message in self.notices:
            return
        self.notices.append(message)
        self.notice_label.configure(
            text="  ".join(f"⚠️ {m}" for m in self.notices))
        # 用 winfo_manager() 而非 winfo_ismapped()：視窗被最小化或尚未顯示時
        # ismapped 一律是 0，會導致重複 pack 或收不起來。
        if not self.notice_frame.winfo_manager():
            self.notice_frame.pack(fill=X, before=self.notebook)

    def clear_notices(self):
        self.notices = []
        self.notice_label.configure(text="")
        if self.notice_frame.winfo_manager():
            self.notice_frame.pack_forget()

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

    @staticmethod
    def row_values(tender: dict, seq) -> tuple:
        """欄位順序必須與 COLUMNS_CONFIG 一致；單筆校驗後也走同一份實作重繪。"""
        return (
            seq,
            tender.get("公告日期", ""),
            tender.get("招標機關", ""),
            tender.get("標案名稱", ""),
            tender.get("預算金額", ""),
            tender.get("決標方式", ""),
            tender.get("招標方式", ""),
            tender.get("決標方式來源", ""),
            tender.get("截止投標", ""),
            tender.get("命中關鍵字", "") or "—",
        )

    def row_tags(self, tender: dict) -> tuple:
        """
        用顏色講清楚每一列的可信度：
        橘色＝決標方式還沒經官方詳細頁確認（「公開取得」在搜尋結果頁看不出決標方式）；
        灰色＝已確認但結果不符目前的決標方式條件，留著只是讓使用者知道它被排除了。
        """
        if not core.is_award_confirmed(tender):
            return ("unconfirmed",) if self.is_award_pending(tender) else ()
        # 沿用 filter_tenders 判定，才不會在這裡另外硬編一套「什麼算符合」
        matched = core.filter_tenders([tender], "不限", self.active_award_target)
        return () if matched else ("disqualified",)

    @staticmethod
    def is_award_pending(tender: dict) -> bool:
        """該筆的決標方式是否仍是「公開取得 (待確認)」這種無法判定的推估值。"""
        return (not core.is_award_confirmed(tender)
                and "待確認" in tender.get("決標方式", ""))

    def hide_pending(self) -> bool:
        """僅限主執行緒呼叫（Tkinter 變數不可跨執行緒讀取）。"""
        return bool(self.hide_pending_var.get())

    def filter_treeview(self, tree, query: str, is_matched: bool):
        query = query.strip().lower()
        dataset = self.tenders_matched if is_matched else self.tenders_all
        hide_pending = is_matched and self.hide_pending()
        tree.delete(*tree.get_children())

        seq = 1
        for t in dataset:
            if hide_pending and self.is_award_pending(t):
                continue
            haystack = " ".join([
                t.get("招標機關", ""), t.get("標案名稱", ""),
                t.get("標案案號", ""), t.get("命中關鍵字", ""),
            ]).lower()
            if query and query not in haystack:
                continue
            # 以 pk 作為 item id，開啟連結時可直接回查，不必比對標案名稱
            tree.insert("", END, iid=t.get("pk", f"row{seq}"),
                        values=self.row_values(t, seq), tags=self.row_tags(t))
            seq += 1

    def include_misses(self) -> bool:
        """僅限主執行緒呼叫（Tkinter 變數不可跨執行緒讀取）。"""
        return bool(self.include_misses_var.get())

    def _apply_matched_dataset(self):
        """依核取方塊決定精選分頁顯示哪一份資料，並同步分頁標題。"""
        self.tenders_matched = (self.tenders_qualified if self.include_misses()
                                else self.tenders_keyword_hits)
        self._update_matched_tab_title()

    def on_hide_pending_toggled(self):
        """切換是否隱藏決標方式尚未確認的列（只影響精選分頁的顯示）。"""
        query = self.filter_entry_matched.get() if self.filter_entry_matched else ""
        self.filter_treeview(self.tree_matched, query, is_matched=True)

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

    def award_cache_path(self) -> str:
        """已確認決標方式的快取檔位置（搜尋時自動讀寫，不需使用者操作）。"""
        return core.award_cache_path(self.output_dir)

    def open_output_dir(self):
        if not os.path.exists(self.output_dir):
            return
        if sys.platform == "win32":
            os.startfile(self.output_dir)
        else:
            webbrowser.open(f"file://{self.output_dir}")

    # ==================== 搜尋流程 ====================

    def on_start_or_cancel(self):
        """同一顆按鈕：閒置時開始搜尋，搜尋中則要求停止。"""
        if self.is_running:
            self.on_cancel_scrape()
        else:
            self.on_start_scrape()

    def on_cancel_scrape(self):
        if not self.is_running or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.start_btn.configure(text="停止中...", state="disabled")
        self.status_badge.configure(text="停止中...", bootstyle="inverse-secondary")
        self._append_log("⏹ 已要求停止，正在收尾（已抓到的資料會保留）...")

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
        self.active_award_target = target_award
        self.save_settings()

        self.is_running = True
        self.cancel_event.clear()
        self.clear_notices()
        self.start_btn.configure(text="停止搜尋  (Esc)", bootstyle="danger", state="normal")
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
                                   date_type=date_type, log=self.log, progress_cb=_on_page,
                                   should_stop=self.cancel_event.is_set)

            unique_tenders = {}
            core.merge_by_tender_id(unique_tenders, rows, "")
            tenders_list = list(unique_tenders.values())
            core.tag_keywords(tenders_list, keywords)

            hits = sum(1 for t in tenders_list if t.get("命中關鍵字群"))
            self.log(f"📦 掃描完畢，共 {len(tenders_list)} 筆不重複標案（其中 {hits} 筆命中關鍵字）。")

            # 先套快取：過去確認過的標案不必再花官網那少得可憐的詳細頁額度
            cache_path = self.award_cache_path()
            cache = core.load_award_cache(cache_path)
            applied = core.apply_award_cache(tenders_list, cache)
            if applied:
                self.log(f"♻️ 由快取套用 {applied} 筆先前已確認的官方決標方式（不花額度）。")

            if self.cancel_event.is_set():
                # 使用者按了停止：已抓到的照樣整理出來，但不再花時間連詳細頁
                self.log("⏹ 已停止搜尋，以下為中斷前取得的部分結果。")
                verify = False

            if verify and tenders_list:
                targets = core.select_rows_for_enrichment(
                    tenders_list, target_attr, target_award,
                    require_keyword_hit=not include_misses)

                def _on_progress(done, total):
                    if done % max(1, total // 50) == 0 or done == total:
                        share = 100 - SEARCH_PROGRESS_SHARE
                        self._post("progress", SEARCH_PROGRESS_SHARE + int(done / total * share))

                if targets:
                    self.log(f"⚡ 從 {len(tenders_list)} 筆中挑出 {len(targets)} 筆尚未確認的候選，"
                             f"連線官方詳細頁校驗真實決標方式...")
                    stats = core.enrich_actual_award_methods(
                        targets, progress_cb=_on_progress, log=self.log,
                        cache=cache, cache_path=cache_path,
                        should_stop=self.cancel_event.is_set)
                    if stats["blocked"]:
                        self._post("notice",
                                   f"官網詳細頁額度已用盡，本次只確認 {stats['ok']} 筆。"
                                   f"已確認的都寫進快取了，下次搜尋會直接套用。")
                        self.log(f"⛔ 校驗提前中止：本次確認 {stats['ok']} 筆，官網額度已用盡。"
                                 f"已確認的都已寫入快取，下次搜尋會直接套用。")
                    else:
                        self.log(f"✅ 校驗完成：本次確認 {stats['ok']}/{stats['total']} 筆，"
                                 f"其餘維持「{core.AWARD_SOURCE_ESTIMATED}」。")
            elif not verify:
                self.log(f"ℹ️ 已關閉深度校驗，未快取的標案決標方式全部為「{core.AWARD_SOURCE_ESTIMATED}」。")

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

            pending = sum(1 for t in self.tenders_qualified if self.is_award_pending(t))
            if pending:
                self.log(f"⚠️ 精選中有 {pending} 筆決標方式仍是「公開取得 (待確認)」（橘色列）——"
                         f"官網搜尋結果頁看不出決標方式，實測這類標案有相當比例其實是最有利標。"
                         f"詳細頁每輪只給約 {core.CAPTCHA_STREAK_LIMIT} 筆額度，無法一次查完；"
                         f"每次搜尋會自動補幾筆進快取，要立刻確認某一筆請雙擊該列到官方頁面查看。")
                self._post("notice",
                           f"精選中有 {pending} 筆決標方式是推估的（橘色列），"
                           f"這類「公開取得」標案有相當比例其實是最有利標——投標前請雙擊該列確認。")

            if self.cancel_event.is_set():
                self._post("notice", "本次搜尋被中斷，清單只涵蓋中斷前抓到的部分標案。")

            self._post("completed")

        except Exception as e:
            # 只印 str(e) 會讓「哪一行爆的」完全消失，出事時無從查起；
            # 完整 traceback 進「執行紀錄」分頁，對話框仍只給一行摘要。
            self.log(f"❌ 搜尋過程發生未預期錯誤: {e.__class__.__name__}: {e}")
            self.log(traceback.format_exc().rstrip())
            self._post("failed", f"{e.__class__.__name__}: {e}")

    def on_scrape_completed(self):
        self.is_running = False
        cancelled = self.cancel_event.is_set()
        self.cancel_event.clear()
        self.start_btn.configure(text="開始搜尋標案  (F5)", state="normal", bootstyle="success")
        self.export_btn.configure(state="normal")
        self.export_csv_btn.configure(state="normal")
        if cancelled:
            self.status_badge.configure(text="已停止", bootstyle="inverse-secondary")
        else:
            self.status_badge.configure(text="搜尋完成", bootstyle="inverse-success")
        self.progressbar.configure(value=100)

        self._apply_matched_dataset()
        self.notebook.tab(1, text=f" 📋 所有搜尋標案 ({len(self.tenders_all)} 筆) ")

        self.filter_treeview(self.tree_matched, "", is_matched=True)
        self.filter_treeview(self.tree_all, "", is_matched=False)

        headline = "已停止" if cancelled else "搜尋全部完成"
        self._append_log(
            f"🎉 {headline}！共撈取 {len(self.tenders_all)} 筆不重複標案，"
            f"符合篩選條件 {len(self.tenders_qualified)} 筆，"
            f"其中命中關鍵字 {len(self.tenders_keyword_hits)} 筆。"
        )
        self.bottom_status.configure(
            text=f"{headline}：共 {len(self.tenders_all)} 筆標案，符合條件 {len(self.tenders_qualified)} 筆，"
                 f"命中關鍵字 {len(self.tenders_keyword_hits)} 筆"
                 f"　·　F5 重新搜尋　·　Ctrl+F 篩選　·　Ctrl+E 匯出"
        )
        self.auto_export_backup()

    def on_scrape_failed(self, err_msg):
        self.is_running = False
        self.cancel_event.clear()
        self.start_btn.configure(text="開始搜尋標案  (F5)", state="normal", bootstyle="success")
        self.status_badge.configure(text="失敗", bootstyle="inverse-danger")
        self.add_notice("上次搜尋失敗，完整錯誤訊息在「執行紀錄」分頁。")
        messagebox.showerror("錯誤", f"搜尋發生錯誤：\n{err_msg}\n\n"
                                     f"完整 traceback 已寫入「執行紀錄」分頁。")

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
            self._append_log(f"  ⚠️ 自動儲存備份失敗: {e.__class__.__name__}: {e}")
            self._append_log(traceback.format_exc().rstrip())

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
            self._append_log(f"❌ 匯出失敗: {e.__class__.__name__}: {e}")
            self._append_log(traceback.format_exc().rstrip())
            messagebox.showerror(
                "匯出失敗",
                f"匯出過程發生錯誤：\n{e}\n\n完整錯誤訊息已寫入「執行紀錄」分頁。")

    def on_export_csv(self):
        file_path = self._ask_save_path(".csv", "CSV 檔案")
        if not file_path:
            return
        try:
            rows = self.tenders_matched or self.tenders_all
            core.write_csv_report(file_path, rows)
            messagebox.showinfo("匯出成功", f"已匯出 {len(rows)} 筆標案至：\n{file_path}")
        except Exception as e:
            self._append_log(f"❌ 匯出失敗: {e.__class__.__name__}: {e}")
            self._append_log(traceback.format_exc().rstrip())
            messagebox.showerror(
                "匯出失敗",
                f"匯出過程發生錯誤：\n{e}\n\n完整錯誤訊息已寫入「執行紀錄」分頁。")


def main():
    app = PCCScraperApp()
    app.mainloop()


if __name__ == "__main__":
    main()
