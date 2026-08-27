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
from datetime import date

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
    app_window.table_all.rows = app_window.tenders_all
    app_window.table_all.sort_state = {"col": None, "reverse": False}
    app_window.table_matched.sort_state = {"col": None, "reverse": False}
    for col_id, name, _w, _a, _f in gui.COLUMNS_CONFIG:
        app_window.table_all.tree.heading(col_id, text=name)
        app_window.table_matched.tree.heading(col_id, text=name)
    app_window.hide_pending_var.set(False)
    app_window.active_award_target = "最低標"
    # 搜尋條件是模組共用視窗上的狀態，每個測試前要回到預設值
    app_window.reset_keywords()
    app_window.date_mode_combo.set(f"{core.DATE_MODE_SPDT} (現正招標中)")
    app_window.days_combo.set("7")
    app_window.attr_combo.set("勞務")
    app_window.award_combo.set("最低標")
    app_window.verify_var.set(True)
    app_window.include_misses_var.set(False)
    for entry in (app_window.table_all.entry, app_window.table_matched.entry):
        entry.delete(0, "end")
    # 佇列裡若殘留上個測試的 "failed"，會在別的測試 drain 時彈出 modal
    # 對話框而讓整個測試程序卡死——每個測試前一定要倒乾淨。
    while not app_window.ui_queue.empty():
        app_window.ui_queue.get_nowait()
    app_window.is_running = False
    app_window.cancel_event.clear()
    app_window.clear_notices()
    # 快取與設定都是跨次執行的持久檔，測試之間必須清乾淨才不會互相汙染
    app_window.cancel_trickle()
    app_window._trickle_busy = False
    app_window.trickle_var.set(True)
    for path in (app_window.award_cache_path(), app_window.settings_path(),
                 app_window.pending_queue_path()):
        try:
            os.remove(path)
        except OSError:
            pass
    app_window.table_all.render()
    return app_window



def set_query(table, text):
    """把快速篩選框設成 text 並重畫——使用者實際上就是這樣操作的。"""
    table.entry.delete(0, "end")
    table.entry.insert(0, text)
    table.render()

def pending_row(pk, tender_id, name="待確認案"):
    """招標方式為「公開取得」時，搜尋結果頁看不出決標方式，只能推估。"""
    desc, is_lowest = core.determine_award_method("公開取得報價單或企劃書")
    return {
        "pk": pk, "標案案號": tender_id, "標案名稱": name, "招標機關": "機關丙",
        "招標方式": "公開取得報價單或企劃書", "採購性質": "勞務類",
        "決標方式": desc, "決標方式來源": core.AWARD_SOURCE_ESTIMATED,
        "預算金額": "500,000 元", "公告日期": "2026/08/25", "截止投標": "2026/09/05",
        "命中關鍵字": "系統", "是否為勞務類": "是",
        "是否為最低標": "是" if is_lowest else "否",
        "詳細連結": f"https://example.invalid/{pk}",
    }


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
    assert list(window.table_all.tree.get_children()) == ["PK1", "PK2"]


def test_row_values_cover_every_column(window):
    values = window.table_all.tree.item("PK1", "values")
    assert len(values) == len(gui.COLUMNS_CONFIG)
    assert core.AWARD_SOURCE_OFFICIAL in values


def test_duplicate_titles_still_resolve_to_distinct_links(window):
    """迴歸測試：兩筆標案同名時，開啟連結不可再靠標案名稱比對。"""
    for tender in window.tenders_all:
        tender["標案名稱"] = "同名標案"
    window.table_all.clear_query() or window.table_all.render()

    opened = []
    original = webbrowser.open
    webbrowser.open = opened.append
    try:
        window.table_all.tree.selection_set("PK2")
        window.table_all.open_selected()
    finally:
        webbrowser.open = original

    assert opened == ["https://example.invalid/2"]


def test_filter_matches_org(window):
    set_query(window.table_all, "機關乙")
    assert list(window.table_all.tree.get_children()) == ["PK2"]
    window.table_all.clear_query() or window.table_all.render()
    assert len(window.table_all.tree.get_children()) == 2


def test_budget_sorts_numerically(window):
    """字串排序會把 9,000 排在 21,000,000 之後，數值排序不會。"""
    window.table_all.sort_by("budget")
    assert [t["標案案號"] for t in window.tenders_all] == ["A2", "A1"]
    window.table_all.sort_by("budget")
    assert [t["標案案號"] for t in window.tenders_all] == ["A1", "A2"]


