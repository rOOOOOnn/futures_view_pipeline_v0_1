from __future__ import annotations

import re


COMMODITY_SECTORS = {
    # 黑色系
    "钢材": "黑色系", "螺纹钢": "黑色系", "热卷": "黑色系", "线材": "黑色系",
    "铁矿石": "黑色系", "焦煤": "黑色系", "焦炭": "黑色系",
    "硅铁": "黑色系", "锰硅": "黑色系", "动力煤": "黑色系", "煤焦": "黑色系",
    # 导师品种对应表将不锈钢、玻璃和纯碱纳入黑色板块。
    "不锈钢": "黑色系", "玻璃": "黑色系", "纯碱": "黑色系",
    # 贵金属
    "贵金属": "贵金属", "黄金": "贵金属", "白银": "贵金属",
    "铂": "贵金属", "钯": "贵金属",
    # 有色金属
    "铜": "有色金属", "氧化铝": "有色金属", "铝": "有色金属",
    "铝合金": "有色金属", "锌": "有色金属", "锡": "有色金属",
    "镍": "有色金属", "铅": "有色金属", "国际铜": "有色金属",
    # 单独板块
    "多晶硅": "多晶硅", "碳酸锂": "碳酸锂",
    # 能源化工
    "原油": "能化板块", "燃料油": "能化板块", "低硫燃料油": "能化板块",
    "PX": "能化板块", "PTA": "能化板块", "短纤": "能化板块",
    "瓶片": "能化板块", "乙二醇": "能化板块", "纯苯": "能化板块",
    "苯乙烯": "能化板块", "LLDPE": "能化板块", "聚乙烯": "能化板块",
    "PP": "能化板块", "聚丙烯": "能化板块", "甲醇": "能化板块",
    "烧碱": "能化板块", "PVC": "能化板块", "尿素": "能化板块", "天然橡胶": "能化板块",
    "橡胶": "能化板块", "合成橡胶": "能化板块", "沥青": "能化板块",
    "LPG": "能化板块", "聚烯烃": "能化板块", "20号胶": "能化板块",
    "纸浆": "能化板块", "纤维板": "能化板块", "胶合板": "能化板块",
    # 农产品
    "粕类": "农产品", "油脂油料": "农产品", "油脂": "农产品",
    "大豆": "农产品", "豆一": "农产品", "豆二": "农产品", "豆粕": "农产品", "菜粕": "农产品", "棕榈油": "农产品",
    "豆油": "农产品", "菜油": "农产品", "生猪": "农产品",
    "玉米": "农产品", "玉米淀粉": "农产品", "白糖": "农产品", "棉花": "农产品", "棉纱": "农产品",
    "鸡蛋": "农产品", "花生": "农产品", "苹果": "农产品", "红枣": "农产品",
    "普麦": "农产品", "菜籽": "农产品", "晚籼": "农产品", "粳稻": "农产品",
    "早籼稻": "农产品", "强麦": "农产品", "粳米": "农产品",
    # 新上市品种不在导师表内，保留项目既有口径。
    "原木": "农产品", "胶版印刷纸": "农产品",
}

# 这些品种在广发日评中存在，但不属于当前固定的七个评分板块。
UNSCORED_COMMODITIES = {"股指", "工业硅", "集运欧线"}

ALIASES = {
    "铁矿": "铁矿石", "铁矿砂": "铁矿石", "铸造铝合金": "铝合金",
    "塑料": "LLDPE", "聚氯乙烯": "PVC", "线性低密度聚乙烯": "LLDPE",
    "橡胶": "天然橡胶", "二乙醇": "乙二醇", "乙二醇2": "乙二醇",
    "贵金属品种": "贵金属", "热轧卷板": "热卷",
    "对二甲苯": "PX", "MEG": "乙二醇", "蛋白粕": "粕类", "双粕": "粕类",
    "化工综合": "综合", "黑色系综合": "综合", "基本金属综合": "综合",
    "新能源金属综合": "综合",
    "有色综合": "综合", "有色金属综合": "综合", "贵金属综合": "综合",
    "农产品综合": "综合", "农业综合": "综合", "能化综合": "综合",
    "能源化工综合": "综合", "油脂综合": "油脂", "油粕": "粕类",
    "黑色系板块": "综合", "有色金属板块": "综合", "农产品板块": "综合",
    "能源化工板块": "综合", "能化板块": "综合",
    "集运指数(欧线)": "集运欧线", "集运指数（欧线）": "集运欧线",
    "沪铜": "铜", "沪铝": "铝", "沪锌": "锌", "沪铅": "铅",
    "沪镍": "镍", "沪锡": "锡", "沪金": "黄金", "沪银": "白银",
    "燃油": "燃料油", "石油沥青": "沥青", "国际铜": "铜",
    "沪胶": "天然橡胶", "郑糖": "白糖", "郑棉": "棉花",
    "郑醇": "甲醇", "郑油": "菜油", "连粕": "豆粕", "连豆": "大豆",
    "双胶纸": "胶版印刷纸", "胶版纸": "胶版印刷纸",
}

