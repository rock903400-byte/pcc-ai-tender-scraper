# -*- coding: utf-8 -*-
"""
追蹤清單持久化與解析（由 app.py 抽離）。

原位於 app.py 的 watchlist_path / load_watchlist / save_watchlist /
_resolve_watchlist_rows 邏輯，改為可測試的純函式。
儲存改走 core.save_json_dict 原子寫，避免 FileLock 外掛依賴。
"""
import json
import os
from datetime import datetime

import pcc_core as core


WATCHLIST_FILENAME = "watchlist.json"


def watchlist_path(output_dir: str) -> str:
    return os.path.join(output_dir, WATCHLIST_FILENAME)


def load_watchlist(output_dir: str) -> dict:
    """載入追蹤清單，自動相容舊格式（value 直接是 tender dict）。"""
    path = watchlist_path(output_dir)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    return {}
                upgraded = {}
                need_upgrade = False
                for k, v in data.items():
                    if isinstance(v, dict) and "snapshot" in v and "added_at" in v:
                        upgraded[k] = v
                    elif isinstance(v, dict):
                        upgraded[k] = {"added_at": datetime.now().isoformat(), "snapshot": v}
                        need_upgrade = True
                    else:
                        upgraded[k] = v
                if need_upgrade:
                    try:
                        core.save_json_dict(upgraded, path)
                    except Exception:
                        pass
                return upgraded
    except Exception:
        pass
    return {}


def save_watchlist(output_dir: str, wl: dict) -> bool:
    """原子儲存追蹤清單，沿用 core.save_json_dict 的 tmp+replace 策略。"""
    path = watchlist_path(output_dir)
    return core.save_json_dict(wl, path)


def resolve_watchlist_rows(watchlist: dict, tenders_by_pk: dict, award_cache: dict = None) -> list:
    """
    解析追蹤清單為顯示用列，回填最新狀態。

    依序嘗試：
      1. tenders_by_pk（本次搜尋最新結果）
      2. award_cache 回填決標方式（core.apply_award_cache）
      3. 退回 snapshot
    每筆回填 _watchlist_source 供 UI 標示。
    """
    if award_cache is None:
        award_cache = {}
    resolved = []
    for pk, entry in (watchlist or {}).items():
        snapshot = entry.get("snapshot", entry) if isinstance(entry, dict) and "snapshot" in entry else entry
        latest = None
        try:
            latest = (tenders_by_pk or {}).get(pk)
        except Exception:
            latest = None
        if latest is not None:
            tender = dict(latest)
            try:
                tmp = [dict(tender)]
                core.apply_award_cache(tmp, award_cache)
                tender = tmp[0]
            except Exception:
                pass
            tender["_watchlist_source"] = "最新搜尋"
            resolved.append(tender)
            continue
        # 嘗試 award_cache 回填（即使不在本次搜尋結果中）
        if snapshot and isinstance(snapshot, dict):
            # pk 也可能以案號存（fallback），試雙 key
            cache_hit = False
            for lookup_key in (pk, snapshot.get("標案案號", "")):
                if lookup_key and lookup_key in award_cache:
                    cache_hit = True
                    break
            if cache_hit:
                try:
                    tmp = [dict(snapshot)]
                    core.apply_award_cache(tmp, award_cache)
                    tender = tmp[0]
                    # apply 正常會覆蓋，若因 TTL 失效則手動補來源
                    if tender.get("決標方式來源") in core.CONFIRMED_SOURCES:
                        tender["_watchlist_source"] = "快取回填"
                        resolved.append(tender)
                        continue
                except Exception:
                    pass
                # 手動回填 fallback
                try:
                    tender = dict(snapshot)
                    for lk in (pk, snapshot.get("標案案號", "")):
                        ce = award_cache.get(lk)
                        if isinstance(ce, dict) and ce.get("決標方式"):
                            if "決標方式" in ce:
                                tender["決標方式"] = ce["決標方式"]
                            if "決標方式來源" in ce:
                                tender["決標方式來源"] = ce["決標方式來源"]
                            break
                    tender["_watchlist_source"] = "快取回填"
                    resolved.append(tender)
                    continue
                except Exception:
                    pass
        if isinstance(snapshot, dict):
            tender = dict(snapshot)
            tender["_watchlist_source"] = "快照"
            resolved.append(tender)
        else:
            resolved.append(snapshot if isinstance(snapshot, dict) else {})
    return resolved
