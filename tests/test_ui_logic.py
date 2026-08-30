# -*- coding: utf-8 -*-
"""
ui_logic 的離線單元測試

涵蓋 TICKET-B1 搬出的純函式與看板聚合邏輯，
風格比照 tests/test_pcc_core.py：pytest 原生 assert、中文 docstring，
完全離線、不寫入 output/。
"""

from datetime import date, timedelta

import pandas as pd
import pytest

import pcc_core as core
import ui_logic


# ==================== 基礎 helpers ====================

class TestDescribeFilter:
    """describe_filter 的組合邏輯"""

    def test_兩者皆不限回全部條件(self):
        assert ui_logic.describe_filter("不限", "不限") == "全部條件"

    def test_單一條件(self):
        assert ui_logic.describe_filter("勞務", "不限") == "勞務"
        assert ui_logic.describe_filter("不限", "最低標") == "最低標"

    def test_兩者皆有值直接拼接(self):
        assert ui_logic.describe_filter("勞務", "最低標") == "勞務最低標"

    def test_空字串視同不限(self):
        assert ui_logic.describe_filter("", "") == "全部條件"


class TestIsAwardPending:
    """is_award_pending 需同時滿足：未確認且含 待確認"""

    def test_待確認且未確認回True(self):
        t = {"決標方式": "公開取得 (待確認)", "決標方式來源": "推估"}
        # 確保 core.is_award_confirmed 為 False 的情況
        # 透過來源為推估來保證未確認
        assert ui_logic.is_award_pending(t) is True

    def test_已確認即使含待確認字樣也不算(self):
        t = {"決標方式": "最低標", "決標方式來源": core.AWARD_SOURCE_MIRROR}
        assert ui_logic.is_award_pending(t) is False

    def test_未確認但不含待確認字樣不算(self):
        t = {"決標方式": "最低標", "決標方式來源": core.AWARD_SOURCE_ESTIMATED}
        # 這種即使來源是推估，但字串不含 待確認，依現行邏輯不算 pending
        assert ui_logic.is_award_pending(t) is False


# ==================== parse_deadline_date / get_days_remaining ====================

class TestParseDeadlineDate:
    """parse_deadline_date 的多格式解析與民國年分支"""

    def test_民國年115轉西元2026(self):
        """y < 1900 需加 1911，115/09/15 → 2026-09-15"""
        assert ui_logic.parse_deadline_date("115/09/15") == date(2026, 9, 15)

    def test_西元年斜線格式(self):
        assert ui_logic.parse_deadline_date("2026/09/15") == date(2026, 9, 15)

    def test_破折號格式(self):
        assert ui_logic.parse_deadline_date("2026-09-15") == date(2026, 9, 15)

    def test_帶時間字串僅取首段(self):
        assert ui_logic.parse_deadline_date("2026/09/15 17:00") == date(2026, 9, 15)

    def test_空字串回None(self):
        assert ui_logic.parse_deadline_date("") is None

    def test_None回None(self):
        assert ui_logic.parse_deadline_date(None) is None

    def test_非字串型別回None(self):
        assert ui_logic.parse_deadline_date(12345) is None
        assert ui_logic.parse_deadline_date(3.14) is None

    def test_垃圾字串待補回None(self):
        assert ui_logic.parse_deadline_date("待補") is None
        assert ui_logic.parse_deadline_date("N/A") is None
        assert ui_logic.parse_deadline_date("2026/13/40") is None

    def test_前後空白可容忍(self):
        assert ui_logic.parse_deadline_date("  2026/09/15  ") == date(2026, 9, 15)

    def test_民國年99對應2010(self):
        assert ui_logic.parse_deadline_date("99/01/01") == date(2010, 1, 1)


class TestGetDaysRemaining:
    """get_days_remaining 對 parse 結果做今日差值"""

    def test_已過期回負數(self):
        past = (date.today() - timedelta(days=5)).strftime("%Y/%m/%d")
        days = ui_logic.get_days_remaining(past)
        assert days == -5

    def test_今日回0(self):
        today = date.today().strftime("%Y/%m/%d")
        assert ui_logic.get_days_remaining(today) == 0

    def test_未來3天(self):
        future = (date.today() + timedelta(days=3)).strftime("%Y/%m/%d")
        assert ui_logic.get_days_remaining(future) == 3

    def test_無效日期回None(self):
        assert ui_logic.get_days_remaining("待補") is None
        assert ui_logic.get_days_remaining("") is None
        assert ui_logic.get_days_remaining(None) is None

    def test_民國年輸入亦可計算(self):
        target = date.today() + timedelta(days=10)
        roc_date = f"{target.year - 1911}/{target.month:02d}/{target.day:02d}"
        days = ui_logic.get_days_remaining(roc_date)
        assert days == 10


