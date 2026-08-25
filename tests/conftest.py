# -*- coding: utf-8 -*-
"""共用的測試設定與 fixture 載入工具。"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


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
