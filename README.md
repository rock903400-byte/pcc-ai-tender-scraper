# PCC AI & IT Service Tender Scraper (政府採購網 AI/資訊 勞務最低標爬蟲)

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GUI: ttkbootstrap](https://img.shields.io/badge/GUI-ttkbootstrap-info.svg)](https://ttkbootstrap.readthedocs.io/)

專門針對**台灣政府電子採購網 ([web.pcc.gov.tw](https://web.pcc.gov.tw))** 設計的自動化標案爬蟲與桌面 GUI 應用程式。

支援自動查詢最新發布之標案公告，並精準自動篩選：
- 🎯 **採購性質**：`勞務類`
- 🎯 **決標方式**：`最低標`（包含公開招標最低標、公開取得報價單等）
- 🎯 **特定領域關鍵字**：`AI`、`人工智慧`、`機器學習`、`資訊系統`、`網站`、`軟體開發`、`資安`、`雲端`、`大數據` 等相關領域

---

## 🖥️ 視覺化前端介面 (GUI) 功能

- 🎨 **現代化 Bootstrap 視覺設計**：精緻卡片式佈局、直覺操作面板。
- 🔍 **多關鍵字自由切換**：預設涵蓋 AI、網站、軟體、資安等 18 組核心關鍵字，支援即時修改與重設。
- ⚡ **多執行緒背景搜尋**：爬取過程 UI 流暢不卡頓，具備即時進度條與日誌輸出。
- 📊 **雙工作表分類檢視**：
  - **🏆 精選：勞務最低標**（完全符合條件之精準標案）。
  - **📋 所有搜尋標案**（所有命中關鍵字之標案完整清單）。
- 🔗 **一鍵直達官方頁面**：雙擊表格任意列或點擊「開啟選取標案網頁」按鈕，立即在瀏覽器中開啟政府採購網詳細資訊。
- 💾 **多格式報表匯出**：支援直接在介面中一鍵匯出為格式化 Excel (`.xlsx`) 與 CSV。

---

## 📁 專案架構

```text
pcc-ai-tender-scraper/
├── dist/
│   └── 政府採購網標案爬蟲.exe # 獨立可執行檔 (雙擊即可直接運行，免裝 Python)
├── app.py                   # 桌面 GUI 應用程式入口
├── crawler.py               # 命令列 CLI 爬蟲核心
├── config.py                # 關鍵字與篩選規則設定檔
├── requirements.txt         # Python 相依套件清單
├── README.md                # 專案說明文件
├── LICENSE                  # MIT 授權條款
└── output/                  # 搜尋結果與報表輸出目錄
```

---

## 🚀 執行方式

### 方式 A：直接執行 EXE（推薦，最簡單）
直接進入 `dist/` 目錄，雙擊執行：
👉 **[`dist/政府採購網標案爬蟲.exe`](dist/)**

無需安裝任何 Python 環境與套件，即可直接開啟前端操作介面！

---

### 方式 B：透過 Python 啟動 GUI 介面
```bash
cd C:\Users\user\Desktop\github\pcc-ai-tender-scraper
pip install -r requirements.txt
python app.py
```

---

### 方式 C：透過 CLI 終端機背景執行（適合自動排程）
```bash
# 預設執行（查詢最近 7 天）
python crawler.py

# 自訂天數
python crawler.py --days 14

# 自訂關鍵字清單
python crawler.py -k AI 人工智慧 網站建置 資訊安全
```

---

## 📦 自行重新打包 EXE

若日後有修改程式碼，可透過以下指令重新打包：

```bash
pyinstaller --noconsole --onefile --name "政府採購網標案爬蟲" --collect-all ttkbootstrap app.py
```
打包完成後檔案將自動輸出於 `dist/` 資料夾內。

---

## ⚖️ 免責聲明與使用規範
- 本專案僅供學術研究、技術評估與公開政府資訊彙整用途。
- 請遵守政府電子採購網之使用規範與 `robots.txt` 禮節，避免高頻頻繁請求造成伺服器負載。
