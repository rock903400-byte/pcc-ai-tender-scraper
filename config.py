# -*- coding: utf-8 -*-
"""
政府電子採購網 (PCC) - AI 與資訊勞務最低標標案爬蟲設定檔
"""

import os

# ==================== 關鍵字與篩選條件設定 ====================

# 1. 搜尋關鍵字清單 (自動逐一搜尋並去重)
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
    "資料探勘",
    "智慧化",
    "資訊系統",
    "資訊軟體",
    "軟體開發",
    "雲端服務",
    "數位轉型",
]

# 2. 標案性質過濾條件 (預設: 勞務)
# 可選: "勞務", "財物", "工程", 或 None (不限制)
TARGET_ATTR = "勞務"

# 3. 決標方式過濾條件 (預設: 最低標)
# 支援比對關鍵字: "最低標", "最有利標", "參考最有利標" 等
TARGET_AWARD_WAY = "最低標"

# 4. 預設查詢時間範圍 (天數)
DEFAULT_DAYS = 7

# 5. 輸出設定
OUTPUT_DIR = "output"
EXPORT_EXCEL = True
EXPORT_CSV = True
EXPORT_JSON = False

# 6. Webhook 通知設定 (可填入 LINE Notify Token 或 Discord Webhook URL)
NOTIFY_CONFIG = {
    "enabled": False,
    "line_notify_token": os.getenv("LINE_NOTIFY_TOKEN", ""),
    "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
}