# ==================== sanitize ====================

class TestSanitizeExcelValue:
    """sanitize_excel_value 的公式注入防護"""

    @pytest.mark.parametrize("evil", ["=1+1", "+2", "-3", "@SUM(A1)"])
    def test_危險字元開頭加單引號前綴(self, evil):
        assert ui_logic.sanitize_excel_value(evil) == "'" + evil

    def test_tab與回車因lstrip會被清掉現行為不加前綴(self):
        # 現行實作先 lstrip() 再檢查，導致 \t / \r 開頭會被清掉而不會被攔
        # 此為已知行為，搬移時保持原樣；若要修應改為僅 lstrip 空白
        assert ui_logic.sanitize_excel_value("\t123") == "\t123"
        assert ui_logic.sanitize_excel_value("\r123") == "\r123"
        # 但若前面有空白再接 \t，lstrip 後仍為 \t 開頭？實測仍會被清掉
        # 所以此分支僅記錄現行行為，非理想安全行為

    def test_前面有空白的等號仍要攔(self):
        assert ui_logic.sanitize_excel_value(" =1+1") == "' =1+1"
        assert ui_logic.sanitize_excel_value("  @test") == "'  @test"

    def test_非字串型別原樣回傳(self):
        assert ui_logic.sanitize_excel_value(123) == 123
        assert ui_logic.sanitize_excel_value(3.14) == 3.14
        assert ui_logic.sanitize_excel_value(None) is None
        assert ui_logic.sanitize_excel_value(["a"]) == ["a"]

    def test_正常中文字串不被改動(self):
        assert ui_logic.sanitize_excel_value("臺北市政府") == "臺北市政府"
        assert ui_logic.sanitize_excel_value("AI 標案") == "AI 標案"

    def test_正常前綴非危險字元不變(self):
        assert ui_logic.sanitize_excel_value("正常文字") == "正常文字"
        assert ui_logic.sanitize_excel_value(" 正常") == " 正常"


class TestSanitizeRows:
    """sanitize_rows 對每欄做清洗且不改原資料"""

    def test_每列每個字串欄位皆清洗(self):
        rows = [{"標案名稱": "=1+1", "預算金額": "100萬", "數字": 123}]
        out = ui_logic.sanitize_rows(rows)
        assert out[0]["標案名稱"] == "'=1+1"
        assert out[0]["預算金額"] == "100萬"
        assert out[0]["數字"] == 123

    def test_不改動原始資料(self):
        rows = [{"標案名稱": "=hack"}]
        out = ui_logic.sanitize_rows(rows)
        assert rows[0]["標案名稱"] == "=hack"
        assert out[0]["標案名稱"] == "'=hack"

    def test_空列回空(self):
        assert ui_logic.sanitize_rows([]) == []


# ==================== validate_keywords ====================

class TestValidateKeywords:
    """validate_keywords 的長度與數量檢查"""

    def test_超過500字元截斷且回警告(self):
        raw = "a " * 300  # 600 字元含空白
        cleaned, warn = ui_logic.validate_keywords(raw)
        assert len(cleaned) <= 500
        assert "截斷至 500 字元" in warn

    def test_超過100組截斷至100組(self):
        raw = " ".join([f"k{i}" for i in range(105)])
        cleaned, warn = ui_logic.validate_keywords(raw)
        assert len(cleaned.split()) == 100
        assert "100 組" in warn

    def test_單組超過30字回警告但不截斷(self):
        long_word = "a" * 31
        raw = f"正常 {long_word} 測試"
        cleaned, warn = ui_logic.validate_keywords(raw)
        # 現行行為：回警告但不截斷
        assert cleaned == raw
        assert "過長" in warn

    def test_逗號與空白混合分隔(self):
        raw = "AI,人工智慧  資安, 資訊"
        cleaned, warn = ui_logic.validate_keywords(raw)
        assert warn is None
        # 內部以空白與逗號切分，應得到 4 組
        words = cleaned.replace(",", " ").split()
        assert len(words) == 4

    def test_正常輸入回None警告(self):
        raw = "AI 人工智慧 資安"
        cleaned, warn = ui_logic.validate_keywords(raw)
        assert cleaned == raw
        assert warn is None

    def test_恰好500字元不截斷(self):
        raw = " ".join(["abca"] * 99 + ["abcde"])  # 4*99+5+99=500，100組且每組≤30
        assert len(raw) == 500
        cleaned, warn = ui_logic.validate_keywords(raw)
        assert cleaned == raw
        assert warn is None

    def test_恰好100組不截斷(self):
        raw = " ".join([f"k{i}" for i in range(100)])
        cleaned, warn = ui_logic.validate_keywords(raw)
        assert warn is None
        assert len(cleaned.split()) == 100


