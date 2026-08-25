# PCC AI & IT Service Tender Scraper (政府採購網 AI/資訊 勞務最低標爬蟲)

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

專門針對**台灣政府電子採購網 ([web.pcc.gov.tw](https://web.pcc.gov.tw))** 設計的自動化標案爬蟲工具。

支援自動查詢最新發布之標案公告，並精準自動篩選：
- 🎯 **採購性質**：`勞務類`
- 🎯 **決標方式**：`最低標`（包含訂有底價最低標、未訂底價最低標等）
- 🎯 **特定領域關鍵字**：`AI`、`人工智慧`、`機器學習`、`資訊系統`、`軟體開發`、`大數據`、`演算法` 等相關領域

---

## ✨ 核心特色

1. **自動多關鍵字查詢與去重**：內建常見 AI、資訊、軟體、資料科學等關鍵字，自動發送查詢並依「標案案號」自動去重。
2. **深度規格解析（雙層驗證）**：
   - 第一層：快速檢索招標公告清單。
   - 第二層：自動深入標案詳細資訊頁面，擷取「決標方式」、「採購性質」、「預算金額」、「履約地點」及「聯絡人」等完整欄位。
3. **精緻報表自動輸出**：
   - **Excel 活頁簿 (`.xlsx`)**：包含 `精選_勞務最低標` 與 `所有搜尋標案` 雙工作表，支援直接開啟檢閱。
   - **CSV 格式 (`.csv`)**：採用 `utf-8-sig` 編碼，Excel 開啟絕無亂碼。
4. **禮貌爬蟲機制**：內建自動重試、延遲間隔、Cookie 維持與防反爬蟲機制。
5. **高度自訂彈性**：支援透過命令列參數（CLI）或設定檔（`config.py`）自由調整關鍵字、搜尋天數及篩選條件。

---

## 📁 專案架構

```text
pcc-ai-tender-scraper/
├── crawler.py           # 爬蟲核心程式（支援 CLI 指令與自動篩選匯出）
├── config.py            # 預設關鍵字、篩選規則與輸出設定檔
├── requirements.txt     # Python 相依套件清單
├── .gitignore           # Git 忽略檔案設定
├── LICENSE              # MIT 授權條款
├── README.md            # 專案說明文件
└── output/              # 爬取結果輸出目錄 (自動產生)
    ├── pcc_tenders_YYYYMMDD_HHMMSS.xlsx
    └── pcc_tenders_YYYYMMDD_HHMMSS_勞務最低標.csv
```

---

## 🚀 快速開始

### 1. 安裝環境

確保已安裝 Python 3.8 以上版本，並安裝相依套件：

```bash
pip install -r requirements.txt
```

*(註：若僅使用 Python 標準函式庫亦可獨立執行並輸出 CSV；安裝 `pandas` 與 `openpyxl` 可額外產生格式化 Excel 報表)*

---

### 2. 執行爬蟲

#### 快速執行（依預設設定搜尋最近 7 天的 AI / 資訊 勞務最低標）：
```bash
python crawler.py
```

#### 自訂搜尋天數（例如搜尋最近 14 天）：
```bash
python crawler.py --days 14
```

#### 自訂關鍵字清單：
```bash
python crawler.py -k AI 人工智慧 大型語言模型 雲端平台
```

#### 快速預覽（跳過詳細頁面解析，速度更快）：
```bash
python crawler.py --no-detail
```

---

## 🛠️ CLI 參數說明

| 參數 | 縮寫 | 預設值 | 說明 |
| :--- | :--- | :--- | :--- |
| `--days` | `-d` | `7` | 查詢天數區間（例如 `7` 代表今日與過去 7 天內公告） |
| `--keywords` | `-k` | 預設關鍵字群 | 指定要搜尋的關鍵字（空格分隔多個詞彙） |
| `--attr` | | `勞務` | 採購性質過濾（可設為 `勞務`、`財物`、`工程` 或 `""` 不限） |
| `--award-way`| | `最低標` | 決標方式過濾（例如 `最低標`、`最有利標`） |
| `--no-detail`| | `False` | 僅抓取外層清單，不深入詳細頁（執行速度大幅提升） |

---

## ⚙️ 進階客製化 (`config.py`)

您可以在 [`config.py`](config.py) 中自訂常用搜尋關鍵字：

```python
DEFAULT_KEYWORDS = [
    "AI",
    "人工智慧",
    "機器學習",
    "深度學習",
    "大型語言模型",
    "LLM",
    "生成式AI",
    "演算法",
    "大數據",
    "資訊系統",
    "軟體開發",
]

TARGET_ATTR = "勞務"         # 預設過濾：勞務類
TARGET_AWARD_WAY = "最低標"   # 預設過濾：最低標
DEFAULT_DAYS = 7             # 預設查詢天數
```

---

## ⏰ 自動排程設定

### Windows（工作排程器）
1. 開啟「工作排程器」。
2. 建立基本工作，觸發程序設定為「每天 上午 09:00」。
3. 動作選擇「啟動程式」：
   - 程式/指令碼：`python.exe` 完整路徑
   - 新增引數：`crawler.py --days 1`
   - 開始於：`C:\Users\user\Desktop\github\pcc-ai-tender-scraper`

### Linux / macOS (`crontab`)
```bash
# 每天早上 9 點自動抓取昨日至今日最新標案
0 9 * * * cd /path/to/pcc-ai-tender-scraper && python3 crawler.py --days 1 >> output/cron.log 2>&1
```

---

## ⚖️ 免責聲明與使用規範
- 本專案僅供學術研究、技術評估與合法公開資訊彙整用途。
- 請遵守政府電子採購網之使用規範與 `robots.txt` 禮節，避免高頻頻繁請求造成伺服器負載。