def test_sort_indicator_shown_on_active_column_only(window):
    window.table_all.sort_by("org")
    assert window.table_all.tree.heading("org", "text").endswith("▲")
    assert window.table_all.tree.heading("title", "text") == "標案名稱"


def test_sorting_preserves_active_filter(window):
    window.table_all.entry.delete(0, "end")
    window.table_all.entry.insert(0, "機關乙")
    window.table_all.sort_by("org")
    assert list(window.table_all.tree.get_children()) == ["PK2"]
    window.table_all.entry.delete(0, "end")


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
        # 符合勞務+最低標，但名稱沒命中任何關鍵字 —— 不進精選，但必須留在完整清單
        {"pk": "PK8", "標案案號": "A8", "標案名稱": "內湖區湖元里115年樓梯間粉刷案",
         "招標機關": "臺北市內湖區公所", "招標方式": "公開招標", "採購性質": "勞務類",
         "決標方式": "最低標 (公開招標)", "決標方式來源": core.AWARD_SOURCE_ESTIMATED,
         "預算金額": "180,000 元", "公告日期": "2026/08/14", "截止投標": "2026/08/27",
         "是否為勞務類": "是", "是否為最低標": "是", "詳細連結": "https://example.invalid/8"},
    ]

    def fake_search(keyword, start, end, **kwargs):
        captured["keyword"] = keyword
        captured.update(kwargs)
        return [dict(row) for row in scanned]

    monkeypatch.setattr(core, "search_pcc", fake_search)
    monkeypatch.setattr(core, "enrich_actual_award_methods",
                        lambda rows, **kw: {"total": len(rows), "done": len(rows),
                                            "ok": 0, "blocked": False})

    window.include_misses_var.set(False)
    window.run_scrape_thread(["人力資源", "資訊"], 7, "勞務", "最低標",
                             core.DATE_TYPE_RANGE, True)

    assert captured["keyword"] == ""
    assert captured["date_type"] == core.DATE_TYPE_RANGE
    # 兩筆都抓進來了，但只有命中關鍵字的那筆進精選
    assert sorted(t["標案案號"] for t in window.tenders_all) == ["A8", "MY115007"]
    assert [t["標案案號"] for t in window.tenders_qualified] == ["MY115007", "A8"]
    assert [t["標案案號"] for t in window.tenders_keyword_hits] == ["MY115007"]
    # 「顯示哪一份精選」是主執行緒的決定（背景執行緒不該碰畫面狀態），
    # 所以要走完 _apply_matched_dataset 才看得到結果
    window._apply_matched_dataset()
    assert [t["標案案號"] for t in window.tenders_matched] == ["MY115007"]


def test_精選_toggle_switches_dataset_and_title(window, monkeypatch):
    """勾選「包含未命中關鍵字」要能把未命中者叫回來，標題數字同步。"""
    window.active_filter_label = "勞務最低標"
    window.tenders_qualified = [dict(t) for t in SAMPLE]
    window.tenders_keyword_hits = [window.tenders_qualified[0]]

    window.include_misses_var.set(False)
    window.on_include_misses_toggled()
    assert [t["標案案號"] for t in window.tenders_matched] == ["A1"]
    title = window.notebook.tab(0, "text")
    assert "∩關鍵字" in title and "1 筆" in title and "另 1 筆未命中" in title

    window.include_misses_var.set(True)
    window.on_include_misses_toggled()
    assert [t["標案案號"] for t in window.tenders_matched] == ["A1", "A2"]
    assert "2 筆" in window.notebook.tab(0, "text")
    assert list(window.table_matched.tree.get_children()) == ["PK1", "PK2"]


def test_精選_toggle_keeps_sort_and_filter(window):
    """切換資料集後，目前的排序方向與快速篩選文字都不該被清掉。"""
    window.tenders_qualified = [dict(t) for t in SAMPLE]
    window.tenders_keyword_hits = [dict(t) for t in SAMPLE]

    window.include_misses_var.set(True)
    window.on_include_misses_toggled()
    window.table_matched.sort_by("budget")
    assert [t["標案案號"] for t in window.tenders_matched] == ["A2", "A1"]

    window.table_matched.entry.delete(0, "end")
    window.table_matched.entry.insert(0, "機關乙")
    window.include_misses_var.set(False)
    window.on_include_misses_toggled()

    assert [t["標案案號"] for t in window.tenders_matched] == ["A2", "A1"]
    assert list(window.table_matched.tree.get_children()) == ["PK2"]


