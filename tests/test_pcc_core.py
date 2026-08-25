# -*- coding: utf-8 -*-
"""
pcc_core 的離線單元測試。

所有測試都以 tests/fixtures/ 下的真實回應存檔為輸入，
完全不連線政府採購網。
"""

import pytest

import pcc_core as core


# ==================== 日期與金額 ====================

class TestDateConversion:
    @pytest.mark.parametrize("ad, roc", [
        ("2026/08/24", "115/08/24"),
        ("2026-01-05", "115/01/05"),
        ("2000/12/31", "89/12/31"),
    ])
    def test_to_roc_date(self, ad, roc):
        assert core.to_roc_date(ad) == roc

    @pytest.mark.parametrize("roc, ad", [
        ("115/08/24", "2026/08/24"),
        ("115/9/3", "2026/09/03"),
        ("89/12/31", "2000/12/31"),
    ])
    def test_to_ad_date(self, roc, ad):
        assert core.to_ad_date(roc) == ad

    def test_to_ad_date_passes_through_western_dates(self):
        """已是西元的日期不可被再加 1911。"""
        assert core.to_ad_date("2026/08/24") == "2026/08/24"

    @pytest.mark.parametrize("bad", ["", "未定", "115/08", "N/A"])
    def test_to_ad_date_survives_bad_input(self, bad):
        assert core.to_ad_date(bad) == bad.strip()

    def test_roc_ad_round_trip(self):
        assert core.to_ad_date(core.to_roc_date("2026/08/24")) == "2026/08/24"


class TestParseAmount:
    @pytest.mark.parametrize("text, expected", [
        ("2,608,500 元", 2608500.0),
        ("21,000,000", 21000000.0),
        ("0 元", 0.0),
        ("", -1.0),
        ("未公開", -1.0),
        (None, -1.0),
    ])
    def test_parse_amount(self, text, expected):
        assert core.parse_amount(text) == expected

    def test_amounts_sort_numerically_not_lexically(self):
        """字串排序會把 9,000 排在 21,000,000 之後，數值排序不會。"""
        values = ["9,000 元", "21,000,000 元", "100,000 元"]
        assert sorted(values, key=core.parse_amount) == [
            "9,000 元", "100,000 元", "21,000,000 元",
        ]


# ==================== 決標方式判定 ====================

class TestDetermineAwardMethod:
    @pytest.mark.parametrize("tender_way, expected_desc, expected_lowest", [
        ("公開招標", "最低標 (公開招標)", True),
        ("選擇性招標", "最低標 (選擇性招標)", True),
        ("公開取得報價單或企劃書", "公開取得 (待確認)", True),
        ("限制性招標", "限制性招標", False),
        ("公開評選", "最有利標 / 評選", False),
        ("", "未標明", False),
    ])
    def test_estimates_from_tender_way(self, tender_way, expected_desc, expected_lowest):
        assert core.determine_award_method(tender_way) == (expected_desc, expected_lowest)

    @pytest.mark.parametrize("actual, expected_lowest", [
        ("最低標", True),
        ("最低標，且不訂底價", True),
        ("最有利標", False),
        ("準用最有利標", False),
        ("參考最有利標", False),
        ("非以價格為考量之最有利標", False),
    ])
    def test_detail_page_value_wins(self, actual, expected_lowest):
        """詳細頁欄位為最高準則，且原文須原樣保留。"""
        desc, is_lowest = core.determine_award_method("公開招標", actual)
        assert desc == actual
        assert is_lowest is expected_lowest

    def test_reference_best_value_is_not_lowest_bid(self):
        """「參考最有利標」同時含最有利標字樣，不可被誤判為最低標。"""
        _desc, is_lowest = core.determine_award_method("公開招標", "參考最有利標")
        assert is_lowest is False

    def test_detail_page_overrides_optimistic_guess(self):
        """『公開取得』推估為最低標，但詳細頁若為評選則必須被推翻。"""
        _desc, guess = core.determine_award_method("公開取得報價單或企劃書")
        assert guess is True
        _desc, corrected = core.determine_award_method("公開取得報價單或企劃書", "最有利標")
        assert corrected is False

    def test_unknown_detail_value_is_not_lowest(self):
        assert core.determine_award_method("公開招標", "其他") == ("其他", False)


# ==================== 表頭欄位對照 ====================

