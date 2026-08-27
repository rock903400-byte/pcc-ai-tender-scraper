# -*- coding: utf-8 -*-
"""公開資料鏡像來源（pcc_mirror）與 pcc_core 校驗流程的整合測試。"""

import io
import urllib.error

import pcc_core as core
import pcc_mirror as mirror


# ==================== 測試用的假 HTTP ====================

class _FakeResponse(io.BytesIO):
    """urlopen 的最小替身：支援 with 語法與 read()。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "boom", {}, None)


def _record(job_number="A1", unit_id="3.1.1", unit_name="某機關",
            title="某標案", type_="公開取得報價單或企劃書公告"):
    return {"job_number": job_number, "unit_id": unit_id, "unit_name": unit_name,
            "brief": {"title": title, "type": type_}}


def _tender_payload(*pairs):
    """pairs 為 (date, detail dict)，組成 /api/tender 的回應形狀。"""
    return {"records": [{"date": d, "detail": detail} for d, detail in pairs]}


# ==================== HTTP 與退避重試 ====================

class TestFetchJson:
    """429 是軟限制（實測下一筆通常就恢復），值得退避重試；其他失敗不值得。"""

    def test_returns_parsed_json(self, monkeypatch, real_fetch_json):
        monkeypatch.setattr(mirror.urllib.request, "urlopen",
                            lambda req, timeout=None: _FakeResponse(b'{"ok": 1}'))
        assert real_fetch_json("/api/x", {}) == ({"ok": 1}, "ok")

    def test_retries_after_429_then_succeeds(self, monkeypatch, real_fetch_json):
        monkeypatch.setattr(mirror.time, "sleep", lambda _s: None)
        calls = []

        def _fake(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise _http_error(429)
            return _FakeResponse(b'{"ok": 1}')

        monkeypatch.setattr(mirror.urllib.request, "urlopen", _fake)
        assert real_fetch_json("/api/x", {}) == ({"ok": 1}, "ok")
        assert len(calls) == 2

    def test_persistent_429_is_blocked_not_error(self, monkeypatch, real_fetch_json):
        """blocked 代表稍後重試就有，error 代表重試也一樣——呼叫端靠這個決定要不要退回官網。"""
        monkeypatch.setattr(mirror.time, "sleep", lambda _s: None)
        monkeypatch.setattr(mirror.urllib.request, "urlopen",
                            lambda req, timeout=None: (_ for _ in ()).throw(_http_error(429)))
        assert real_fetch_json("/api/x", {}) == (None, "blocked")

    def test_other_http_error_is_error(self, monkeypatch, real_fetch_json):
        monkeypatch.setattr(mirror.urllib.request, "urlopen",
                            lambda req, timeout=None: (_ for _ in ()).throw(_http_error(403)))
        assert real_fetch_json("/api/x", {}) == (None, "error")

    def test_html_error_page_is_error_not_a_crash(self, monkeypatch, real_fetch_json):
        """查無此端點時對方回的是 HTML（HTTP 200），不能讓 json 解析錯誤炸到呼叫端。"""
        monkeypatch.setattr(mirror.urllib.request, "urlopen",
                            lambda req, timeout=None: _FakeResponse(b"<html>404</html>"))
        assert real_fetch_json("/api/x", {}) == (None, "error")


# ==================== 當日索引與消歧義 ====================

class TestDayIndex:
    def test_day_conversion(self):
        assert mirror.to_mirror_day("2026/08/27") == "20260827"
        assert mirror.to_mirror_day("") == ""
        assert mirror.to_mirror_day("115/08/27") == "", "民國日期不該被當成合法輸入送出去"

    def test_index_is_keyed_by_job_number(self):
        index = mirror.build_index({"records": [_record("A1"), _record("A2")]})
        assert sorted(index) == ["A1", "A2"]
        assert index["A1"][0]["unit_id"] == "3.1.1"

    def test_records_without_ids_are_skipped(self):
        assert mirror.build_index({"records": [{"brief": {}}, _record("A1")]}) == {
            "A1": mirror.build_index({"records": [_record("A1")]})["A1"]}

    def test_unique_case_resolves(self):
        index = mirror.build_index({"records": [_record("A1")]})
        assert mirror.resolve_unit_id(index, {"標案案號": "A1"}) == "3.1.1"

    def test_duplicate_job_numbers_are_split_by_agency(self):
        """案號會跨機關重複（實測同日就有兩個機關都用 115-004）。"""
        index = mirror.build_index({"records": [
            _record("115-004", unit_id="3.76.44.53", unit_name="甲機關"),
            _record("115-004", unit_id="3.11.94.6", unit_name="乙機關"),
        ]})
        assert mirror.resolve_unit_id(index, {"標案案號": "115-004",
                                              "招標機關": "乙機關"}) == "3.11.94.6"

    def test_duplicate_job_numbers_are_split_by_title(self):
        index = mirror.build_index({"records": [
            _record("115-004", unit_id="U1", unit_name="", title="清潔案"),
            _record("115-004", unit_id="U2", unit_name="", title="維護案"),
        ]})
        assert mirror.resolve_unit_id(index, {"標案案號": "115-004",
                                              "標案名稱": "維護案"}) == "U2"

    def test_same_case_announced_twice_still_resolves(self):
        """原公告＋更正公告是同一案的兩筆記錄，unit_id 相同就不算有歧義。"""
        index = mirror.build_index({"records": [
            _record("A1", unit_id="U1", title="原公告"),
            _record("A1", unit_id="U1", title="更正公告"),
        ]})
        assert mirror.resolve_unit_id(index, {"標案案號": "A1"}) == "U1"

    def test_unresolvable_ambiguity_returns_empty(self):
        """寧可回空讓呼叫端反查，也不要把別人的決標方式寫進快取——使用者看不出來。"""
        index = mirror.build_index({"records": [
            _record("A1", unit_id="U1", unit_name="甲", title="甲案"),
            _record("A1", unit_id="U2", unit_name="乙", title="乙案"),
        ]})
        assert mirror.resolve_unit_id(index, {"標案案號": "A1", "招標機關": "丙"}) == ""

    def test_unknown_job_number_returns_empty(self):
        assert mirror.resolve_unit_id({}, {"標案案號": "A1"}) == ""


# ==================== 決標方式解析 ====================

class TestExtractAwardMethod:
    def test_reads_the_tender_announcement_field(self):
        payload = _tender_payload((20260827, {mirror.AWARD_DETAIL_KEY: "最低標"}))
        assert mirror.extract_award_method(payload) == "最低標"

    def test_ignores_other_announcements_with_similar_keys(self):
        """「無法決標公告:招標方式」長得很像，收進來就會給出完全錯誤的答案。"""
        payload = _tender_payload(
            (20260827, {"無法決標公告:招標方式": "公開取得報價單或企劃書",
                        "無法決標公告:是否沿用": "是"}))
        assert mirror.extract_award_method(payload) == ""

    def test_correction_notice_wins(self):
        """更正公告會改動決標方式，拿舊的那筆等於給錯答案。"""
        payload = _tender_payload(
            (20260801, {mirror.AWARD_DETAIL_KEY: "最低標"}),
            (20260820, {mirror.AWARD_DETAIL_KEY: "參考最有利標精神"}),
        )
        assert mirror.extract_award_method(payload) == "參考最有利標精神"

    def test_empty_payload_is_not_a_crash(self):
        assert mirror.extract_award_method(None) == ""
        assert mirror.extract_award_method({"records": []}) == ""


# ==================== 取值流程 ====================

class TestFetchAwardMethodStatus:
    @staticmethod
    def _routes(monkeypatch, day_records=(), tender=None, calls=None):
        def _fake(path, params):
            if calls is not None:
                calls.append((path, params))
            if path == mirror.LIST_BY_DATE_PATH:
                return {"records": list(day_records)}, "ok"
            if path == mirror.TENDER_PATH:
                return tender, "ok"
            return None, "error"

        monkeypatch.setattr(mirror, "fetch_json", _fake)

    def test_happy_path(self, monkeypatch):
        self._routes(monkeypatch, day_records=[_record("A1", unit_id="U1")],
                     tender=_tender_payload((20260827, {mirror.AWARD_DETAIL_KEY: "最低標"})))
        tender = {"標案案號": "A1", "公告日期": "2026/08/27"}
        assert mirror.fetch_award_method_status(tender, {}) == ("最低標", "ok")

    def test_day_index_is_fetched_once_per_date(self, monkeypatch):
        """一次 listbydate 就涵蓋當天全部標案，這是本模組省請求的關鍵。"""
        calls = []
        self._routes(monkeypatch,
                     day_records=[_record("A%d" % i, unit_id="U%d" % i) for i in range(5)],
                     tender=_tender_payload((20260827, {mirror.AWARD_DETAIL_KEY: "最低標"})),
                     calls=calls)
        index_cache = {}
        for i in range(5):
            mirror.fetch_award_method_status(
                {"標案案號": "A%d" % i, "公告日期": "2026/08/27"}, index_cache)

        assert sum(1 for path, _ in calls if path == mirror.LIST_BY_DATE_PATH) == 1
        assert sum(1 for path, _ in calls if path == mirror.TENDER_PATH) == 5

    def test_falls_back_to_title_search_when_the_day_index_misses(self, monkeypatch):
        calls = []

        def _fake(path, params):
            calls.append(path)
            if path == mirror.LIST_BY_DATE_PATH:
                return {"records": []}, "ok"          # 當天索引裡沒有這案
            if path == mirror.SEARCH_BY_TITLE_PATH:
                return {"records": [_record("A1", unit_id="U9")]}, "ok"
            return _tender_payload((20260827, {mirror.AWARD_DETAIL_KEY: "最低標"})), "ok"

        monkeypatch.setattr(mirror, "fetch_json", _fake)
        tender = {"標案案號": "A1", "標案名稱": "某標案", "公告日期": "2026/08/27"}
        assert mirror.fetch_award_method_status(tender, {}) == ("最低標", "ok")
        assert mirror.SEARCH_BY_TITLE_PATH in calls

    def test_missing_field_is_error_not_blocked(self, monkeypatch):
        """error＝重試也沒用，呼叫端才會改走官網詳細頁。"""
        self._routes(monkeypatch, day_records=[_record("A1", unit_id="U1")],
                     tender=_tender_payload((20260827, {"其他欄位": "x"})))
        tender = {"標案案號": "A1", "公告日期": "2026/08/27"}
        assert mirror.fetch_award_method_status(tender, {}) == ("", "error")

    def test_rate_limited_is_blocked(self, monkeypatch):
        monkeypatch.setattr(mirror, "fetch_json", lambda path, params: (None, "blocked"))
        tender = {"標案案號": "A1", "標案名稱": "某標案", "公告日期": "2026/08/27"}
        assert mirror.fetch_award_method_status(tender, {}) == ("", "blocked")


# ==================== 與 pcc_core 校驗流程的整合 ====================

class TestEnrichmentUsesMirrorFirst:
    @staticmethod
    def _rows(count=3):
        return [{"pk": "pk%d" % i, "標案案號": "A%d" % i, "標案名稱": "第 %d 案" % i,
                 "招標機關": "某機關", "公告日期": "2026/08/27",
                 "招標方式": "公開取得報價單或企劃書",
                 "決標方式來源": core.AWARD_SOURCE_ESTIMATED}
                for i in range(count)]

    @staticmethod
    def _mirror_returns(monkeypatch, value, status):
        monkeypatch.setattr(mirror, "fetch_award_method_status",
                            lambda tender, index_cache=None: (value, status))

    def test_mirror_success_never_touches_the_official_detail_page(self, monkeypatch):
        """整個改動的重點：官網那 5 筆/輪的額度根本不必動用。"""
        official_calls = []
        monkeypatch.setattr(core, "fetch_award_method_status",
                            lambda pk: official_calls.append(pk) or ("最低標", "ok"))
        self._mirror_returns(monkeypatch, "最低標", "ok")

        rows = self._rows()
        stats = core.enrich_actual_award_methods(rows)

        assert official_calls == []
        assert stats["ok"] == 3 and stats["mirror_ok"] == 3 and stats["official_ok"] == 0
        assert all(r["決標方式來源"] == core.AWARD_SOURCE_MIRROR for r in rows)

    def test_mirror_miss_falls_back_to_the_official_page(self, monkeypatch):
        monkeypatch.setattr(core, "DETAIL_DELAY_RANGE", (0, 0))
        monkeypatch.setattr(core, "fetch_award_method_status", lambda pk: ("最低標", "ok"))
        self._mirror_returns(monkeypatch, "", "error")

        rows = self._rows(count=2)
        stats = core.enrich_actual_award_methods(rows)

        assert stats["mirror_ok"] == 0 and stats["official_ok"] == 2
        assert all(r["決標方式來源"] == core.AWARD_SOURCE_OFFICIAL for r in rows)

    def test_mirror_rate_limit_does_not_burn_official_quota(self, monkeypatch):
        """鏡像限流是暫時的，下一輪就有；此時退回官網只是白燒那 5 筆額度。"""
        official_calls = []
        monkeypatch.setattr(core, "fetch_award_method_status",
                            lambda pk: official_calls.append(pk) or ("最低標", "ok"))
        self._mirror_returns(monkeypatch, "", "blocked")

        stats = core.enrich_actual_award_methods(self._rows())

        assert official_calls == []
        assert stats["ok"] == 0
        assert stats["blocked"] is False, "鏡像限流不該觸發官網那條「額度用盡」的中止規則"

    def test_mirror_giving_up_switches_the_whole_round_to_the_official_page(self, monkeypatch):
        """鏡像連續查不到就別再每筆都試，那只是白等節流間隔。"""
        monkeypatch.setattr(core, "DETAIL_DELAY_RANGE", (0, 0))
        monkeypatch.setattr(core, "fetch_award_method_status", lambda pk: ("最低標", "ok"))
        mirror_calls = []
        monkeypatch.setattr(
            mirror, "fetch_award_method_status",
            lambda tender, index_cache=None: mirror_calls.append(tender) or ("", "error"))

        messages = []
        core.enrich_actual_award_methods(self._rows(count=12), log=messages.append)

        assert len(mirror_calls) == mirror.MIRROR_FAIL_STREAK
        assert any("改用官網詳細頁" in m for m in messages)

    def test_sustained_rate_limiting_does_not_hand_the_round_to_the_official_page(self, monkeypatch):
        """
        限流是暫時的、額度是永久的：因為鏡像 429 就整輪改走官網，
        等於把一個下一輪會自己好的問題，換成真的用掉那 5 筆額度。
        """
        official_calls = []
        monkeypatch.setattr(core, "fetch_award_method_status",
                            lambda pk: official_calls.append(pk) or ("最低標", "ok"))
        self._mirror_returns(monkeypatch, "", "blocked")

        core.enrich_actual_award_methods(self._rows(count=12))

        assert official_calls == []

    def test_use_mirror_false_keeps_the_old_official_only_behaviour(self, monkeypatch):
        monkeypatch.setattr(core, "DETAIL_DELAY_RANGE", (0, 0))
        monkeypatch.setattr(core, "fetch_award_method_status", lambda pk: ("最低標", "ok"))
        called = []
        monkeypatch.setattr(mirror, "fetch_award_method_status",
                            lambda tender, index_cache=None: called.append(1) or ("", "error"))

        stats = core.enrich_actual_award_methods(self._rows(), use_mirror=False)

        assert called == []
        assert stats["official_ok"] == 3

    def test_cache_remembers_which_source_confirmed_it(self, monkeypatch, tmp_path):
        """快取套回去時來源標籤要跟著回來，不然鏡像確認的列會被標成官方詳細頁。"""
        self._mirror_returns(monkeypatch, "最低標", "ok")
        cache = {}
        core.enrich_actual_award_methods(self._rows(count=1), cache=cache)

        fresh = self._rows(count=1)
        assert core.apply_award_cache(fresh, cache) == 1
        assert fresh[0]["決標方式來源"] == core.AWARD_SOURCE_MIRROR

    def test_legacy_cache_entries_still_count_as_confirmed(self):
        """舊快取沒記來源欄位，不能因此被當成未確認而重查一次。"""
        cache = {"A0": {"決標方式": "最低標", "verified_at": "2026-08-26"}}
        rows = self._rows(count=1)
        assert core.apply_award_cache(rows, cache) == 1
        assert rows[0]["決標方式來源"] == core.AWARD_SOURCE_OFFICIAL
        assert core.is_award_confirmed(rows[0])


# ==================== 佇列必須帶著鏡像定位需要的欄位 ====================

class TestPendingQueueCarriesLookupKeys:
    def test_queue_round_trip_keeps_the_announcement_date(self, tmp_path):
        """公告日期是鏡像當日索引的鑰匙，佇列不存就只能每筆多打一次反查。"""
        path = core.pending_queue_path(str(tmp_path))
        core.save_pending_queue([{"pk": "p1", "標案案號": "A1", "標案名稱": "某案",
                                  "招標機關": "某機關", "招標方式": "公開取得報價單或企劃書",
                                  "公告日期": "2026/08/27"}], path)

        row = core.load_pending_queue(path)[0]
        assert row["公告日期"] == "2026/08/27"
        assert row["招標機關"] == "某機關"

    def test_queue_written_by_an_older_version_still_loads(self, tmp_path):
        """舊版佇列沒有公告日期，只能退回以標案名稱反查，但不能因此壞掉。"""
        path = core.pending_queue_path(str(tmp_path))
        core.save_json_dict({"A1": {"pk": "p1", "標案名稱": "某案", "order": 0}}, path)

        row = core.load_pending_queue(path)[0]
        assert row["公告日期"] == ""
        assert mirror.to_mirror_day(row["公告日期"]) == ""
