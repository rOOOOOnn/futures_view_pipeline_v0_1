from __future__ import annotations
import json, re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

DATE_PATTERNS = [
    re.compile(r"(20\d{2})[-/\.](\d{1,2})[-/\.](\d{1,2})"),
    re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日"),
]

def shanghai_today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

def clean_text(text: str) -> str:
    text = (text or "").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def normalize_date_in_text(text: str):
    for p in DATE_PATTERNS:
        m = p.search(text or "")
        if m:
            y, mo, d = map(int, m.groups())
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None

def date_variants(date_str: str) -> list[str]:
    y, m, d = date_str.split("-")
    return [date_str, f"{y}/{m}/{d}", f"{y}.{m}.{d}", f"{y}年{int(m)}月{int(d)}日", f"{int(m)}月{int(d)}日", f"{y}{m}{d}"]

def contains_target_date(text: str, date_str: str) -> bool:
    return any(x in (text or "") for x in date_variants(date_str))

def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def load_yaml(path: str | Path):
    import yaml
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