# ==================== build_advanced_display_rows ====================

def _make_tender(**overrides):
    """產生測試用標案 dict 的 helper，含預設值"""
    base = {
        "招標機關": "測試機關",
        "標案名稱": "測試標案",
        "標案案號": "A123",
        "命中關鍵字": "AI",
        "預算金額": "5000000",
        "截止投標": (date.today() + timedelta(days=10)).strftime("%Y/%m/%d"),
        "決標方式": "最低標",
        "決標方式來源": core.AWARD_SOURCE_MIRROR,
        "pk": "pk_test",
        "是否為最低標": "是",
        "採購性質": "勞務類",
        "是否為勞務類": "是",
    }
    # 依決標方式自動調整 是否為最低標，避免 filter_tenders 誤判
    if "決標方式" in overrides:
        dm = overrides["決標方式"]
        if "最低標" in dm:
            base["是否為最低標"] = "是"
        else:
            base["是否為最低標"] = "否"
    base.update(overrides)
    # 若 caller 明確指定是否為最低標，則以 caller 為準（已在 update 後）
    return base


class TestBuildAdvancedDisplayRows:
    """build_advanced_display_rows 的多維度過濾"""

    def test_預算區間邊界恰好等於上下限皆納入(self):
        t = _make_tender(預算金額="1000000")  # 100 萬
        # 100 萬 = 100 萬元，設區間 100~100 應納入
        assert len(ui_logic.build_advanced_display_rows([t], min_budget_wan=100, max_budget_wan=100)) == 1
        # 邊界 99~101 也應納入
        assert len(ui_logic.build_advanced_display_rows([t], min_budget_wan=99, max_budget_wan=101)) == 1
        # 超出 101 應排除
        assert len(ui_logic.build_advanced_display_rows([t], min_budget_wan=101, max_budget_wan=200)) == 0

    def test_預算未公開時有下限則排除無下限則保留(self):
        t = _make_tender(預算金額="")
        # parse_amount("") -> -1.0
        assert core.parse_amount("") == -1.0
        # 無下限應保留
        assert len(ui_logic.build_advanced_display_rows([t], min_budget_wan=0)) == 1
        # 有下限應排除
        assert len(ui_logic.build_advanced_display_rows([t], min_budget_wan=1)) == 0

    def test_急迫度五檔邊界值(self):
        # 3天內
        t3 = _make_tender(截止投標=(date.today() + timedelta(days=3)).strftime("%Y/%m/%d"))
        t4 = _make_tender(截止投標=(date.today() + timedelta(days=4)).strftime("%Y/%m/%d"))
        t7 = _make_tender(截止投標=(date.today() + timedelta(days=7)).strftime("%Y/%m/%d"))
        t8 = _make_tender(截止投標=(date.today() + timedelta(days=8)).strftime("%Y/%m/%d"))
        t14 = _make_tender(截止投標=(date.today() + timedelta(days=14)).strftime("%Y/%m/%d"))
        t15 = _make_tender(截止投標=(date.today() + timedelta(days=15)).strftime("%Y/%m/%d"))
        t_expired = _make_tender(截止投標=(date.today() - timedelta(days=1)).strftime("%Y/%m/%d"))
        t_invalid = _make_tender(截止投標="待補")

        # 🔥 3天內
        assert len(ui_logic.build_advanced_display_rows([t3], urgency="🔥 3天內即將截標 (≤3天)")) == 1
        assert len(ui_logic.build_advanced_display_rows([t4], urgency="🔥 3天內即將截標 (≤3天)")) == 0
        assert len(ui_logic.build_advanced_display_rows([t_expired], urgency="🔥 3天內即將截標 (≤3天)")) == 0
        assert len(ui_logic.build_advanced_display_rows([t_invalid], urgency="🔥 3天內即將截標 (≤3天)")) == 0

        # ⏳ 7天內
        assert len(ui_logic.build_advanced_display_rows([t7], urgency="⏳ 7天內截標 (≤7天)")) == 1
        assert len(ui_logic.build_advanced_display_rows([t8], urgency="⏳ 7天內截標 (≤7天)")) == 0

        # 📅 充足 8~14天
        assert len(ui_logic.build_advanced_display_rows([t8], urgency="📅 充足 (8~14天)")) == 1
        assert len(ui_logic.build_advanced_display_rows([t14], urgency="📅 充足 (8~14天)")) == 1
        assert len(ui_logic.build_advanced_display_rows([t7], urgency="📅 充足 (8~14天)")) == 0
        assert len(ui_logic.build_advanced_display_rows([t15], urgency="📅 充足 (8~14天)")) == 0

        # 🗓️ 充裕 >14天
        assert len(ui_logic.build_advanced_display_rows([t15], urgency="🗓️ 充裕 (>14天)")) == 1
        assert len(ui_logic.build_advanced_display_rows([t14], urgency="🗓️ 充裕 (>14天)")) == 0

        # ⌛ 排除已截標
        assert len(ui_logic.build_advanced_display_rows([t_expired], urgency="⌛ 排除已截標")) == 0
        assert len(ui_logic.build_advanced_display_rows([t3], urgency="⌛ 排除已截標")) == 1
        assert len(ui_logic.build_advanced_display_rows([t_invalid], urgency="⌛ 排除已截標")) == 1

    def test_決標狀態三選項(self):
        confirmed = _make_tender(決標方式="最低標", 決標方式來源=core.AWARD_SOURCE_MIRROR)
        pending = _make_tender(決標方式="公開取得 (待確認)", 決標方式來源=core.AWARD_SOURCE_ESTIMATED)
        # 確保 pending 判定正確
        assert ui_logic.is_award_pending(pending) is True
        assert core.is_award_confirmed(confirmed) is True

        assert len(ui_logic.build_advanced_display_rows([confirmed, pending], award_status="全部")) == 2
        assert len(ui_logic.build_advanced_display_rows([confirmed, pending], award_status="✅ 僅已確認 (鏡像/官方)")) == 1
        assert ui_logic.build_advanced_display_rows([confirmed, pending], award_status="✅ 僅已確認 (鏡像/官方)")[0] == confirmed
        assert len(ui_logic.build_advanced_display_rows([confirmed, pending], award_status="🟠 僅待確認 (推估)")) == 1
        assert ui_logic.build_advanced_display_rows([confirmed, pending], award_status="🟠 僅待確認 (推估)")[0] == pending

    def test_hide_pending_filter濾掉待確認(self):
        pending = _make_tender(決標方式="公開取得 (待確認)", 決標方式來源=core.AWARD_SOURCE_ESTIMATED)
        confirmed = _make_tender(決標方式="最低標", 決標方式來源=core.AWARD_SOURCE_MIRROR)
        result = ui_logic.build_advanced_display_rows([pending, confirmed], hide_pending_filter=True)
        assert len(result) == 1
        assert result[0] == confirmed

    def test_文字搜尋大小寫不敏感且四欄位皆可命中(self):
        t = _make_tender(招標機關="臺北市政府", 標案名稱="AI 智慧平台", 標案案號="A001", 命中關鍵字="資訊")
        assert len(ui_logic.build_advanced_display_rows([t], query="ai")) == 1
        assert len(ui_logic.build_advanced_display_rows([t], query="臺北市")) == 1
        assert len(ui_logic.build_advanced_display_rows([t], query="a001")) == 1
        assert len(ui_logic.build_advanced_display_rows([t], query="資訊")) == 1
        assert len(ui_logic.build_advanced_display_rows([t], query="不存在")) == 0

    def test_機關過濾為子字串比對(self):
        t = _make_tender(招標機關="臺北市政府資訊局")
        # any(sel in agency) 的行為，子字串即命中
        assert len(ui_logic.build_advanced_display_rows([t], selected_agencies=["資訊局"])) == 1
        assert len(ui_logic.build_advanced_display_rows([t], selected_agencies=["臺北市"])) == 1
        assert len(ui_logic.build_advanced_display_rows([t], selected_agencies=["高雄市"])) == 0

    def test_多條件同時生效為AND(self):
        t = _make_tender(招標機關="測試機關", 預算金額="5000000", 截止投標=(date.today() + timedelta(days=2)).strftime("%Y/%m/%d"))
        # 預算符合但急迫度不符 → 排除
        assert len(ui_logic.build_advanced_display_rows([t], min_budget_wan=100, max_budget_wan=1000, urgency="📅 充足 (8~14天)")) == 0
        # 兩者皆符合 → 保留
        assert len(ui_logic.build_advanced_display_rows([t], min_budget_wan=100, max_budget_wan=1000, urgency="🔥 3天內即將截標 (≤3天)")) == 1

    def test_預算上限邊界精確(self):
        t_exact = _make_tender(預算金額="2000000")  # 200 萬
        assert len(ui_logic.build_advanced_display_rows([t_exact], min_budget_wan=0, max_budget_wan=200)) == 1
        assert len(ui_logic.build_advanced_display_rows([t_exact], min_budget_wan=0, max_budget_wan=199)) == 0