def test_rows_without_keyword_hits_are_marked_in_the_table(window):
    window.tenders_all[0]["命中關鍵字"] = ""
    window.table_all.clear_query() or window.table_all.render()
    values = window.table_all.tree.item("PK1", "values")
    assert values[-1] == "—"


# ==================== 決標方式的確認狀態 ====================

def test_pending_rows_are_marked_but_kept(window):
    """
    「公開取得 (待確認)」仍算最低標候選、不能被丟掉，
    但要用顏色講清楚它還沒經官方詳細頁確認。
    """
    pending = pending_row("PK3", "A3")
    assert window.is_award_pending(pending)
    assert window.row_tags(pending) == ("unconfirmed",)

    core.apply_award_method(pending, "最低標")
    assert not window.is_award_pending(pending)
    assert window.row_tags(pending) == ()


def test_confirmed_non_lowest_rows_are_greyed_out(window):
    window.active_award_target = "最低標"
    tender = pending_row("PK3", "A3")
    core.apply_award_method(tender, "參考最有利標精神")
    assert tender["是否為最低標"] == "否"
    assert window.row_tags(tender) == ("disqualified",)


def test_greying_follows_the_active_award_filter(window):
    """
    查「最有利標／評選」時，確認為最有利標的列才是命中的那些，
    不能沿用「不是最低標就標灰」而把使用者要的標案全部畫成排除。
    """
    tender = pending_row("PK3", "A3")
    core.apply_award_method(tender, "參考最有利標精神")

    window.active_award_target = "最有利標/評選"
    assert window.row_tags(tender) == ()

    window.active_award_target = "不限"
    assert window.row_tags(tender) == ()


def test_hide_pending_toggle_only_hides_unconfirmed(window):
    confirmed = dict(SAMPLE[0])
    pending = pending_row("PK3", "A3")
    window.tenders_matched = [confirmed, pending]

    window.hide_pending_var.set(False)
    window.table_matched.clear_query() or window.table_matched.render()
    assert list(window.table_matched.tree.get_children()) == ["PK1", "PK3"]

    window.hide_pending_var.set(True)
    window.table_matched.render()
    assert list(window.table_matched.tree.get_children()) == ["PK1"]


def test_table_stays_single_select(window):
    """
    介面上沒有需要複選的動作，維持單選才不會讓使用者以為選了一批就能做什麼。
    """
    for tree in (window.table_matched.tree, window.table_all.tree):
        assert str(tree.cget("selectmode")) == "browse"


def test_scrape_writes_confirmations_to_cache(window, monkeypatch):
    """
    使用者不做任何事，每次搜尋免費確認到的那幾筆也要落地，
    否則下次重跑又得重花一次稀缺的詳細頁額度。
    """
    scanned = [pending_row("PK3", "A3", "智慧系統維護案")]
    monkeypatch.setattr(core, "search_pcc", lambda *a, **kw: [dict(scanned[0])])
    monkeypatch.setattr(core, "DETAIL_DELAY_RANGE", (0, 0))
    monkeypatch.setattr(core, "fetch_award_method_status", lambda pk: ("最低標", "ok"))

    window.run_scrape_thread(["系統"], 7, "勞務", "最低標", core.DATE_TYPE_RANGE, True)

    assert core.load_award_cache(window.award_cache_path())["A3"]["決標方式"] == "最低標"
    assert core.is_award_confirmed(window.tenders_all[0])


def test_scrape_applies_award_cache_before_filtering(window, monkeypatch):
    """
    快取要在精選成形【之前】套用，否則已知是最有利標的標案還是會被收進精選。
    """
    scanned = [pending_row("PK3", "A3", "智慧系統維護案")]
    monkeypatch.setattr(core, "search_pcc", lambda *a, **kw: [dict(scanned[0])])

    core.save_award_cache({"A3": {"決標方式": "參考最有利標精神"}}, window.award_cache_path())
    window.include_misses_var.set(False)
    window.run_scrape_thread(["系統"], 7, "勞務", "最低標", core.DATE_TYPE_RANGE, False)

    assert window.tenders_all[0]["決標方式"] == "參考最有利標精神"
    assert window.tenders_qualified == []