class TestColumnIndex:
    def test_derives_indices_from_real_header(self, search_html):
        index_map, warnings = core.parse_column_index(search_html)
        assert warnings == []
        assert index_map == {
            "org": 1, "id_name": 2, "way": 4,
            "cate": 5, "pub": 6, "deadline": 7, "budget": 8,
        }

    def test_falls_back_and_warns_when_header_missing(self):
        """網站改版導致表頭消失時，必須發出警告而非靜默錯位。"""
        index_map, warnings = core.parse_column_index("<table><tr><td>x</td></tr></table>")
        assert index_map == core.DEFAULT_COLUMN_INDEX
        assert warnings and "表頭" in warnings[0]

    def test_warns_on_renamed_column(self):
        html = ("<table><tr><th>項次</th><th>機關名稱</th><th>標案案號標案名稱</th>"
                "<th>傳輸次數</th><th>招標方式</th><th>採購類別</th>"
                "<th>公告日期</th><th>截止投標</th><th>預算金額</th></tr></table>")
        index_map, warnings = core.parse_column_index(html)
        assert any("採購性質" in w for w in warnings)
        assert index_map["cate"] == core.DEFAULT_COLUMN_INDEX["cate"]

    def test_tracks_shifted_columns(self):
        """若網站在前面插入一欄，索引必須跟著位移。"""
        html = ("<table><tr><th>核選</th><th>項次</th><th>機關名稱</th>"
                "<th>標案案號標案名稱</th><th>傳輸次數</th><th>招標方式</th>"
                "<th>採購性質</th><th>公告日期</th><th>截止投標</th>"
                "<th>預算金額</th></tr></table>")
        index_map, warnings = core.parse_column_index(html)
        assert warnings == []
        assert index_map["org"] == 2
        assert index_map["budget"] == 9


# ==================== 搜尋結果解析 ====================

class TestParseTenderRows:
    def test_parses_expected_row_count(self, search_html):
        assert len(core.parse_tender_rows(search_html, "AI")) == 7

    def test_parses_full_page(self, paged_html):
        assert len(core.parse_tender_rows(paged_html, "系統")) == 50

    def test_first_row_fields(self, search_html):
        row = core.parse_tender_rows(search_html, "AI")[0]
        assert row["標案案號"] == "115JZ037"
        assert row["標案名稱"] == "115年「AI軟硬體暨教育訓練服務採購案」"
        assert row["招標機關"] == "交通部鐵道局"
        assert row["招標方式"] == "公開招標"
        assert row["採購性質"] == "勞務類"
        assert row["預算金額"] == "2,608,500 元"
        assert row["搜尋關鍵字"] == "AI"
        assert row["詳細連結"].endswith(row["pk"])

    def test_both_date_columns_are_western(self, search_html):
        """公告日期與截止投標必須同為西元，否則同表兩欄格式不一致。"""
        for row in core.parse_tender_rows(search_html, "AI"):
            assert row["公告日期"].startswith("20")
            assert row["截止投標"].startswith("20")

    def test_dates_sort_chronologically_as_strings(self, search_html):
        rows = core.parse_tender_rows(search_html, "AI")
        dates = [r["公告日期"] for r in rows]
        assert sorted(dates) == sorted(dates, key=lambda d: [int(p) for p in d.split("/")])

    def test_correction_notice_split_out_of_tender_id(self, search_html):
        """『(更正公告)』註記須移出標案案號，否則同一標案會被當成兩筆。"""
        rows = core.parse_tender_rows(search_html, "AI")
        corrected = [r for r in rows if r["公告類型"]]
        assert corrected, "fixture 應含至少一筆更正公告"
        assert corrected[0]["標案案號"] == "11508121"
        assert corrected[0]["公告類型"] == "更正公告"
        assert all("更正公告" not in r["標案案號"] for r in rows)

    def test_tender_name_never_falls_back_to_transfer_count(self):
        """
        迴歸測試：標案名稱與案號同屬第 2 欄，第 3 欄是「傳輸次數」。
        名稱取不到時必須退回同一格的連結文字，不可誤取傳輸次數。
        """
        html = """
        <table>
          <tr><th>項次</th><th>機關名稱</th><th>標案案號標案名稱</th><th>傳輸次數</th>
              <th>招標方式</th><th>採購性質</th><th>公告日期</th><th>截止投標</th>
              <th>預算金額</th></tr>
          <tr>
            <td>1</td><td>某某機關</td>
            <td>A123<br><a href="/prkms/urlSelector/common/tpam?pk=ABC123">純文字標案名稱</a></td>
            <td>07</td><td>公開招標</td><td>勞務類</td>
            <td>115/08/24</td><td>115/09/03</td><td>1,000</td>
          </tr>
        </table>
        """
        row = core.parse_tender_rows(html, "測試")[0]
        assert row["標案名稱"] == "純文字標案名稱"
        assert row["標案名稱"] != "07"

    def test_ignores_rows_without_pk(self):
        html = "<table><tr><td>1</td><td>2</td><td>3</td></tr></table>"
        assert core.parse_tender_rows(html, "kw") == []

    def test_flags_service_and_lowest_bid(self, search_html):
        for row in core.parse_tender_rows(search_html, "AI"):
            expected = "是" if "勞務" in row["採購性質"] else "否"
            assert row["是否為勞務類"] == expected