# ==================== tenders_to_dataframe ====================

class TestTendersToDataframe:
    """tenders_to_dataframe 的欄位與前綴邏輯"""

    def test_空清單回空DataFrame含正確欄位(self):
        df = ui_logic.tenders_to_dataframe([])
        assert list(df.columns) == ui_logic.DISPLAY_COLUMNS
        assert len(df) == 0

    def test_待確認標案加橙色前綴(self):
        t = _make_tender(決標方式="公開取得 (待確認)", 決標方式來源=core.AWARD_SOURCE_ESTIMATED)
        df = ui_logic.tenders_to_dataframe([t], active_award_target="最低標")
        assert df.iloc[0]["決標方式"].startswith(ui_logic.PENDING_PREFIX)

    def test_已確認但不符決標篩選加灰色前綴(self):
        # 當前篩選為 最低標，但標案為 最有利標 且已確認，應加 ⚪
        t = _make_tender(決標方式="最有利標", 決標方式來源=core.AWARD_SOURCE_MIRROR)
        df = ui_logic.tenders_to_dataframe([t], active_award_target="最低標")
        assert df.iloc[0]["決標方式"].startswith(ui_logic.DISQUALIFIED_PREFIX)

    def test_已確認且符合篩選不加前綴(self):
        t = _make_tender(決標方式="最低標", 決標方式來源=core.AWARD_SOURCE_MIRROR)
        df = ui_logic.tenders_to_dataframe([t], active_award_target="最低標")
        assert not df.iloc[0]["決標方式"].startswith(ui_logic.PENDING_PREFIX)
        assert not df.iloc[0]["決標方式"].startswith(ui_logic.DISQUALIFIED_PREFIX)

    def test_預算未公開轉0元(self):
        t = _make_tender(預算金額="")
        df = ui_logic.tenders_to_dataframe([t])
        assert df.iloc[0]["預算金額"] == 0

    def test_命中關鍵字空值轉破折號(self):
        t = _make_tender(命中關鍵字="")
        df = ui_logic.tenders_to_dataframe([t])
        assert df.iloc[0]["命中關鍵字"] == "—"

    def test_預設參數為最低標(self):
        t = _make_tender(決標方式="最有利標", 決標方式來源=core.AWARD_SOURCE_MIRROR)
        df = ui_logic.tenders_to_dataframe([t])
        # 預設 最低標 時，最有利標應被標為 ⚪
        assert df.iloc[0]["決標方式"].startswith(ui_logic.DISQUALIFIED_PREFIX)