def test_scrape_without_cache_keeps_pending_in_shortlist(window, monkeypatch):
    """沒有快取時「待確認」仍要留在精選，不能因為無法確認就整批漏掉。"""
    scanned = [pending_row("PK3", "A3", "智慧系統維護案")]
    monkeypatch.setattr(core, "search_pcc", lambda *a, **kw: [dict(scanned[0])])

    window.run_scrape_thread(["系統"], 7, "勞務", "最低標", core.DATE_TYPE_RANGE, False)

    assert [t["標案案號"] for t in window.tenders_qualified] == ["A3"]
    assert window.is_award_pending(window.tenders_qualified[0])


# ==================== 使用者設定的存讀 ====================

def test_settings_round_trip(window):
    """每天要用的工具，關鍵字與篩選條件不該每次開啟都要重打一次。"""
    window.kw_entry.delete(0, "end")
    window.kw_entry.insert(0, "AI 資安")
    window.date_mode_combo.set(core.DATE_MODE_RANGE)
    window.days_combo.set("30")
    window.attr_combo.set("財物")
    window.award_combo.set("不限")
    window.verify_var.set(False)
    window.hide_pending_var.set(True)
    window.save_settings()

    # 改回別的值，再還原
    window.kw_entry.delete(0, "end")
    window.kw_entry.insert(0, "別的東西")
    window.date_mode_combo.set(f"{core.DATE_MODE_SPDT} (現正招標中)")
    window.days_combo.set("7")
    window.attr_combo.set("勞務")
    window.award_combo.set("最低標")
    window.verify_var.set(True)
    window.hide_pending_var.set(False)

    assert window.restore_settings() is True
    assert window.kw_entry.get() == "AI 資安"
    assert window.date_mode_combo.get() == core.DATE_MODE_RANGE
    assert window.days_combo.get() == "30"
    assert window.attr_combo.get() == "財物"
    assert window.award_combo.get() == "不限"
    assert window.verify_var.get() is False
    assert window.hide_pending_var.get() is True


def test_restore_without_settings_file_is_a_noop(window):
    assert window.restore_settings() is False
    assert window.kw_entry.get() == window.default_keywords


def test_restore_ignores_values_no_longer_offered(window):
    """
    設定檔被手改過、或下拉選項改版時，寧可退回預設，
    也不要讓 Combobox 停在一個送出去查不到東西的值。
    """
    core.save_json_dict({"attr": "外太空", "days": "999", "keywords": "AI"},
                        window.settings_path())
    assert window.restore_settings() is True
    assert window.attr_combo.get() == "勞務"
    assert window.days_combo.get() == "7"
    assert window.kw_entry.get() == "AI"


def test_corrupt_settings_file_does_not_break_startup(window):
    with open(window.settings_path(), "w", encoding="utf-8") as f:
        f.write("{ 這不是 JSON")
    assert window.restore_settings() is False


def test_starting_a_search_saves_the_conditions(window, monkeypatch):
    """按下搜尋當下的那組條件才是使用者真的要的，不能只靠關視窗才存。"""
    monkeypatch.setattr(window, "run_scrape_thread", lambda *a, **kw: None)
    window.attr_combo.set("工程")
    try:
        window.on_start_scrape()
        assert core.load_json_dict(window.settings_path())["attr"] == "工程"
    finally:
        window.is_running = False
        window.attr_combo.set("勞務")


def test_scrape_failure_logs_the_traceback(window, monkeypatch):
    """
    只印 str(e) 會讓「哪一行爆的」完全消失。
    完整 traceback 要留在「執行紀錄」分頁，否則出事無從查起。
    """
    def _boom(*_a, **_kw):
        raise ValueError("模擬的網站改版錯誤")

    monkeypatch.setattr(core, "search_pcc", _boom)
    logs = []
    monkeypatch.setattr(window, "log", logs.append)
    window.run_scrape_thread(["AI"], 7, "勞務", "最低標", core.DATE_TYPE_RANGE, False)

    joined = chr(10).join(logs)
    assert "ValueError" in joined
    assert "模擬的網站改版錯誤" in joined
    assert "Traceback (most recent call last)" in joined
    assert "_boom" in joined


# ==================== 介面：停止、警告列、快捷鍵、tooltip ====================

def test_start_button_becomes_cancel_while_running(window, monkeypatch):
    """
    搜尋動輒好幾分鐘，同一顆按鈕在執行中必須變成「停止」，
    否則使用者條件設錯只能等它跑完或強制關閉程式。
    """
    monkeypatch.setattr(window, "run_scrape_thread", lambda *a, **kw: None)
    try:
        window.on_start_scrape()
        assert window.is_running is True
        assert "停止" in window.start_btn.cget("text")
        assert str(window.start_btn.cget("state")) == "normal"

        window.on_start_or_cancel()          # 再按一次 = 要求停止
        assert window.cancel_event.is_set()
    finally:
        window.is_running = False
        window.cancel_event.clear()


