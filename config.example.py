# -*- coding: utf-8 -*-
"""明辨 · 配置模板 —— 复制成 config.py 再改,真正的 config.py 不进仓库。"""

# ============ 必须改:填你自己的密钥 ============
API_KEY = "你的 API Key"
API_BASE = "https://api.deepseek.com"
MODEL = "deepseek-flash"        # 想更聪明换成 deepseek-v4-pro(贵些)

# ============ 服务 ============
PORT = 5195                     # 网页地址:http://127.0.0.1:5195

# ============ 群规(防烧钱的两个闸门)============
MAX_ROUNDS = 4                  # 最多聊几轮就强制散会
MAX_WORDS = 80                  # 告诉它每条发言别超过多少字(软要求)

# ============ 群里的人 ============
# name=名字  emoji/color=头像  persona=人设(决定它怎么说话)
AGENTS = [
    {
        "name": "Dev",
        "emoji": "🛠",
        "color": "#6ea8fe",
        "persona": "你是团队的开发主力。说话直接、爱拿数据说话,喜欢用"
                   "「谁的代码跑得绿 / 谁的 diff 小」这种硬指标分胜负。",
    },
    {
        "name": "Mochi",
        "emoji": "🍡",
        "color": "#f78fb3",
        "persona": "你是团队里最会挑刺的那个。专找方案里的漏洞,但从不只说不行 "
                   "—— 每次挑完必须给一个替代方案。",
    },
    {
        "name": "阿策",
        "emoji": "📐",
        "color": "#7ee0b0",
        "persona": "你是负责拍板的那个。别人吵完,你把结论收成一句话,"
                   "谁做什么写清楚。到你发言时基本就该散会了。",
    },
]
