# -*- coding: utf-8 -*-
"""
建置與效能優化發布腳本 (build_release.py)
一鍵生成：
1. 秒開綠色版 (Onedir) - < 1 秒極速啟動
2. 極速瘦身單檔版 (Onefile) - 精準排除冗餘大型套件 (scipy, pyarrow, numba 等)
"""

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

def build_onedir():
    print("\n" + "=" * 60)
    print("[1/2] 正在建置：【秒開綠色版 (--onedir)】...")
    print("=" * 60)
    name = "政府採購網標案爬蟲_秒開版"
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
        "--collect-all", "ttkbootstrap",
    ] + get_exclude_flags() + ["app.py"]
    
    start_t = time.time()
    res = subprocess.run(cmd, cwd=PROJECT_DIR)
    dur = time.time() - start_t
    
    exe_path = os.path.join(target_dir, f"{name}.exe")
    if res.returncode == 0 or os.path.exists(exe_path):
        print(f"[SUCCESS] 秒開綠色版建置成功 (耗時 {dur:.1f} 秒)！")
        print(f"  執行路徑: {exe_path}")
    else:
        print("[FAIL] 秒開綠色版建置失敗！")

def build_onefile():
    print("\n" + "=" * 60)
    print("[2/2] 正在建置：【極速瘦身單檔版 (--onefile)】...")
    print("=" * 60)
    name = "政府採購網標案爬蟲"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "-y",
        "--noconsole",
        "--onefile",
        "--name", name,
        "--collect-all", "ttkbootstrap",
    ] + get_exclude_flags() + ["app.py"]
    
    start_t = time.time()
    res = subprocess.run(cmd, cwd=PROJECT_DIR)
    dur = time.time() - start_t
    
    if res.returncode == 0:
        exe_path = os.path.join(DIST_DIR, f"{name}.exe")
        size_mb = os.path.getsize(exe_path) / (1024 * 1024) if os.path.exists(exe_path) else 0
        print(f"[SUCCESS] 極速瘦身單檔版建置成功 (耗時 {dur:.1f} 秒)！")
        print(f"  檔案大小: {size_mb:.1f} MB (原版為 162 MB，大幅瘦身！)")
        print(f"  執行路徑: {exe_path}")
    else:
        print("[FAIL] 極速瘦身單檔版建置失敗！")

def main():
    print("=" * 60)
    print("[*] 開始執行雙模式發布建置...")
    print("=" * 60)
    
    build_onedir()
    build_onefile()
    
    print("\n" + "=" * 60)
    print("[*] 所有建置與優化已完成！")
    print(f"檔案輸出目錄: {DIST_DIR}")
    print("=" * 60)

if __name__ == "__main__":
    main()
