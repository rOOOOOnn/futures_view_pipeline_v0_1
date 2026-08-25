from __future__ import annotations

COMPANIES = ["广发期货", "中信期货", "国泰海通期货", "东证期货"]
COMPANY_KEYS = {
    "gfqh": "广发期货",
    "citicsf": "中信期货",
    "gtjaqh": "国泰海通期货",
    "dzqh": "东证期货",
}
KEY_BY_COMPANY = {value: key for key, value in COMPANY_KEYS.items()}

SECTORS = ["黑色系", "贵金属", "农产品", "有色金属", "能化板块", "碳酸锂", "多晶硅"]
VIEWS = ["强烈看多", "看多", "中性", "看空", "强烈看空"]
VIEW_SCORE = {
    "强烈看多": 2.0,
    "看多": 1.0,
    "中性": 0.0,
    "看空": -1.0,
    "强烈看空": -2.0,
}
CONFIDENCE_LEVELS = {"高", "中", "低"}

STRONG_BULL_THRESHOLD = 1.25
BULL_THRESHOLD = 0.50
BEAR_THRESHOLD = -0.50
STRONG_BEAR_THRESHOLD = -1.25
HIGH_CONSISTENCY_MAX_DIVERGENCE = 1.0
MEDIUM_CONSISTENCY_MAX_DIVERGENCE = 2.0