def test_cancelled_search_keeps_partial_results(window, monkeypatch):
    """按下停止後，中斷前抓到的標案要照樣整理出來，而不是整批丟掉。"""
    scanned = [pending_row("PK3", "A3", "智慧系統維護案")]

    def _fake_search(*_a, **kwargs):
        # 模擬 pcc_core：翻頁途中使用者按下停止，回傳部分結果
        window.cancel_event.set()
        assert kwargs["should_stop"]() is True    # 取消旗標確實接進 pcc_core
        return [dict(scanned[0])]

    monkeypatch.setattr(core, "search_pcc", _fake_search)
    logs = []
    monkeypatch.setattr(window, "log", logs.append)
    try:
        window.run_scrape_thread(["系統"], 7, "勞務", "最低標", core.DATE_TYPE_RANGE, True)
        assert [t["標案案號"] for t in window.tenders_all] == ["A3"]
        assert any("已停止搜尋" in m for m in logs)
    finally:
        window.cancel_event.clear()


def test_notice_bar_shows_and_hides(window):
    """
    待確認筆數這類警訊本來只寫進「執行紀錄」分頁，
    但使用者九成時間在看「精選」分頁，等於看不到。
    """
    # 用 winfo_manager()：視窗在測試中是 withdrawn 的，winfo_ismapped 永遠是 0
    window.clear_notices()
    assert window.notice_frame.winfo_manager() == ""

    window.add_notice("有 182 筆是推估的")
    assert window.notice_frame.winfo_manager() == "pack"
    assert "182" in window.notice_label.cget("text")

    window.add_notice("有 182 筆是推估的")      # 重複的不該疊加
    assert window.notices == ["有 182 筆是推估的"]

    window.clear_notices()
    assert window.notice_frame.winfo_manager() == ""


def test_scrape_raises_notice_for_pending_rows(window, monkeypatch):
    monkeypatch.setattr(core, "search_pcc",
                        lambda *a, **kw: [pending_row("PK3", "A3", "智慧系統維護案")])
    window.clear_notices()
    window.run_scrape_thread(["系統"], 7, "勞務", "最低標", core.DATE_TYPE_RANGE, False)
    window._drain_ui_queue()

    assert any("推估" in n for n in window.notices)


def test_escape_clears_filter_when_idle(window):
    window.notebook.select(0)
    window.table_matched.entry.insert(0, "機關乙")
    window.on_escape()
    assert window.table_matched.entry.get() == ""


def test_escape_cancels_while_running(window, monkeypatch):
    monkeypatch.setattr(window, "run_scrape_thread", lambda *a, **kw: None)
    try:
        window.on_start_scrape()
        window.on_escape()
        assert window.cancel_event.is_set()
    finally:
        window.is_running = False
        window.cancel_event.clear()


def test_shortcuts_are_bound(window):
    for sequence in ("<F5>", "<Escape>", "<Control-f>", "<Control-e>"):
        assert window.bind(sequence), f"{sequence} 未綁定"
    assert window.kw_entry.bind("<Return>")


def test_tooltip_text_covers_truncated_columns(window):
    """欄寬固定會截斷長標案名，tooltip 要補回完整內容與投標前需要的資訊。"""
    tender = pending_row("PK3", "A3", "宜蘭縣科技教育推動落實數位公平程式學習教學計畫採購案")
    text = window.tooltip_text(tender)

    assert tender["標案名稱"] in text
    assert tender["招標機關"] in text
    assert tender["預算金額"] in text
    assert tender["截止投標"] in text
    # 推估值要在 tooltip 裡也講清楚，不能只靠顏色
    assert "推估值" in text


def test_tooltip_marks_confirmed_rows_plainly(window):
    tender = pending_row("PK3", "A3")
    core.apply_award_method(tender, "最低標")
    text = window.tooltip_text(tender)
    assert "推估值" not in text
    assert core.AWARD_SOURCE_OFFICIAL in text


# ==================== 背景涓流校驗 ====================
#
# 一次搜尋只能免費撿走約 5 筆決標方式，補不完整份待確認清單。這組測試守的是
# 「使用者什麼都不必做，橘色列會自己隨時間變少」這個承諾。


