# -*- coding: utf-8 -*-
"""
政府電子採購網 - AI 與資訊勞務最低標標案爬蟲 Flask 輕量版

取代 Streamlit 版，無需 streamlit/altair，純 Flask + 原生 HTML/JS + Chart.js。
核心爬取/解析/校驗邏輯仍共用 pcc_core / pcc_mirror / ui_logic / watchlist_utils / app_export。

啟動：
    python app.py
瀏覽器開啟 http://localhost:8502
"""
import os
import queue
import re
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_file

# PyInstaller frozen 支援：模板/靜態路徑與輸出目錄改以 exe 所在為基準
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    # 讓 Flask 在 _MEIPASS 中找到 templates/static（PyInstaller add-data 會解到 _MEIPASS）
    _bundle_dir = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _bundle_dir = BASE_DIR

import pcc_core as core
import watchlist_utils as wl_utils
from app_export import (
    auto_export_backup as flask_auto_export,
    build_csv_bytes,
    cached_csv_bytes,
    rows_fingerprint,
)
from config import DEFAULT_KEYWORDS, USE_MIRROR_SOURCE
from ui_logic import (
    DISPLAY_COLUMNS,
    KEYWORDS_MAX_CHARS,
    KEYWORDS_MAX_WORDS,
    KEYWORDS_MAX_WORD_LEN,
    SORT_OPTIONS,
    award_composition_frame,
    budget_tier_frame,
    build_advanced_display_rows,
    describe_filter,
    get_days_remaining,
    is_award_pending,
    keyword_ranking_frame,
    kpi_summary,
    sort_tenders,
    top_agencies_frame,
    urgency_bins_frame,
    validate_keywords,
)

core.install_ipv4_preference()

# 明確指定 template/static 路徑，兼容 onedir/onefile
app = Flask(__name__, template_folder=os.path.join(_bundle_dir, "templates"), static_folder=os.path.join(_bundle_dir, "static"))

# ==================== 常數 ====================
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

WATCHLIST_FILENAME = wl_utils.WATCHLIST_FILENAME
MAX_OUTPUT_KEEP = 20
RATE_LIMIT_SECONDS = 60

DEFAULT_DAYS_OPTIONS = ["1 (今日)", "3", "7", "14", "30", "60"]
ATTR_OPTIONS = ["勞務", "不限", "財物", "工程"]
AWARD_OPTIONS = ["最低標", "不限", "最有利標/評選"]
DATE_MODE_OPTIONS = [f"{core.DATE_MODE_SPDT} (現正招標中)", core.DATE_MODE_RANGE]

URGENCY_OPTIONS = [
    "全部",
    "🔥 3天內即將截標 (≤3天)",
    "⏳ 7天內截標 (≤7天)",
    "📅 充足 (8~14天)",
    "🗓️ 充裕 (>14天)",
    "⌛ 排除已截標",
]
AWARD_STATUS_FILTER_OPTIONS = [
    "全部",
    "✅ 僅已確認 (鏡像/官方)",
    "🟠 僅待確認 (推估)",
]

# ==================== 全域狀態（單機單人） ====================
app_state = {
    "tenders_all": [],
    "tenders_qualified": [],
    "tenders_keyword_hits": [],
    "tenders_by_pk": {},
    "active_filter_label": "勞務最低標",
    "active_attr_target": "勞務",
    "active_award_target": "最低標",
    "log_lines": [],
    "notices": [],
    "last_search_ts": 0.0,
}

job_store = {}  # job_id -> {thread, queue, stop_event, status, progress, logs, notices, result, type}
job_lock = threading.Lock()
state_lock = threading.Lock()

MAX_LOG_LINES = 1000
LOG_KEEP = 500

def award_cache_path():
    return core.award_cache_path(OUTPUT_DIR)

def pending_queue_path():
    return core.pending_queue_path(OUTPUT_DIR)

def settings_path():
    return core.settings_path(OUTPUT_DIR)