# ==================== 分頁 ====================

class TestPagination:
    def test_reads_total_records(self, paged_html):
        assert core.parse_total_records(paged_html) == 138

    def test_computes_total_pages(self, paged_html):
        assert core.parse_total_pages(paged_html) == 3

    def test_single_page_result_has_no_banner(self, search_html):
        assert core.parse_total_pages(search_html) == 1

    def test_extracts_displaytag_page_param(self, paged_html):
        assert core.parse_page_param(paged_html) == "d-49738-p"

    def test_page_param_also_discoverable_from_sort_links(self, search_html):
        """
        單頁結果不會渲染分頁列，但欄位排序連結同樣帶有該參數，
        因此參數名稱仍取得到（總頁數為 1 時不會被使用）。
        """
        assert core.parse_page_param(search_html) == "d-49738-p"

    def test_page_param_missing_when_markup_changes(self):
        assert core.parse_page_param("<table><tr><td>無分頁</td></tr></table>") is None

    def test_falls_back_to_first_page_when_param_missing(self, monkeypatch, paged_html):
        """找不到分頁參數時只回第 1 頁並示警，不可猜參數名亂送。"""
        html = paged_html.replace("d-49738-p", "d-XXXXX-q")
        monkeypatch.setattr(core, "http_post", lambda url, data, **kw: html)
        messages = []
        rows = core.search_pcc("系統", "115/06/01", "115/08/25",
                               log=messages.append, polite_delay=0)
        assert len(rows) == 50
        assert any("找不到分頁參數" in m for m in messages)

    @pytest.mark.parametrize("total, expected_pages", [
        (0, 1), (1, 1), (50, 1), (51, 2), (100, 2), (138, 3),
    ])
    def test_page_count_arithmetic(self, total, expected_pages):
        html = f'共有<span class="red"> {total} </span>筆資料'
        assert core.parse_total_pages(html) == expected_pages


class TestSearchPcc:
    """search_pcc 的翻頁行為（以假的 http_post 取代網路連線）。"""

    def test_walks_every_page(self, monkeypatch, paged_html, search_html):
        requested = []

        def fake_post(url, data, **kwargs):
            requested.append(dict(data))
            page = data.get("d-49738-p", "1")
            return paged_html if page == "1" else search_html

        monkeypatch.setattr(core, "http_post", fake_post)
        rows = core.search_pcc("系統", "115/06/01", "115/08/25", polite_delay=0)

        assert [r.get("d-49738-p") for r in requested] == [None, "2", "3"]
        # 第 1 頁 50 筆 + 第 2、3 頁各回 7 筆的替身內容
        assert len(rows) == 50 + 7 + 7

    def test_stops_at_max_pages(self, monkeypatch, paged_html, search_html):
        monkeypatch.setattr(core, "http_post",
                            lambda url, data, **kw: paged_html if "d-49738-p" not in data else search_html)
        rows = core.search_pcc("系統", "115/06/01", "115/08/25", max_pages=2, polite_delay=0)
        assert len(rows) == 50 + 7

    def test_single_page_does_not_paginate(self, monkeypatch, search_html):
        calls = []

        def fake_post(url, data, **kwargs):
            calls.append(data)
            return search_html

        monkeypatch.setattr(core, "http_post", fake_post)
        rows = core.search_pcc("AI", "115/08/01", "115/08/25", polite_delay=0)
        assert len(calls) == 1
        assert len(rows) == 7

    def test_captcha_page_returns_empty(self, monkeypatch):
        monkeypatch.setattr(core, "http_post", lambda url, data, **kw: "請輸入圖形驗證碼")
        monkeypatch.setattr(core.time, "sleep", lambda _s: None)
        assert core.search_pcc("AI", "115/08/01", "115/08/25", polite_delay=0) == []

    def test_connection_failure_is_reported_not_raised(self, monkeypatch):
        def boom(url, data, **kwargs):
            raise OSError("連線逾時")

        monkeypatch.setattr(core, "http_post", boom)
        messages = []
        assert core.search_pcc("AI", "115/08/01", "115/08/25",
                               log=messages.append, polite_delay=0) == []
        assert any("搜尋連線失敗" in m for m in messages)

    def test_proctrg_cate_is_forwarded(self, monkeypatch, search_html):
        captured = {}

        def fake_post(url, data, **kwargs):
            captured.update(data)
            return search_html

        monkeypatch.setattr(core, "http_post", fake_post)
        core.search_pcc("AI", "115/08/01", "115/08/25",
                        proctrg_cate=core.PROCTRG_CATE["工程"], polite_delay=0)
        assert captured["radProctrgCate"] == "RAD_PROCTRG_CATE_1"

    def test_unlimited_category_omits_the_parameter(self, monkeypatch, search_html):
        captured = {}

        def fake_post(url, data, **kwargs):
            captured.update(data)
            return search_html

        monkeypatch.setattr(core, "http_post", fake_post)
        core.search_pcc("AI", "115/08/01", "115/08/25", proctrg_cate=None, polite_delay=0)
        assert "radProctrgCate" not in captured