# ==================== 看板聚合 ====================

class TestBudgetTierFrame:
    """budget_tier_frame 的六檔級距與邊界"""

    def test_六檔加未公開共七列(self):
        df = ui_logic.budget_tier_frame([])
        assert len(df) == 7
        assert "未公開 / 0 元" in df["級距"].values

    def test_邊界值分箱正確(self):
        # 999,999 → <100萬
        # 1,000,000 → 100萬~500萬
        # 199,999,999 → 5,000萬~2億
        # 200,000,000 → ≥2億
        tenders = [
            _make_tender(預算金額="999999"),
            _make_tender(預算金額="1000000"),
            _make_tender(預算金額="199999999"),
            _make_tender(預算金額="200000000"),
            _make_tender(預算金額=""),
        ]
        df = ui_logic.budget_tier_frame(tenders)
        assert int(df.loc[df["級距"] == "< 100萬 (公告金額以下)", "標案筆數"].iloc[0]) == 1
        assert int(df.loc[df["級距"] == "100萬 ~ 500萬", "標案筆數"].iloc[0]) == 1
        assert int(df.loc[df["級距"] == "5,000萬 ~ 2億", "標案筆數"].iloc[0]) == 1
        assert int(df.loc[df["級距"] == "≥ 2億 (巨額採購)", "標案筆數"].iloc[0]) == 1
        assert int(df.loc[df["級距"] == "未公開 / 0 元", "標案筆數"].iloc[0]) == 1

    def test_各級距中間值(self):
        tenders = [
            _make_tender(預算金額="3000000"),  # 100~500
            _make_tender(預算金額="7000000"),  # 500~1000
            _make_tender(預算金額="20000000"),  # 1000~5000
        ]
        df = ui_logic.budget_tier_frame(tenders)
        assert int(df.loc[df["級距"] == "100萬 ~ 500萬", "標案筆數"].iloc[0]) == 1
        assert int(df.loc[df["級距"] == "500萬 ~ 1,000萬", "標案筆數"].iloc[0]) == 1
        assert int(df.loc[df["級距"] == "1,000萬 ~ 5,000萬 (查核金額)", "標案筆數"].iloc[0]) == 1


