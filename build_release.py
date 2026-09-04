# -*- coding: utf-8 -*-
"""
建置腳本 — Flask 輕量版

取代舊 Streamlit/ttkbootstrap 打包，改為 Flask + PyInstaller。
支援 onedir（秒開綠色版，推薦）與 onefile（單檔）雙模式。

用法：
    python build_release.py          # 雙模式
    python build_release.py --onedir  # 僅綠色版
    python build_release.py --onefile # 僅單檔
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(PROJECT_DIR, "dist")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")

EXCLUDES = [
    "scipy",
    "pyarrow",
    "numba",
    "llvmlite",
    "matplotlib",
    "pytest",
    "IPython",
    "notebook",
    "jupyter",
    "torch",
    "torchvision",
    "tensorflow",
    "sklearn",
    "scikit_learn",
    "sympy",
    "Cython",
]

def get_exclude_flags():
    flags = []
    for mod in EXCLUDES:
        flags.extend(["--exclude-module", mod])
    return flags

def base_flags():
    # Flask 不需要 --collect-all streamlit，改收 flask/jinja2
    return [
        "--collect-all", "flask",
        "--collect-all", "jinja2",
        "--hidden-import", "jinja2.ext",
    ]

def build_onedir():
    print("\n" + "=" * 60)
    print("[1/2] 正在建置：【Flask 綠色版 (--onedir) 秒開】...")
    print("=" * 60)
    name = "政府採購網標案爬蟲_Flask"
    target_dir = os.path.join(DIST_DIR, name)
    if os.path.exists(target_dir):
        try:
            shutil.rmtree(target_dir, ignore_errors=True)
        except Exception:
            pass

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "-y",
        "--noconsole",
        "--onedir",
        "--name", name,
        "--add-data", f"templates{os.pathsep}templates",
        "--add-data", f"static{os.pathsep}static",
    ] + base_flags() + get_exclude_flags() + ["app.py"]

    start_t = time.time()
    res = subprocess.run(cmd, cwd=PROJECT_DIR)
    dur = time.time() - start_t

    exe_path = os.path.join(target_dir, f"{name}.exe")
    if res.returncode == 0 and os.path.exists(exe_path):
        size_mb = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(target_dir) for f in fs) / (1024*1024)
        print(f"[SUCCESS] 綠色版建置成功 (耗時 {dur:.1f}s, 總大小 {size_mb:.1f} MB)")
        print(f"  執行路徑: {exe_path}")
        print(f"  雙擊即可啟動，自動開瀏覽器 http://localhost:8502")
        return True
    else:
        print("[FAIL] 綠色版建置失敗！")
        if res.returncode != 0:
            print(f"  returncode={res.returncode}")
        return False

def build_onefile():
    print("\n" + "=" * 60)
    print("[2/2] 正在建置：【Flask 單檔版 (--onefile)】...")
    print("=" * 60)
    name = "政府採購網標案爬蟲_Flask_單檔"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "-y",
        "--noconsole",
        "--onefile",
        "--name", name,
        "--add-data", f"templates{os.pathsep}templates",
        "--add-data", f"static{os.pathsep}static",
    ] + base_flags() + get_exclude_flags() + ["app.py"]

    start_t = time.time()
    res = subprocess.run(cmd, cwd=PROJECT_DIR)
    dur = time.time() - start_t

    exe_path = os.path.join(DIST_DIR, f"{name}.exe")
    if res.returncode == 0 and os.path.exists(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024*1024)
        print(f"[SUCCESS] 單檔版建置成功 (耗時 {dur:.1f}s, 大小 {size_mb:.1f} MB)")
        print(f"  執行路徑: {exe_path}")
        return True
    else:
        print("[FAIL] 單檔版建置失敗！")
        return False

def main():
    parser = argparse.ArgumentParser(description="Flask 版建置")
    parser.add_argument("--onedir", action="store_true", help="僅建綠色版")
    parser.add_argument("--onefile", action="store_true", help="僅建單檔版")
    args = parser.parse_args()

    print("=" * 60)
    print("[*] 開始執行 Flask 版建置...")
    print("=" * 60)

    do_onedir = args.onedir or not args.onefile
    do_onefile = args.onefile or not args.onedir

    ok1 = build_onedir() if do_onedir else True
    ok2 = build_onefile() if do_onefile else True

    print("\n" + "=" * 60)
    if ok1 and ok2:
        print("[*] 建置完成！")
    else:
        print("[*] 建置結束（部分失敗，請看上方日誌）")
    print(f"檔案輸出目錄: {DIST_DIR}")
    print("=" * 60)

if __name__ == "__main__":
    main()
