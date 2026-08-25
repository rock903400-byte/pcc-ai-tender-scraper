# -*- coding: utf-8 -*-
"""
GUI 層的煙霧測試。

這些測試會真的建立 Tk 視窗，需要可用的桌面工作階段，因此預設跳過；
在本機桌面環境下以環境變數開啟：

    PCC_GUI_TESTS=1 python -m pytest tests/ -v

核心爬取與解析邏輯不依賴 GUI，已由 test_pcc_core.py 完整涵蓋，
CI（無顯示環境的 Linux runner）僅執行該部分。

註：ttkbootstrap 的 Style 是行程層級的單例，會綁定第一個建立的視窗，
    視窗銷毀後就無法再建立第二個，因此整個模組共用同一個視窗實例。
"""

import os
import webbrowser

import pytest

if os.environ.get("PCC_GUI_TESTS") != "1":
    pytest.skip("未設定 PCC_GUI_TESTS=1，跳過需要桌面環境的 GUI 測試",
                allow_module_level=True)

pytest.importorskip("ttkbootstrap")
pytest.importorskip("tkinter")

import pcc_core as core
import app as gui


SAMPLE = [
    {"pk": "PK1", "標案案號": "A1", "標案名稱": "甲案", "招標機關": "機關甲",
     "預算金額": "9,000 元", "決標方式": "最低標", "招標方式": "公開招標",
     "決標方式來源": core.AWARD_SOURCE_OFFICIAL, "公告日期": "2026/08/01",
     "截止投標": "2026/08/20", "命中關鍵字": "AI", "詳細連結": "https://example.invalid/1",
     "採購性質": "勞務類", "是否為最低標": "是"},
    {"pk": "PK2", "標案案號": "A2", "標案名稱": "乙案", "招標機關": "機關乙",
     "預算金額": "21,000,000 元", "決標方式": "最有利標", "招標方式": "公開評選",
     "決標方式來源": core.AWARD_SOURCE_ESTIMATED, "公告日期": "2026/08/24",
     "截止投標": "2026/09/03", "命中關鍵字": "資訊", "詳細連結": "https://example.invalid/2",
     "採購性質": "勞務類", "是否為最低標": "否"},
]


@pytest.fixture(scope="module")
def app_window(tmp_path_factory):
    """整個模組共用的單一視窗（ttkbootstrap 每個行程只能建立一個）。"""
    workdir = tmp_path_factory.mktemp("gui")
    previous_cwd = os.getcwd()
    os.chdir(workdir)
    try:
        window = gui.PCCScraperApp()
        window.withdraw()
        yield window
        window.destroy()
    finally:
        os.chdir(previous_cwd)


@pytest.fixture
def window(app_window):
    """每個測試前把視窗的資料與表格狀態重設乾淨。"""
    app_window.tenders_all = [dict(t) for t in SAMPLE]
    app_window.tenders_matched = app_window.tenders_all[:1]
    app_window.tenders_by_pk = {t["pk"]: t for t in app_window.tenders_all}
    app_window.sort_state_all = {"col": None, "reverse": False}
    app_window.sort_state_matched = {"col": None, "reverse": False}
    for col_id, name, _w, _a, _f in gui.COLUMNS_CONFIG:
        app_window.tree_all.heading(col_id, text=name)
        app_window.tree_matched.heading(col_id, text=name)
    app_window.filter_treeview(app_window.tree_all, "", is_matched=False)
    return app_window


def test_every_column_maps_to_a_real_field():
    """欄位定義的資料鍵必須都存在於實際資料中，否則表格會出現空欄。"""
    for _col_id, _name, _w, _a, field in gui.COLUMNS_CONFIG:
        if field is not None:
            assert field in SAMPLE[0], f"欄位 {field} 不存在於標案資料中"


def test_default_keywords_come_from_config(window):
    """關鍵字預設值必須來自 config.py，不可另外硬編一份。"""
    from config import DEFAULT_KEYWORDS
    assert window.kw_entry.get() == " ".join(DEFAULT_KEYWORDS)


def test_rows_use_pk_as_item_id(window):
    assert list(window.tree_all.get_children()) == ["PK1", "PK2"]


def test_row_values_cover_every_column(window):
    values = window.tree_all.item("PK1", "values")
    assert len(values) == len(gui.COLUMNS_CONFIG)
    assert core.AWARD_SOURCE_OFFICIAL in values


def test_duplicate_titles_still_resolve_to_distinct_links(window):
    """迴歸測試：兩筆標案同名時，開啟連結不可再靠標案名稱比對。"""
    for tender in window.tenders_all:
        tender["標案名稱"] = "同名標案"
    window.filter_treeview(window.tree_all, "", is_matched=False)

    opened = []
    original = webbrowser.open
    webbrowser.open = opened.append
    try:
        window.tree_all.selection_set("PK2")
        window.open_selected_link(window.tree_all)
    finally:
        webbrowser.open = original

    assert opened == ["https://example.invalid/2"]