def append_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with state_lock:
        app_state["log_lines"].append(line)
        if len(app_state["log_lines"]) > MAX_LOG_LINES:
            app_state["log_lines"] = app_state["log_lines"][-LOG_KEEP:]

def add_notice(msg: str):
    with state_lock:
        if msg not in app_state["notices"]:
            app_state["notices"].append(msg)

def clear_notices():
    with state_lock:
        app_state["notices"] = []

def _default_keywords_str():
    return " ".join(DEFAULT_KEYWORDS)

def selected_date_type(date_mode: str) -> str:
    return core.DATE_TYPE_RANGE if str(date_mode).startswith(core.DATE_MODE_RANGE) else core.DATE_TYPE_SPDT

def refresh_datasets():
    with state_lock:
        all_t = app_state["tenders_all"]
        attr = app_state["active_attr_target"]
        award = app_state["active_award_target"]
        app_state["tenders_qualified"] = core.filter_tenders(all_t, attr, award)
        app_state["tenders_keyword_hits"] = core.filter_tenders(all_t, attr, award, require_keyword_hit=True)

# 初始載入 watchlist 與 settings
try:
    _watchlist = wl_utils.load_watchlist(OUTPUT_DIR)
except Exception:
    _watchlist = {}
app_state["watchlist"] = _watchlist

# ==================== 搜尋核心（沿用 Streamlit 版邏輯，去 st 依賴） ====================
SEARCH_PROGRESS_SHARE = 70