def test_trickle_is_scheduled_after_a_search(window):
    window.trickle_var.set(True)
    window.cancel_trickle()
    window.schedule_trickle()
    assert window._trickle_job is not None


def test_disabling_the_toggle_cancels_the_pending_round(window):
    window.trickle_var.set(True)
    window.schedule_trickle()
    window.trickle_var.set(False)
    window.on_trickle_toggled()
    assert window._trickle_job is None


def test_scheduling_while_disabled_does_nothing(window):
    window.trickle_var.set(False)
    window.schedule_trickle()
    assert window._trickle_job is None
    window.trickle_var.set(True)


def test_only_one_round_is_ever_scheduled(window):
    """重複排程不該堆出一串 after 任務，否則間隔會愈縮愈短、更快撞上額度。"""
    window.trickle_var.set(True)
    window.schedule_trickle()
    first = window._trickle_job
    window.schedule_trickle()
    assert window._trickle_job != first
    assert first not in window.tk.call("after", "info")


def test_round_is_skipped_while_a_search_is_running(window, monkeypatch):
    """背景補齊不該跟前景搜尋搶站方那點額度。"""
    called = []
    monkeypatch.setattr(core, "trickle_verify",
                        lambda *a, **kw: called.append(1) or {"ok": 0})
    window.trickle_var.set(True)
    window.is_running = True
    try:
        window._start_trickle_round()
    finally:
        window.is_running = False
    assert called == []
    assert window._trickle_job is not None, "跳過的這輪要重新排，不能就此停擺"


def test_confirmed_row_leaves_the_shortlist_after_a_round(window):
    """
    整個設計的重點：一筆「公開取得 (待確認)」被背景確認成最有利標時，
    要從「最低標」精選中【消失】，不能只換掉那一格的文字。
    """
    pending = pending_row("PK9", "A9", "待確認的資訊系統案")
    window.tenders_all = [pending]
    window.tenders_by_pk = {"PK9": pending}
    window.active_attr_target = "勞務"
    window.active_award_target = "最低標"
    window.include_misses_var.set(True)
    window.refresh_datasets()
    assert [t["標案案號"] for t in window.tenders_qualified] == ["A9"]

    core.save_award_cache({"A9": {"決標方式": "最有利標",
                                  "verified_at": date.today().isoformat()}},
                          window.award_cache_path())
    window.on_trickle_round_done({"ok": 1, "picked": 5, "blocked": False, "remaining": 3})

    assert window.tenders_qualified == [], "已確認為最有利標的標案不該留在最低標精選"
    assert core.is_award_confirmed(window.tenders_all[0])
    assert "PK9" not in window.table_matched.tree.get_children()


def test_round_without_results_leaves_the_table_alone(window):
    before = list(window.table_all.tree.get_children())
    window.on_trickle_round_done({"ok": 0, "picked": 5, "blocked": True, "remaining": 7})
    assert list(window.table_all.tree.get_children()) == before
    assert window._trickle_busy is False


def test_refresh_preserves_sort_and_filter(window):
    window.active_attr_target = "不限"
    window.active_award_target = "不限"
    window.table_all.sort_by("budget")
    window.table_all.entry.delete(0, "end")
    window.table_all.entry.insert(0, "機關乙")

    window.refresh_datasets()

    assert [t["標案案號"] for t in window.tenders_all] == ["A2", "A1"]
    assert list(window.table_all.tree.get_children()) == ["PK2"]
    window.table_all.entry.delete(0, "end")


def test_worker_error_does_not_kill_the_app(window, monkeypatch):
    """背景工作爆掉只該記一行 log，不能把整個應用程式帶走。"""
    def _boom(*args, **kwargs):
        raise RuntimeError("站方掛了")

    monkeypatch.setattr(core, "trickle_verify", _boom)
    window._trickle_worker()
    window._drain_ui_queue()
    window.update()
    assert "背景補齊發生錯誤" in window.log_text.get("1.0", "end")


def test_trickle_preference_survives_a_restart(window):
    window.trickle_var.set(False)
    window.save_settings()
    window.trickle_var.set(True)
    assert window.restore_settings() is True
    assert window.trickle_var.get() is False
    window.trickle_var.set(True)


def test_settings_write_failure_is_logged(window, monkeypatch):
    """設定存不下來時要講出來，否則使用者只會發現「條件又沒還原」卻查不到原因。"""
    monkeypatch.setattr(core, "save_json_dict", lambda data, path: False)
    window.save_settings()
    assert "無法寫入搜尋條件" in window.log_text.get("1.0", "end")