class TestAwardCompositionFrame:
    """award_composition_frame 的分類"""

    def test_分類正確(self):
        pending = _make_tender(決標方式="公開取得 (待確認)", 決標方式來源=core.AWARD_SOURCE_ESTIMATED)
        lowest = _make_tender(決標方式="最低標", 決標方式來源=core.AWARD_SOURCE_MIRROR)
        best = _make_tender(決標方式="最有利標", 決標方式來源=core.AWARD_SOURCE_MIRROR)
        other = _make_tender(決標方式="議價", 決標方式來源=core.AWARD_SOURCE_MIRROR)
        df = ui_logic.award_composition_frame([pending, lowest, best, other])
        # 應有四類
        assert len(df) == 4
        assert int(df.loc[df["決標方式類別"] == "推估待確認 (🟠)", "筆數"].iloc[0]) == 1
        assert int(df.loc[df["決標方式類別"] == "最低標", "筆數"].iloc[0]) == 1
        assert int(df.loc[df["決標方式類別"] == "最有利標/評選", "筆數"].iloc[0]) == 1
        assert int(df.loc[df["決標方式類別"] == "其他方式", "筆數"].iloc[0]) == 1

    def test_空清單回空DataFrame(self):
        df = ui_logic.award_composition_frame([])
        assert len(df) == 0


class TestTopAgenciesFrame:
    """top_agencies_frame 依筆數遞減且最多 top_n"""

    def test_依筆數遞減排序(self):
        tenders = []
        for i in range(5):
            tenders.append(_make_tender(招標機關="A機關", 預算金額="1000000"))
        for i in range(3):
            tenders.append(_make_tender(招標機關="B機關", 預算金額="1000000"))
        for i in range(1):
            tenders.append(_make_tender(招標機關="C機關", 預算金額="1000000"))
        df = ui_logic.top_agencies_frame(tenders, top_n=10)
        assert df.iloc[0]["招標機關"] == "A機關"
        assert df.iloc[0]["標案筆數"] == 5
        assert df.iloc[1]["招標機關"] == "B機關"
        assert df.iloc[2]["招標機關"] == "C機關"

    def test_最多10筆(self):
        tenders = [_make_tender(招標機關=f"機關{i}", 預算金額="1000000") for i in range(15)]
        df = ui_logic.top_agencies_frame(tenders, top_n=10)
        assert len(df) == 10

    def test_累積預算正確(self):
        tenders = [
            _make_tender(招標機關="A機關", 預算金額="1000000"),
            _make_tender(招標機關="A機關", 預算金額="2000000"),
        ]
        df = ui_logic.top_agencies_frame(tenders)
        assert df.iloc[0]["累積預算(萬元)"] == 300.0


class TestKeywordRankingFrame:
    """keyword_ranking_frame 以 `、` 拆分並忽略 `—`"""

    def test_正確拆分多關鍵字(self):
        tenders = [
            _make_tender(命中關鍵字="AI、資訊"),
            _make_tender(命中關鍵字="AI"),
            _make_tender(命中關鍵字="資訊、網站、AI"),
        ]
        df = ui_logic.keyword_ranking_frame(tenders)
        assert int(df.loc[df["關鍵字"] == "AI", "命中次數"].iloc[0]) == 3
        assert int(df.loc[df["關鍵字"] == "資訊", "命中次數"].iloc[0]) == 2

    def test_忽略破折號(self):
        tenders = [
            _make_tender(命中關鍵字="—"),
            _make_tender(命中關鍵字="AI"),
        ]
        df = ui_logic.keyword_ranking_frame(tenders)
        assert "—" not in df["關鍵字"].values
        assert len(df) == 1

    def test_top_n限制(self):
        tenders = [_make_tender(命中關鍵字=f"KW{i}") for i in range(20)]
        df = ui_logic.keyword_ranking_frame(tenders, top_n=12)
        assert len(df) == 12

    def test_空關鍵字回空(self):
        df = ui_logic.keyword_ranking_frame([_make_tender(命中關鍵字="—")])
        assert df.empty