def test_filter_matches_org(window):
    window.filter_treeview(window.tree_all, "機關乙", is_matched=False)
    assert list(window.tree_all.get_children()) == ["PK2"]
    window.filter_treeview(window.tree_all, "", is_matched=False)
    assert len(window.tree_all.get_children()) == 2


def test_budget_sorts_numerically(window):
    """字串排序會把 9,000 排在 21,000,000 之後，數值排序不會。"""
    window.on_sort_column(window.tree_all, "budget", is_matched=False)
    assert [t["標案案號"] for t in window.tenders_all] == ["A2", "A1"]
    window.on_sort_column(window.tree_all, "budget", is_matched=False)
    assert [t["標案案號"] for t in window.tenders_all] == ["A1", "A2"]


def test_sort_indicator_shown_on_active_column_only(window):
    window.on_sort_column(window.tree_all, "org", is_matched=False)
    assert window.tree_all.heading("org", "text").endswith("▲")
    assert window.tree_all.heading("title", "text") == "標案名稱"


def test_sorting_preserves_active_filter(window):
    window.filter_entry_all.delete(0, "end")
    window.filter_entry_all.insert(0, "機關乙")
    window.on_sort_column(window.tree_all, "org", is_matched=False)
    assert list(window.tree_all.get_children()) == ["PK2"]
    window.filter_entry_all.delete(0, "end")


def test_log_from_worker_thread_reaches_widget(window):
    """log() 由背景執行緒呼叫時，必須經佇列在主執行緒寫入 widget。"""
    import threading

    thread = threading.Thread(target=window.log, args=("背景執行緒訊息",))
    thread.start()
    thread.join()

    window._drain_ui_queue()
    window.update()
    assert "背景執行緒訊息" in window.log_text.get("1.0", "end")


@pytest.mark.parametrize("attr, award, expected", [
    ("勞務", "最低標", "勞務最低標"),
    ("工程", "最有利標/評選", "工程最有利標/評選"),
    ("不限", "最低標", "最低標"),
    ("不限", "不限", "全部條件"),
])
def test_tab_label_reflects_active_filter(attr, award, expected):
    """分頁標題不可再寫死「勞務最低標」。"""
    assert gui.PCCScraperApp._describe_filter(attr, award) == expected


# ==================== 全面掃描與日期模式 ====================

def test_days_combo_follows_date_mode(window):
    """等標期內模式下站方會忽略日期，查詢天數必須停用以免誤導使用者。"""
    window.date_mode_combo.set(f"{core.DATE_MODE_SPDT} (現正招標中)")
    window.on_date_mode_changed()
    assert str(window.days_combo.cget("state")) == "disabled"
    assert window.selected_date_type() == core.DATE_TYPE_SPDT

    window.date_mode_combo.set(core.DATE_MODE_RANGE)
    window.on_date_mode_changed()
    assert str(window.days_combo.cget("state")) == "readonly"
    assert window.selected_date_type() == core.DATE_TYPE_RANGE


def test_scrape_scans_everything_and_only_tags_keywords(window, monkeypatch):
    """
    迴歸測試：名稱不含任何關鍵字的標案（本專案的漏抓案例）
    必須照樣被抓進來，只是「命中關鍵字」為空。
    """
    captured = {}
    scanned = [
        {"pk": "PK9", "標案案號": "MY115007", "標案名稱": "桃園醫院人力資源E指通計畫採購案",
         "招標機關": "衛生福利部桃園醫院", "招標方式": "公開招標", "採購性質": "勞務類",
         "決標方式": "最低標 (公開招標)", "決標方式來源": core.AWARD_SOURCE_ESTIMATED,
         "預算金額": "1,737,200 元", "公告日期": "2026/08/13", "截止投標": "2026/08/26",
         "是否為勞務類": "是", "是否為最低標": "是", "詳細連結": "https://example.invalid/9"},
    ]

    def fake_search(keyword, start, end, **kwargs):
        captured["keyword"] = keyword
        captured.update(kwargs)
        return [dict(row) for row in scanned]

    monkeypatch.setattr(core, "search_pcc", fake_search)
    monkeypatch.setattr(core, "enrich_actual_award_methods",
                        lambda rows, **kw: {"total": len(rows), "done": len(rows),
                                            "ok": 0, "blocked": False})

    window.run_scrape_thread(["AI", "資訊"], 7, "勞務", "最低標", core.DATE_TYPE_RANGE, True)

    assert captured["keyword"] == ""
    assert captured["date_type"] == core.DATE_TYPE_RANGE
    assert [t["標案案號"] for t in window.tenders_all] == ["MY115007"]
    assert window.tenders_all[0]["命中關鍵字"] == ""
    assert [t["標案案號"] for t in window.tenders_matched] == ["MY115007"]


def test_rows_without_keyword_hits_are_marked_in_the_table(window):
    window.tenders_all[0]["命中關鍵字"] = ""
    window.filter_treeview(window.tree_all, "", is_matched=False)
    values = window.tree_all.item("PK1", "values")
    assert values[-1] == "—"