# 合约代码用于修复品种单元格的 OCR 错字。顺序必须从长前缀到短前缀。
CONTRACT_COMMODITIES = (
    ("IF", "股指"), ("IH", "股指"), ("IC", "股指"), ("IM", "股指"),
    ("AU", "贵金属"), ("AG", "贵金属"), ("PT", "贵金属"), ("PD", "贵金属"),
    ("RB", "钢材"), ("HC", "钢材"), ("WR", "线材"), ("JM", "焦煤"), ("SF", "硅铁"),
    ("SM", "锰硅"), ("CU", "铜"), ("AO", "氧化铝"), ("AL", "铝"),
    ("AD", "铝合金"), ("ZN", "锌"), ("SN", "锡"), ("NI", "镍"),
    ("SS", "不锈钢"), ("SI", "工业硅"), ("PS", "多晶硅"), ("LC", "碳酸锂"),
    ("BC", "铜"), ("SC", "原油"), ("FU", "燃料油"), ("LU", "低硫燃料油"),
    ("PG", "LPG"), ("BU", "沥青"), ("NR", "20号胶"),
    ("PX", "PX"), ("TA", "PTA"), ("PF", "短纤"),
    ("PR", "瓶片"), ("EG", "乙二醇"), ("BZ", "纯苯"), ("EB", "苯乙烯"),
    ("PP", "PP"), ("MA", "甲醇"), ("SH", "烧碱"), ("SA", "纯碱"),
    ("FG", "玻璃"), ("UR", "尿素"), ("RU", "天然橡胶"), ("BR", "合成橡胶"),
    ("EC", "集运欧线"), ("FB", "纤维板"), ("BB", "胶合板"),
    ("RM", "粕类"), ("LH", "生猪"), ("OI", "油脂"),
    ("OP", "胶版印刷纸"), ("SP", "纸浆"), ("LG", "原木"),
    ("SR", "白糖"), ("CF", "棉花"), ("CY", "棉纱"), ("JD", "鸡蛋"), ("AP", "苹果"),
    ("CJ", "红枣"), ("PM", "普麦"), ("RS", "菜籽"), ("LR", "晚籼"),
    ("JR", "粳稻"), ("RI", "早籼稻"), ("WH", "强麦"), ("RR", "粳米"),
    ("PVC", "PVC"),
    ("I", "铁矿石"), ("J", "焦炭"), ("L", "LLDPE"), ("V", "PVC"),
    ("M", "粕类"), ("A", "豆一"), ("B", "豆二"), ("C", "玉米"),
    ("CS", "玉米淀粉"), ("P", "油脂"), ("Y", "油脂"),
)


def _compact(value: str) -> str:
    return re.sub(r"[\s:：,，、/\\|]+", "", str(value or "")).strip()


def commodity_from_contract(contract: str) -> str | None:
    value = re.sub(r"[^A-Z0-9/]", "", str(contract or "").upper())
    # RapidOCR 常把大写 I 读为数字 1；仅在合约以 1+四位数字开头时修复。
    if re.match(r"^1\d{4}(?:$|/)", value):
        value = "I" + value[1:]
    for prefix, commodity in CONTRACT_COMMODITIES:
        if re.search(rf"(?:^|/){re.escape(prefix)}\d", value):
            return commodity
    return None


def normalize_contract(contract: str, commodity: str = "") -> str:
    """Repair a few unambiguous OCR confusions in Chinese futures contract codes."""
    value = re.sub(r"\s+", "", str(contract or "")).upper()
    value = re.sub(r"(?<=\d)(?=[A-Z]{1,3}\d)", "/", value)
    canonical = canonical_commodity(commodity, value) if commodity else ""
    if canonical == "铁矿石":
        value = re.sub(r"^1(?=\d{4}(?:$|/))", "I", value)
    if canonical == "氧化铝":
        value = re.sub(r"^A0(?=\d{4})", "AO", value)
    if canonical == "油脂":
        value = re.sub(r"(?:^|/)01701(?:$|/)", lambda m: m.group(0).replace("01701", "OI2701"), value)
    # Domestic futures codes have four delivery digits; discard obvious duplicated OCR tails.
    value = re.sub(r"([A-Z]{1,3}\d{4})\d+(?=$|/)", r"\1", value)
    return value


def canonical_commodity(raw: str, contract: str = "") -> str:
    value = _compact(raw)
    if value in ALIASES:
        return ALIASES[value]
    value = re.sub(r"(?:期货|品种|板块)$", "", value)
    if value in COMMODITY_SECTORS or value in UNSCORED_COMMODITIES:
        return value
    if value in ALIASES:
        return ALIASES[value]
    for alias, canonical in sorted(ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias and alias in value:
            return canonical
    for known in sorted((*COMMODITY_SECTORS, *UNSCORED_COMMODITIES), key=len, reverse=True):
        if known in value:
            return known
    by_contract = commodity_from_contract(contract)
    return by_contract or value


def sector_for_commodity(commodity: str) -> str | None:
    return COMMODITY_SECTORS.get(canonical_commodity(commodity))
