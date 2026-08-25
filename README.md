# PCC AI & IT Service Tender Scraper (政府採購網 AI/資訊 勞務最低標爬蟲)

[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GUI: ttkbootstrap](https://img.shields.io/badge/GUI-ttkbootstrap-info.svg)](https://ttkbootstrap.readthedocs.io/)

專門針對**台灣政府電子採購網 ([web.pcc.gov.tw](https://web.pcc.gov.tw))** 設計的自動化標案爬蟲與桌面 GUI 應用程式。

支援自動查詢最新發布之標案公告，並精準自動篩選：
- 🎯 **採購性質**：`勞務類`（亦可切換財物／工程／不限）
- 🎯 **決標方式**：`最低標` —— 先依招標方式推估，**再逐案連線官方詳細頁讀取真正的「決標方式」欄位覆蓋**，
  精確區分「最低標」與「參考最有利標／最有利標／評選」
- 🎯 **特定領域關鍵字**：`AI`、`人工智慧`、`機器學習`、`資訊`、`網站`、`軟體`、`資安`、`雲端`、`大數據` 等

---

## 🖥️ 視覺化前端介面 (GUI) 功能

- 🎨 **現代化 Bootstrap 視覺設計**：卡片式佈局、直覺操作面板。
- 🔍 **多關鍵字自由切換**：預設關鍵字取自 `config.py`，可於介面即時修改與重設。
- ⚡ **背景執行緒搜尋**：爬取過程 UI 不卡頓，具備即時進度條與執行紀錄。
- 📄 **自動翻頁**：自動讀取搜尋結果的所有分頁，不會停在第 1 頁。
- 📊 **雙工作表分類檢視**：
  - **🏆 精選**：完全符合採購性質＋決標方式條件之標案。
  - **📋 所有搜尋標案**：所有命中關鍵字之標案完整清單。
- ↕️ **型態感知排序**：點擊欄位標題切換升／降冪；金額依數值排序、日期依時序排序。
- 🔗 **一鍵直達官方頁面**：雙擊表格任意列或點擊「開啟選取標案網頁」。
- 💾 **報表匯出**：一鍵匯出 Excel (`.xlsx`) 或 CSV；每次搜尋完成亦自動備份一份 Excel 至 `output/`。

---

## 📁 專案架構

```text
pcc-ai-tender-scraper/
├── app.py                   # 桌面 GUI 應用程式入口
├── crawler.py               # 命令列 CLI 入口
├── pcc_core.py              # 共用核心：連線、分頁、解析、決標方式判定、報表輸出
├── config.py                # 關鍵字與篩選規則設定檔
├── build_release.py         # 一鍵打包 EXE（秒開綠色版 + 瘦身單檔版）
├── requirements.txt         # 執行期相依套件
├── requirements-dev.txt     # 開發／打包／測試相依套件
├── tests/                   # 離線單元測試（以真實回應存檔為 fixture）
│   ├── conftest.py
│   ├── fixtures/            # 政府採購網回應存檔，測試不需連網
│   └── test_pcc_core.py
└── output/                  # 搜尋結果與報表輸出目錄（已 gitignore）
```

`app.py` 與 `crawler.py` 皆為薄薄的入口層，所有爬取與解析邏輯集中在 `pcc_core.py`，
兩種介面行為必然一致。

---

## 🚀 執行方式

### 方式 A：直接執行 EXE（最簡單）
至本專案的 **[Releases](../../releases)** 頁面下載打包好的執行檔，雙擊即可開啟操作介面，
無需安裝 Python 環境與套件。

> 若 Releases 尚無檔案，請依「[自行打包 EXE](#-自行打包-exe)」自行產生。

---

### 方式 B：透過 Python 啟動 GUI 介面
```bash
git clone https://github.com/<your-account>/pcc-ai-tender-scraper.git
cd pcc-ai-tender-scraper
pip install -r requirements.txt
python app.py
```

---

### 方式 C：透過 CLI 終端機執行（適合自動排程）
```bash
# 預設執行（查詢最近 7 天的勞務最低標）
python crawler.py

# 自訂天數
python crawler.py --days 14

# 自訂關鍵字清單
python crawler.py -k AI 人工智慧 網站 資訊安全

# 改查工程類的最有利標
python crawler.py --attr 工程 --award-way 最有利標/評選
```

輸出檔案會寫入 `output/`，包含雙工作表 Excel 與一份 CSV。

---

## 🧪 執行測試

測試以 `tests/fixtures/` 內的真實回應存檔為輸入，**完全離線、不會對政府採購網發出任何請求**：

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

另有一組會實際建立 Tk 視窗的 GUI 煙霧測試，需要桌面工作階段，預設跳過；
在本機桌面環境下以環境變數開啟：

```bash
PCC_GUI_TESTS=1 python -m pytest tests/ -v      # PowerShell: $env:PCC_GUI_TESTS=1
```

---

## 📦 自行打包 EXE

```bash
pip install -r requirements-dev.txt
python build_release.py
```

會同時產出兩種版本至 `dist/`：
- **秒開綠色版**（`--onedir`）：啟動快，為一整個資料夾。
- **瘦身單檔版**（`--onefile`）：單一 `.exe`，已排除 scipy / torch 等冗餘套件。

---

## ⚖️ 免責聲明與使用規範
- 本專案僅供學術研究、技術評估與公開政府資訊彙整用途。
- 已內建請求間隔與退避重試，並會偵測網站的頻率防護驗證碼後主動停止；
  請勿自行調低間隔或加大執行緒數，避免造成伺服器負載。
- `config.py` 的預設關鍵字已刻意去除彼此涵蓋者（例如有「資訊」就不再放「資訊系統」），
  以減少無謂的重複請求。