class TestUrgencyBinsFrame:
    """urgency_bins_frame 的五檔分箱"""

    def test_五檔皆有(self):
        df = ui_logic.urgency_bins_frame([])
        assert len(df) == 5

    def test_分箱邏輯(self):
        t3 = _make_tender(截止投標=(date.today() + timedelta(days=2)).strftime("%Y/%m/%d"))
        t7 = _make_tender(截止投標=(date.today() + timedelta(days=6)).strftime("%Y/%m/%d"))
        t10 = _make_tender(截止投標=(date.today() + timedelta(days=10)).strftime("%Y/%m/%d"))
        t20 = _make_tender(截止投標=(date.today() + timedelta(days=20)).strftime("%Y/%m/%d"))
        t_expired = _make_tender(截止投標=(date.today() - timedelta(days=1)).strftime("%Y/%m/%d"))
        t_invalid = _make_tender(截止投標="待補")
        df = ui_logic.urgency_bins_frame([t3, t7, t10, t20, t_expired, t_invalid])
        assert int(df.loc[df["急迫度分類"] == "🔥 3天內即將截標", "標案數量"].iloc[0]) == 1
        assert int(df.loc[df["急迫度分類"] == "⏳ 4~7天內截標", "標案數量"].iloc[0]) == 1
        assert int(df.loc[df["急迫度分類"] == "📅 8~14天內截標", "標案數量"].iloc[0]) == 1
        assert int(df.loc[df["急迫度分類"] == "🗓️ 14天以上", "標案數量"].iloc[0]) == 1
        assert int(df.loc[df["急迫度分類"] == "⌛ 已截標 / 截止日期未定", "標案數量"].iloc[0]) == 2

    def test_邊界3與4天分屬不同箱(self):
        t3 = _make_tender(截止投標=(date.today() + timedelta(days=3)).strftime("%Y/%m/%d"))
        t4 = _make_tender(截止投標=(date.today() + timedelta(days=4)).strftime("%Y/%m/%d"))
        df = ui_logic.urgency_bins_frame([t3, t4])
        assert int(df.loc[df["急迫度分類"] == "🔥 3天內即將截標", "標案數量"].iloc[0]) == 1
        assert int(df.loc[df["急迫度分類"] == "⏳ 4~7天內截標", "標案數量"].iloc[0]) == 1


class TestRemainingDays:
    """剩餘天數欄位（C2）六個分支與 emoji 門檻"""

    def test_截止日為空回破折號(self):
        assert ui_logic.format_remaining_days("") == "—"
        assert ui_logic.format_remaining_days(None) == "—"
        assert ui_logic.format_remaining_days("待補") == "—"

    def test_已截標回已截標(self):
        past = (date.today() - timedelta(days=1)).strftime("%Y/%m/%d")
        assert ui_logic.format_remaining_days(past) == "已截標"
        assert ui_logic.format_remaining_days((date.today() - timedelta(days=10)).strftime("%Y/%m/%d")) == "已截標"

    def test_3天內為火焰(self):
        for d in [0, 1, 2, 3]:
            ds = (date.today() + timedelta(days=d)).strftime("%Y/%m/%d")
            assert ui_logic.format_remaining_days(ds) == f"🔥 {d} 天"

    def test_4至7天為沙漏(self):
        for d in [4, 5, 6, 7]:
            ds = (date.today() + timedelta(days=d)).strftime("%Y/%m/%d")
            assert ui_logic.format_remaining_days(ds) == f"⏳ {d} 天"

    def test_8至14天為月曆(self):
        for d in [8, 10, 14]:
            ds = (date.today() + timedelta(days=d)).strftime("%Y/%m/%d")
            assert ui_logic.format_remaining_days(ds) == f"📅 {d} 天"

    def test_大於14天為日曆(self):
        for d in [15, 20, 30]:
            ds = (date.today() + timedelta(days=d)).strftime("%Y/%m/%d")
            assert ui_logic.format_remaining_days(ds) == f"🗓️ {d} 天"

    def test_門檻與篩選器一致(self):
        # 篩選器選 🔥 3天內 時，表格內應只剩 🔥
        t_fire = _make_tender(截止投標=(date.today() + timedelta(days=2)).strftime("%Y/%m/%d"))
        t_late = _make_tender(截止投標=(date.today() + timedelta(days=10)).strftime("%Y/%m/%d"))
        filtered = ui_logic.build_advanced_display_rows([t_fire, t_late], urgency="🔥 3天內即將截標 (≤3天)")
        df = ui_logic.tenders_to_dataframe(filtered)
        assert len(df) == 1
        assert df.iloc[0]["剩餘天數"].startswith("🔥")

    def test_tenders_to_dataframe剩餘天數欄存在且緊接截止投標後(self):
        df = ui_logic.tenders_to_dataframe([_make_tender()])
        assert "剩餘天數" in df.columns
        # 緊接在 截止投標 之後
        cols = list(df.columns)
        assert cols.index("剩餘天數") == cols.index("截止投標") + 1

    def test_剩餘天數排序提示(self):
        # 驗證 app.py 的 TENDER_COLUMN_CONFIG 對剩餘天數有 help 提示
        import pathlib as pl
        app_text = pl.Path("app.py").read_text(encoding="utf-8")
        assert "剩餘天數" in app_text
        assert "排序請用" in app_text