# ==================== 詳細頁校驗 ====================

class TestDetailPage:
    def test_extracts_award_method(self, monkeypatch, detail_html):
        monkeypatch.setattr(core, "http_get", lambda url, **kw: detail_html)
        assert core.fetch_actual_award_method("NzEzMDgzNTk=") == "最低標"

    def test_empty_pk_skips_request(self, monkeypatch):
        monkeypatch.setattr(core, "http_get",
                            lambda url, **kw: pytest.fail("不應對空 pk 發出請求"))
        assert core.fetch_actual_award_method("") == ""

    def test_network_error_degrades_quietly(self, monkeypatch):
        def boom(url, **kwargs):
            raise OSError("timeout")

        monkeypatch.setattr(core, "http_get", boom)
        assert core.fetch_actual_award_method("abc") == ""

    def test_apply_award_method_updates_derived_fields(self):
        tender = {
            "招標方式": "公開取得報價單或企劃書",
            "決標方式": "公開取得 (待確認)",
            "決標方式來源": core.AWARD_SOURCE_ESTIMATED,
            "是否為勞務類": "是",
            "是否為最低標": "是",
            "完全符合目標": "符合 (勞務+最低標)",
        }
        core.apply_award_method(tender, "最有利標")
        assert tender["決標方式"] == "最有利標"
        assert tender["是否為最低標"] == "否"
        assert tender["完全符合目標"] == "其他"
        assert tender["決標方式來源"] == core.AWARD_SOURCE_OFFICIAL

    def test_award_source_marks_unverified_rows(self, monkeypatch, search_html):
        """
        詳細頁取不到值時，該列必須留在「推估」狀態，
        使用者才看得出哪些「最低標」還沒經官方欄位確認。
        """
        rows = core.parse_tender_rows(search_html, "AI")
        assert all(r["決標方式來源"] == core.AWARD_SOURCE_ESTIMATED for r in rows)

        monkeypatch.setattr(core, "fetch_actual_award_method",
                            lambda pk: "最低標" if pk == rows[0]["pk"] else "")
        core.enrich_actual_award_methods(rows, max_workers=2)

        assert rows[0]["決標方式來源"] == core.AWARD_SOURCE_OFFICIAL
        assert all(r["決標方式來源"] == core.AWARD_SOURCE_ESTIMATED for r in rows[1:])

    def test_enrich_reports_failures(self, monkeypatch):
        monkeypatch.setattr(core, "fetch_actual_award_method", lambda pk: "" if pk == "b" else "最低標")
        tenders = [{"pk": "a", "招標方式": "公開招標", "是否為勞務類": "是"},
                   {"pk": "b", "招標方式": "公開招標", "是否為勞務類": "是"}]
        messages = []
        ok = core.enrich_actual_award_methods(tenders, max_workers=2, log=messages.append)
        assert ok == 1
        assert any("1 筆" in m and "無法取得" in m for m in messages)

    def test_enrich_reports_progress_for_every_item(self, monkeypatch):
        monkeypatch.setattr(core, "fetch_actual_award_method", lambda pk: "最低標")
        tenders = [{"pk": str(i), "招標方式": "公開招標", "是否為勞務類": "是"} for i in range(10)]
        seen = []
        core.enrich_actual_award_methods(tenders, max_workers=4,
                                         progress_cb=lambda d, t: seen.append((d, t)))
        assert sorted(d for d, _ in seen) == list(range(1, 11))
        assert all(t == 10 for _, t in seen)


