# -*- coding: utf-8 -*-
"""共用的測試設定與 fixture 載入工具。"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pcc_core as core  # noqa: E402  (要先把專案根目錄放進 sys.path)
import pcc_mirror as mirror  # noqa: E402


_REAL_FETCH_JSON = mirror.fetch_json


@pytest.fixture
def real_fetch_json():
    """下面的 offline fixture 會把 mirror.fetch_json 換掉；要測它本身時用這個拿回真貨。"""
    return _REAL_FETCH_JSON


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """
    整份測試離線化。

    校驗流程現在是「先問公開資料鏡像、查不到才退回官網詳細頁」，
    沒有這道保險的話，只換掉 core.fetch_award_method_status 的既有測試
    會真的打到鏡像 API——慢、看網路臉色，而且會把測試變成對外部服務的壓力測試。

    預設讓鏡像一律回 "error"（＝查無此案），流程就會走官網那條路，
    要驗證鏡像行為的測試自己把 mirror.fetch_json 換成假的即可。
    """
    monkeypatch.setattr(mirror, "polite_delay", lambda: None)
    monkeypatch.setattr(mirror, "fetch_json", lambda path, params: (None, "error"))

    def _no_network(*args, **kwargs):
        raise AssertionError("測試不得連線外部網路")

    monkeypatch.setattr(core.opener, "open", _no_network)


def load_fixture(name: str) -> str:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="session")
def search_html():
    """單頁搜尋結果（關鍵字 AI，7 筆）。"""
    return load_fixture("search_result.html")


@pytest.fixture(scope="session")
def paged_html():
    """多頁搜尋結果（關鍵字 系統，共 138 筆 / 3 頁，本頁 50 筆）。"""
    return load_fixture("search_paged.html")


@pytest.fixture(scope="session")
def detail_html():
    """標案詳細頁（決標方式為「最低標」）。"""
    return load_fixture("detail.html")


@pytest.fixture(scope="session")
def captcha_html():
    """詳細頁被頻率防護擋下時回傳的「驗證碼檢核」頁。"""
    return load_fixture("validate_captcha.html")