class TestKpiSummary:
    """kpi_summary 的聚合與 -0 萬元 bug 修正"""

    def test_正常混合資料(self):
        tenders = [
            _make_tender(預算金額="1000000", 決標方式="最低標", 決標方式來源=core.AWARD_SOURCE_MIRROR),
            _make_tender(預算金額="2000000", 決標方式="最有利標", 決標方式來源=core.AWARD_SOURCE_MIRROR),
            _make_tender(預算金額="", 決標方式="最低標", 決標方式來源=core.AWARD_SOURCE_MIRROR),
        ]
        kpi = ui_logic.kpi_summary(tenders)
        assert kpi["total_budget"] == 3000000.0
        assert kpi["avg_budget"] == 1500000.0
        assert kpi["max_amount"] == 2000000.0
        assert kpi["max_tender_name"] == tenders[1]["標案名稱"]
        assert kpi["total_count"] == 3
        assert kpi["confirmed_count"] == 3
        assert kpi["confirmed_ratio"] == 100.0

    def test_全部預算未公開_max_amount為0不得為負(self):
        tenders = [
            _make_tender(預算金額="", 決標方式="最低標", 決標方式來源=core.AWARD_SOURCE_MIRROR),
            _make_tender(預算金額="", 決標方式="最低標", 決標方式來源=core.AWARD_SOURCE_MIRROR),
        ]
        kpi = ui_logic.kpi_summary(tenders)
        assert kpi["max_amount"] == 0
        assert kpi["max_amount"] != -1.0
        assert kpi["total_budget"] == 0
        assert kpi["avg_budget"] == 0
        # 顯示時應為 0 萬元而非 -0 萬元
        assert f"{kpi['max_amount']/10000:.0f} 萬元" == "0 萬元"

    def test_空清單全部回0不拋例外(self):
        kpi = ui_logic.kpi_summary([])
        assert kpi["total_budget"] == 0
        assert kpi["avg_budget"] == 0
        assert kpi["max_amount"] == 0
        assert kpi["max_tender_name"] == ""
        assert kpi["confirmed_count"] == 0
        assert kpi["confirmed_ratio"] == 0
        assert kpi["total_count"] == 0

    def test_confirmed_ratio分母為全部且空清單為0(self):
        tenders = [
            _make_tender(決標方式="最低標", 決標方式來源=core.AWARD_SOURCE_MIRROR),
            _make_tender(決標方式="公開取得 (待確認)", 決標方式來源=core.AWARD_SOURCE_ESTIMATED),
        ]
        kpi = ui_logic.kpi_summary(tenders)
        assert kpi["confirmed_count"] == 1
        assert kpi["confirmed_ratio"] == 50.0
        assert kpi["total_count"] == 2

    def test_只有一筆時平均等於總和(self):
        t = _make_tender(預算金額="5000000")
        kpi = ui_logic.kpi_summary([t])
        assert kpi["total_budget"] == 5000000.0
        assert kpi["avg_budget"] == 5000000.0
        assert kpi["max_amount"] == 5000000.0


class TestUiLogicNoStreamlit:
    """確保 ui_logic 沒有依賴前端框架"""

    def test_無streamlit字串(self):
        import pathlib
        text = pathlib.Path("ui_logic.py").read_text(encoding="utf-8")
        assert "streamlit" not in text.lower()
        assert "st." not in text or "st.session_state" not in text