# ==================== 去重、篩選與匯出 ====================

class TestMergeAndFilter:
    def test_merge_accumulates_keywords(self):
        unique = {}
        core.merge_by_tender_id(unique, [{"標案案號": "A1", "標案名稱": "案一"}], "AI")
        core.merge_by_tender_id(unique, [{"標案案號": "A1", "標案名稱": "案一"}], "資訊")
        core.merge_by_tender_id(unique, [{"標案案號": "A1", "標案名稱": "案一"}], "AI")
        assert list(unique) == ["A1"]
        assert unique["A1"]["命中關鍵字群"] == ["AI", "資訊"]

    def test_finalize_keywords_flattens(self):
        tenders = [{"命中關鍵字群": ["AI", "資訊"]}, {}]
        core.finalize_keywords(tenders)
        assert tenders[0]["命中關鍵字"] == "AI, 資訊"
        assert tenders[1]["命中關鍵字"] == ""

    @pytest.mark.parametrize("attr, award, expected_ids", [
        ("勞務", "最低標", ["s-low"]),
        ("勞務", "不限", ["s-low", "s-best"]),
        ("不限", "最低標", ["s-low", "g-low"]),
        ("不限", "不限", ["s-low", "s-best", "g-low"]),
        ("勞務", "最有利標/評選", ["s-best"]),
    ])
    def test_filter_combinations(self, attr, award, expected_ids):
        tenders = [
            {"標案案號": "s-low", "採購性質": "勞務類", "是否為最低標": "是", "決標方式": "最低標"},
            {"標案案號": "s-best", "採購性質": "勞務類", "是否為最低標": "否", "決標方式": "最有利標"},
            {"標案案號": "g-low", "採購性質": "財物類", "是否為最低標": "是", "決標方式": "最低標"},
        ]
        result = core.filter_tenders(tenders, attr, award)
        assert [t["標案案號"] for t in result] == expected_ids


class TestReports:
    def test_csv_excludes_internal_keys(self, tmp_path, search_html):
        rows = core.parse_tender_rows(search_html, "AI")
        core.finalize_keywords(rows)
        path = str(tmp_path / "out.csv")
        core.write_csv_report(path, rows)

        with open(path, encoding="utf-8-sig") as f:
            header = f.readline().strip().split(",")
        assert "pk" not in header
        assert "命中關鍵字群" not in header
        assert "標案名稱" in header

    def test_csv_of_empty_list_writes_nothing(self, tmp_path):
        path = str(tmp_path / "empty.csv")
        assert core.write_csv_report(path, []) == ""
        assert not (tmp_path / "empty.csv").exists()

    def test_excel_has_both_sheets_even_when_no_match(self, tmp_path, search_html):
        pytest.importorskip("openpyxl")
        rows = core.parse_tender_rows(search_html, "AI")
        core.finalize_keywords(rows)
        path = str(tmp_path / "out.xlsx")
        core.write_excel_report(path, rows, [])

        from openpyxl import load_workbook
        wb = load_workbook(path)
        assert wb.sheetnames == ["精選_勞務最低標", "所有搜尋標案"]
        assert wb["所有搜尋標案"].max_row == len(rows) + 1

    def test_excel_columns_follow_preferred_order(self, tmp_path, search_html):
        pytest.importorskip("openpyxl")
        rows = core.parse_tender_rows(search_html, "AI")
        core.finalize_keywords(rows)
        path = str(tmp_path / "out.xlsx")
        core.write_excel_report(path, rows, rows[:1])

        from openpyxl import load_workbook
        wb = load_workbook(path)
        header = [c.value for c in wb["所有搜尋標案"][1]]
        assert header == [c for c in core.PREFERRED_COLS if c in rows[0]]


# ==================== HTML 工具 ====================

class TestStripTags:
    @pytest.mark.parametrize("raw, expected", [
        ("<td>  文字  </td>", "文字"),
        ("<td>a<script>var x=1;</script>b</td>", "ab"),
        ("<td>甲&emsp;&emsp;乙</td>", "甲 乙"),
        ("<td>A&amp;B</td>", "A&B"),
        ("", ""),
    ])
    def test_strip_tags(self, raw, expected):
        assert core.strip_tags(raw) == expected


class TestCaptchaDetection:
    @pytest.mark.parametrize("html, expected", [
        ("請輸入圖形驗證碼", True),
        ("<p>請輸入驗證碼</p>", True),
        ("<table>正常結果</table>", False),
    ])
    def test_is_captcha_page(self, html, expected):
        assert core.is_captcha_page(html) is expected