def run_search_job(job_id: str, keywords: list, days: int, target_attr: str, target_award: str, date_type: str, verify: bool, include_misses: bool):
    job = job_store.get(job_id)
    if not job:
        return
    q = job["queue"]
    stop_event = job["stop_event"]

    def qlog(msg: str):
        q.put(("log", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"))
        append_log(msg)

    def qprogress(pct: int):
        with job_lock:
            job["progress"] = int(pct)

    def qstatus(typ: str, msg: str):
        with job_lock:
            job["status"] = (typ, msg)

    def qnotice(msg: str):
        q.put(("notice", msg))
        add_notice(msg)

    try:
        if date_type == core.DATE_TYPE_RANGE:
            days = core.clamp_date_range_days(days, log=lambda m: qlog(m))
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        start_ad = start_date.strftime("%Y/%m/%d")
        end_ad = end_date.strftime("%Y/%m/%d")
        proctrg_cate = core.PROCTRG_CATE.get(target_attr)
        if date_type == core.DATE_TYPE_RANGE:
            qlog(f"🚀 全面掃描【{target_attr}】標案：公告日期 {start_ad} ~ {end_ad} (最近 {days} 天)")
        else:
            qlog(f"🚀 全面掃描【{target_attr}】標案：等標期內（現正招標中，站方會忽略日期區間）")
        qlog(f"🏷️ 標記關鍵字共 {len(keywords)} 組: {', '.join(keywords)}")
        qstatus("info", "🔍 搜尋中… 正在翻頁擷取標案")
        qprogress(5)

        def _on_page(done_pages, total_pages):
            pct = int(done_pages / max(total_pages, 1) * SEARCH_PROGRESS_SHARE)
            qprogress(pct)

        rows = core.search_pcc(
            "", start_ad, end_ad, proctrg_cate=proctrg_cate,
            date_type=date_type, log=lambda m: qlog(m),
            progress_cb=_on_page,
            should_stop=stop_event.is_set,
        )
        if stop_event.is_set():
            qlog("⏹ 已停止搜尋，以下為中斷前取得的部分結果。")

        unique_tenders = {}
        core.merge_by_tender_id(unique_tenders, rows, "")
        tenders_list = list(unique_tenders.values())
        core.tag_keywords(tenders_list, keywords)
        hits = sum(1 for t in tenders_list if t.get("命中關鍵字群"))
        qlog(f"📦 掃描完畢，共 {len(tenders_list)} 筆不重複標案（其中 {hits} 筆命中關鍵字）。")
        qprogress(SEARCH_PROGRESS_SHARE)

        cache_path = award_cache_path()
        cache = core.load_award_cache(cache_path)
        applied = core.apply_award_cache(tenders_list, cache)
        if applied:
            qlog(f"♻️ 由快取套用 {applied} 筆先前已確認的官方決標方式。")

        pending_rows = core.select_rows_for_enrichment(
            tenders_list, target_attr, target_award, limit=0,
            require_keyword_hit=not include_misses)
        if core.save_pending_queue(pending_rows, pending_queue_path()):
            qlog(f"🗂️ 待確認清單已更新（{len(pending_rows)} 筆）。")
        else:
            qlog(f"  ⚠️ 無法寫入待確認清單 {pending_queue_path()}。")

        if stop_event.is_set():
            verify = False
            qlog("⏹ 已略過深度校驗（搜尋已被停止）。")

        if verify and tenders_list:
            targets = core.select_rows_for_enrichment(
                tenders_list, target_attr, target_award,
                require_keyword_hit=not include_misses)
            is_cloud = os.getenv("STREAMLIT_SERVER_HEADLESS") == "1" or os.path.exists("/mount/src")
            if is_cloud and len(targets) > 30:
                qlog(f"  [Cloud] 候選 {len(targets)} 筆較多，本次僅校驗前 30 筆，剩餘可手動補齊。")
                targets = targets[:30]

            def _on_progress(done, total):
                share = 100 - SEARCH_PROGRESS_SHARE
                qprogress(SEARCH_PROGRESS_SHARE + int(done / total * share))
                if done % 5 == 0 or done == total:
                    qstatus("info", f"⚡ 校驗決標方式中… ({done}/{total} 筆候選)")

            if targets:
                qlog(f"⚡ 從 {len(tenders_list)} 筆中挑出 {len(targets)} 筆尚未確認的候選，校驗真實決標方式…")
                qstatus("info", f"⚡ 校驗決標方式中… ({len(targets)} 筆候選)")
                stats = core.enrich_actual_award_methods(
                    targets, progress_cb=_on_progress, log=lambda m: qlog(m),
                    cache=cache, cache_path=cache_path,
                    should_stop=stop_event.is_set,
                    use_mirror=USE_MIRROR_SOURCE)
                if stats["blocked"]:
                    qnotice(f"官網詳細頁額度已用盡，本次只確認 {stats['ok']} 筆。")
                    qlog(f"⛔ 校驗提前中止：本次確認 {stats['ok']} 筆，官網詳細頁額度已用盡。")
                else:
                    qlog(f"✅ 校驗完成：本次確認 {stats['ok']}/{stats['total']} 筆。")
            else:
                qlog("ℹ️ 無需校驗的候選（皆已確認或不符條件）。")
        elif not verify:
            qlog(f"ℹ️ 已關閉深度校驗，未快取的標案決標方式全部為「{core.AWARD_SOURCE_ESTIMATED}」。")

        core.finalize_keywords(tenders_list)
        qualified = core.filter_tenders(tenders_list, target_attr, target_award)
        keyword_hits = core.filter_tenders(tenders_list, target_attr, target_award, require_keyword_hit=True)
        qlog(f"🎯 符合【{target_attr} + {target_award}】共 {len(qualified)} 筆，其中命中關鍵字 {len(keyword_hits)} 筆。")

        pending = sum(1 for t in qualified if is_award_pending(t))
        if pending:
            qlog(f"⚠️ 精選中有 {pending} 筆決標方式仍是「公開取得 (待確認)」（🟠）。")
            qnotice(f"精選中有 {pending} 筆決標方式是推估的（🟠）——投標前請開啟詳細連結確認。")
        if stop_event.is_set():
            qnotice("本次搜尋被中斷，清單只涵蓋中斷前抓到的部分標案。")

        qprogress(100)
        was_stopped = stop_event.is_set()
        if was_stopped:
            qstatus("warning", f"⏹ 已停止：共 {len(tenders_list)} 筆標案，符合條件 {len(qualified)} 筆")
        else:
            qstatus("success", f"🎉 搜尋完成！共 {len(tenders_list)} 筆標案，符合條件 {len(qualified)} 筆，命中關鍵字 {len(keyword_hits)} 筆。")

        # 更新全域狀態
        with state_lock:
            app_state["tenders_all"] = tenders_list
            app_state["tenders_qualified"] = qualified
            app_state["tenders_keyword_hits"] = keyword_hits
            app_state["tenders_by_pk"] = {t["pk"]: t for t in tenders_list if t.get("pk")}
            app_state["active_attr_target"] = target_attr
            app_state["active_award_target"] = target_award
            app_state["active_filter_label"] = describe_filter(target_attr, target_award)

        # 自動備份
        if tenders_list:
            matched_for_export = qualified if include_misses else keyword_hits
            rel = flask_auto_export(OUTPUT_DIR, tenders_list, matched_for_export, keep=MAX_OUTPUT_KEEP)
            if rel and not rel.startswith("失敗"):
                qlog(f"💾 已自動備份 Excel 至 {rel}")
            elif rel:
                qlog(f"  ⚠️ 自動備份失敗: {rel}")

        by_pk = {t["pk"]: t for t in tenders_list if t.get("pk")}
        with job_lock:
            job["result"] = {
                "tenders_all": len(tenders_list),
                "qualified": len(qualified),
                "keyword_hits": len(keyword_hits),
                "by_pk": len(by_pk),
                "summary": f"共 {len(tenders_list)} 筆標案，符合條件 {len(qualified)} 筆，命中關鍵字 {len(keyword_hits)} 筆",
                "was_stopped": was_stopped,
            }
            job["done"] = True
            job["failed"] = None

    except Exception as e:
        import traceback
        qlog(f"❌ 搜尋過程發生未預期錯誤: {e.__class__.__name__}: {e}")
        try:
            tb_text = traceback.format_exc().rstrip()
            err_path = os.path.join(OUTPUT_DIR, "app_error.log")
            with open(err_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now().isoformat()}] {e.__class__.__name__}: {e}\n{tb_text}\n")
        except Exception:
            pass
        with job_lock:
            job["status"] = ("error", f"❌ 搜尋失敗: {e.__class__.__name__}: {e}")
            job["failed"] = f"{e.__class__.__name__}: {e}"
            job["done"] = True

def run_trickle_job(job_id: str):
    job = job_store.get(job_id)
    if not job:
        return

    def qlog(msg: str):
        append_log(msg)
        job["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    try:
        result = core.trickle_verify(award_cache_path(), pending_queue_path(), batch=core.DEFAULT_TRICKLE_BATCH, log=lambda m: qlog(m), should_stop=job["stop_event"].is_set, use_mirror=USE_MIRROR_SOURCE)
        qlog(f"🔄 補齊批次：picked {result.get('picked',0)}、ok {result.get('ok',0)}、remaining {result.get('remaining',0)}")
        # 若成功，回填全域 tenders
        if result.get("ok"):
            try:
                cache = core.load_award_cache(award_cache_path())
                with state_lock:
                    if app_state["tenders_all"]:
                        applied = core.apply_award_cache(app_state["tenders_all"], cache)
                        if applied:
                            refresh_datasets()
            except Exception:
                pass
        with job_lock:
            job["result"] = result
            job["done"] = True
    except Exception as e:
        import traceback
        qlog(f"  ⚠️ 補齊過程錯誤: {e.__class__.__name__}: {e}")
        try:
            tb_text = traceback.format_exc().rstrip()
            err_path = os.path.join(OUTPUT_DIR, "app_error.log")
            with open(err_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now().isoformat()}] trickle {e.__class__.__name__}: {e}\n{tb_text}\n")
        except Exception:
            pass
        with job_lock:
            job["failed"] = str(e)
            job["done"] = True

# ==================== 路由 ====================

@app.route("/")
def index():
    with state_lock:
        total = len(app_state["tenders_all"])
        qual = len(app_state["tenders_qualified"])
        hits = len(app_state["tenders_keyword_hits"])
        watch_count = len(wl_utils.load_watchlist(OUTPUT_DIR))
        notices = list(app_state["notices"])
        logs = app_state["log_lines"][-50:]
    # 讀上次設定
    saved = core.load_json_dict(settings_path())
    kw_default = saved.get("keywords", _default_keywords_str()) if saved else _default_keywords_str()
    date_mode_saved = saved.get("date_mode", DATE_MODE_OPTIONS[0]) if saved else DATE_MODE_OPTIONS[0]
    days_saved = saved.get("days", "7") if saved else "7"
    attr_saved = saved.get("attr", "勞務") if saved else "勞務"
    award_saved = saved.get("award", "最低標") if saved else "最低標"
    return render_template(
        "base.html",
        total=total, qual=qual, hits=hits, watch_count=watch_count,
        notices=notices, logs=logs,
        kw_default=kw_default, date_mode_saved=date_mode_saved, days_saved=days_saved,
        attr_saved=attr_saved, award_saved=award_saved,
        attr_options=ATTR_OPTIONS, award_options=AWARD_OPTIONS,
        date_mode_options=DATE_MODE_OPTIONS, days_options=DEFAULT_DAYS_OPTIONS,
        urgency_options=URGENCY_OPTIONS, award_status_options=AWARD_STATUS_FILTER_OPTIONS,
        sort_options=SORT_OPTIONS, display_columns=DISPLAY_COLUMNS,
    )

@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(force=True)
    raw_kws = (data.get("keywords") or "").strip()
    raw_kws, warn = validate_keywords(raw_kws)
    if not raw_kws:
        return jsonify({"error": "請至少輸入一個關鍵字"}), 400
    keywords = [k.strip() for k in re.split(r"[\s,]+", raw_kws) if k.strip()]
    keywords = [w[:KEYWORDS_MAX_WORD_LEN] for w in keywords]
    days_raw = str(data.get("days", "7")).split()[0]
    days = int(days_raw) if days_raw.isdigit() else 7
    target_attr = data.get("attr", "勞務")
    target_award = data.get("award", "最低標")
    date_mode = data.get("date_mode", DATE_MODE_OPTIONS[0])
    date_type = selected_date_type(date_mode)
    verify = bool(data.get("verify", True))
    include_misses = bool(data.get("include_misses", False))

    # 節流
    with state_lock:
        now = time.time()
        if now - app_state["last_search_ts"] < RATE_LIMIT_SECONDS:
            remain = int(RATE_LIMIT_SECONDS - (now - app_state["last_search_ts"]))
            return jsonify({"error": f"操作過於頻繁，請 {remain} 秒後再試"}), 429
        app_state["last_search_ts"] = now
        app_state["active_attr_target"] = target_attr
        app_state["active_award_target"] = target_award
        app_state["active_filter_label"] = describe_filter(target_attr, target_award)
    # 儲存設定
    try:
        core.save_json_dict({
            "keywords": raw_kws,
            "date_mode": date_mode,
            "days": str(days),
            "attr": target_attr,
            "award": target_award,
            "verify": verify,
            "include_misses": include_misses,
            "hide_pending": bool(data.get("hide_pending", False)),
        }, settings_path())
    except Exception:
        pass
    clear_notices()
    append_log(f"── 開始搜尋（條件：{describe_filter(target_attr, target_award)} / {date_mode}） ──")

    job_id = uuid.uuid4().hex[:8]
    q = queue.Queue()
    stop_event = threading.Event()
    job = {
        "id": job_id,
        "type": "search",
        "queue": q,
        "stop_event": stop_event,
        "progress": 0,
        "status": ("info", "🔍 搜尋中… 正在翻頁擷取標案"),
        "logs": [],
        "done": False,
        "failed": None,
        "result": None,
    }
    with job_lock:
        job_store[job_id] = job
    t = threading.Thread(target=run_search_job, args=(job_id, keywords, days, target_attr, target_award, date_type, verify, include_misses), daemon=True)
    t.start()
    job["thread"] = t
    resp = {"job_id": job_id}
    if warn:
        resp["warning"] = warn
    return jsonify(resp)

@app.route("/api/search/status")
def api_search_status():
    job_id = request.args.get("job_id")
    with job_lock:
        job = job_store.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404
        # 收斂 queue
        logs = []
        while not job["queue"].empty():
            try:
                action, payload = job["queue"].get_nowait()
                if action == "log":
                    logs.append(payload)
                elif action == "notice":
                    logs.append(payload)
            except queue.Empty:
                break
        return jsonify({
            "progress": job.get("progress", 0),
            "status": job.get("status", ("info", "")),
            "done": job.get("done", False),
            "failed": job.get("failed"),
            "result": job.get("result"),
            "logs": logs,
        })

@app.route("/api/search/stop", methods=["POST"])
def api_search_stop():
    data = request.get_json(force=True)
    job_id = data.get("job_id")
    with job_lock:
        job = job_store.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404
        job["stop_event"].set()
    append_log("⏹ 已要求停止，等待當前請求完成後收尾…")
    return jsonify({"ok": True})

@app.route("/api/trickle", methods=["POST"])
def api_trickle():
    job_id = uuid.uuid4().hex[:8]
    stop_event = threading.Event()
    job = {
        "id": job_id,
        "type": "trickle",
        "queue": queue.Queue(),
        "stop_event": stop_event,
        "progress": 0,
        "status": ("info", "補齊中…"),
        "logs": [],
        "done": False,
        "failed": None,
        "result": None,
    }
    with job_lock:
        job_store[job_id] = job
    t = threading.Thread(target=run_trickle_job, args=(job_id,), daemon=True)
    t.start()
    job["thread"] = t
    append_log("🔄 已啟動補齊（背景執行）…")
    return jsonify({"job_id": job_id})

@app.route("/api/trickle/status")
def api_trickle_status():
    job_id = request.args.get("job_id")
    with job_lock:
        job = job_store.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404
        return jsonify({
            "done": job.get("done", False),
            "failed": job.get("failed"),
            "result": job.get("result"),
            "logs": job.get("logs", [])[-20:],
        })

@app.route("/api/tenders")
def api_tenders():
    tab = request.args.get("tab", "matched")
    query = request.args.get("query", "")
    min_b = float(request.args.get("min_budget", 0) or 0)
    max_b = float(request.args.get("max_budget", 0) or 0)
    urgency = request.args.get("urgency", "全部")
    award_status = request.args.get("award_status", "全部")
    agencies = request.args.getlist("agencies")
    sort_opt = request.args.get("sort", SORT_OPTIONS[0])
    page = int(request.args.get("page", 1) or 1)
    limit = request.args.get("limit", "100")
    hide_pending = request.args.get("hide_pending", "false").lower() == "true"

    with state_lock:
        if tab == "all":
            base = list(app_state["tenders_all"])
        elif tab == "watchlist":
            wl = wl_utils.load_watchlist(OUTPUT_DIR)
            try:
                cache = core.load_award_cache(award_cache_path())
            except Exception:
                cache = {}
            base = wl_utils.resolve_watchlist_rows(wl, app_state["tenders_by_pk"], cache)
        else:  # matched
            # 依 include_misses 決定精選來源，但前端已無 session，按 active 標籤回推
            # 簡化：matched 若 hide_pending 由前端傳，否則取 qualified
            # 為保持與舊邏輯一致，前端應在 matched 時帶 include_misses 參數，這裡直接取 qualified
            base = list(app_state["tenders_qualified"])
            # 若前端要求包含未命中，則仍為 qualified；若只看命中，則需再過濾
            include_misses = request.args.get("include_misses", "false").lower() == "true"
            if not include_misses:
                # 只留命中關鍵字者
                base = [t for t in base if t.get("命中關鍵字群") or t.get("命中關鍵字")]

    # 後端多維篩選與排序（複用 ui_logic）
    from ui_logic import build_advanced_display_rows as _filter, sort_tenders as _sort
    filtered = _filter(
        base,
        hide_pending_filter=hide_pending,
        query=query,
        min_budget_wan=min_b,
        max_budget_wan=max_b,
        urgency=urgency,
        award_status=award_status,
        selected_agencies=agencies,
    )
    sorted_rows = _sort(filtered, sort_opt)

    # 分頁
    total = len(sorted_rows)
    if limit == "全部":
        page_rows = sorted_rows
        total_pages = 1
    else:
        try:
            lim = int(limit)
        except:
            lim = 100
        total_pages = max(1, (total + lim - 1) // lim)
        page = max(1, min(page, total_pages))
        start = (page - 1) * lim
        page_rows = sorted_rows[start:start+lim]

    # 為前端表格準備顯示欄位（沿用 tenders_to_dataframe 的前綴邏輯，但回 JSON）
    enriched = []
    for idx, t in enumerate(page_rows, 1 + (page-1)* (lim if limit != "全部" else 0)):
        award_display = t.get("決標方式", "")
        if is_award_pending(t):
            award_display = f"🟠 {award_display}"
        elif core.is_award_confirmed(t):
            # 若已確認但不符目前決標篩選，加 ⚪
            with state_lock:
                cur_award = app_state["active_award_target"]
            matched = core.filter_tenders([t], "不限", cur_award)
            if not matched:
                award_display = f"⚪ {award_display}"
        # 剩餘天數
        from ui_logic import format_remaining_days
        remaining = format_remaining_days(t.get("截止投標", ""))
        budget_val = core.parse_amount(t.get("預算金額", ""))
        budget_num = int(budget_val) if budget_val != -1.0 else 0
        enriched.append({
            "#": idx,
            "公告日期": t.get("公告日期", ""),
            "招標機關": t.get("招標機關", ""),
            "標案名稱": t.get("標案名稱", ""),
            "預算金額": t.get("預算金額", ""),
            "預算數值": budget_num,
            "決標方式": award_display,
            "決標方式_raw": t.get("決標方式", ""),
            "招標方式": t.get("招標方式", ""),
            "決標方式來源": t.get("決標方式來源", ""),
            "截止投標": t.get("截止投標", ""),
            "剩餘天數": remaining,
            "命中關鍵字": t.get("命中關鍵字", "") or "—",
            "詳細連結": t.get("詳細連結", ""),
            "標案案號": t.get("標案案號", ""),
            "pk": t.get("pk", ""),
        })

    return jsonify({
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "rows": enriched,
        "all_count": len(app_state["tenders_all"]) if tab != "all" else total,
    })

@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    wl = wl_utils.load_watchlist(OUTPUT_DIR)
    return jsonify(wl)

@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    data = request.get_json(force=True)
    pks = data.get("pks", [])
    # pks 可為案號或 pk，需對照 app_state
    with state_lock:
        by_pk = dict(app_state["tenders_by_pk"])
        all_rows = list(app_state["tenders_all"])
    # 建立查詢索引
    lookup = {t.get("pk"): t for t in all_rows if t.get("pk")}
    lookup.update({t.get("標案案號"): t for t in all_rows if t.get("標案案號")})
    wl = wl_utils.load_watchlist(OUTPUT_DIR)
    added = 0
    for key in pks:
        t = lookup.get(key)
        if not t:
            continue
        pk = t.get("pk") or t.get("標案案號")
        if not pk or pk in wl:
            continue
        wl[pk] = {"added_at": datetime.now().isoformat(), "snapshot": t}
        added += 1
    if added:
        wl_utils.save_watchlist(OUTPUT_DIR, wl)
    return jsonify({"added": added, "total": len(wl)})

@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    data = request.get_json(force=True)
    keys = data.get("keys", [])
    wl = wl_utils.load_watchlist(OUTPUT_DIR)
    for k in keys:
        wl.pop(k, None)
    wl_utils.save_watchlist(OUTPUT_DIR, wl)
    return jsonify({"total": len(wl)})

@app.route("/api/watchlist/clear", methods=["POST"])
def api_watchlist_clear():
    wl_utils.save_watchlist(OUTPUT_DIR, {})
    return jsonify({"total": 0})

@app.route("/api/dashboard")
def api_dashboard():
    with state_lock:
        tenders = list(app_state["tenders_all"])
        qualified = list(app_state["tenders_qualified"])
        hits = list(app_state["tenders_keyword_hits"])
    if not tenders:
        return jsonify({"empty": True})
    kpi = kpi_summary(tenders)
    tier_df = budget_tier_frame(tenders)
    award_df = award_composition_frame(tenders)
    agency_df = top_agencies_frame(tenders, top_n=10)
    kw_df = keyword_ranking_frame(tenders, top_n=12)
    urgency_df = urgency_bins_frame(tenders)
    # pandas -> json
    def df_to_records(df):
        return df.to_dict(orient="records")
    return jsonify({
        "kpi": kpi,
        "budget_tier": df_to_records(tier_df),
        "award_composition": df_to_records(award_df),
        "top_agencies": df_to_records(agency_df),
        "keyword_ranking": df_to_records(kw_df),
        "urgency_bins": df_to_records(urgency_df),
        "counts": {"total": len(tenders), "qualified": len(qualified), "hits": len(hits)},
    })

@app.route("/api/export")
def api_export():
    fmt = request.args.get("format", "xlsx")
    tab = request.args.get("tab", "matched")
    with state_lock:
        all_t = list(app_state["tenders_all"])
        qual = list(app_state["tenders_qualified"])
        hits = list(app_state["tenders_keyword_hits"])
    if tab == "all":
        rows = all_t
    elif tab == "watchlist":
        wl = wl_utils.load_watchlist(OUTPUT_DIR)
        try:
            cache = core.load_award_cache(award_cache_path())
        except Exception:
            cache = {}
        with state_lock:
            rows = wl_utils.resolve_watchlist_rows(wl, app_state["tenders_by_pk"], cache)
    else:
        # matched 預設為 hits，若前端要求 include_misses
        include_misses = request.args.get("include_misses", "false").lower() == "true"
        rows = qual if include_misses else hits

    if fmt == "csv":
        data = build_csv_bytes(rows)
        from io import BytesIO
        return send_file(BytesIO(data), mimetype="text/csv", as_attachment=True, download_name=f"pcc_tenders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    else:
        from app_export import build_excel_bytes
        data = build_excel_bytes(all_t, rows)
        from io import BytesIO
        return send_file(BytesIO(data), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=f"pcc_tenders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")

@app.route("/api/logs")
def api_logs():
    with state_lock:
        logs = list(app_state["log_lines"])[-300:]
        notices = list(app_state["notices"])
    return jsonify({"logs": logs, "notices": notices})

@app.route("/api/stats")
def api_stats():
    with state_lock:
        return jsonify({
            "total": len(app_state["tenders_all"]),
            "qualified": len(app_state["tenders_qualified"]),
            "hits": len(app_state["tenders_keyword_hits"]),
            "watch": len(wl_utils.load_watchlist(OUTPUT_DIR)),
            "pending": sum(1 for t in app_state["tenders_qualified"] if is_award_pending(t)),
        })

if __name__ == "__main__":
    # 初始日誌
    if not app_state["log_lines"]:
        append_log("✅ Flask 輕量版已啟動，前往 http://localhost:8502")
        append_log("ℹ️ 搜尋會掃描該條件下的全部標案，關鍵字僅用於標記與快速篩選。")
    # exe 雙擊時自動開瀏覽器（僅本機）
    if getattr(sys, "frozen", False):
        try:
            threading.Timer(1.2, lambda: webbrowser.open("http://localhost:8502")).start()
        except Exception:
            pass
    app.run(host="0.0.0.0", port=8502, debug=False, threaded=True)
