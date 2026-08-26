from __future__ import annotations

import argparse
import json
from pathlib import Path

from .constants import COMPANY_KEYS
from .utils import shanghai_today


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="检查指定日期的采集、LLM 与最终输出状态")
    parser.add_argument("--date", help="YYYY-MM-DD；默认上海时区当天")
    args = parser.parse_args()
    target_date = args.date or shanghai_today()
    stamp = target_date.replace("-", "")
    raw_dir = Path("data/raw") / stamp
    normalized_path = Path("data/normalized") / stamp / "documents.json"
    prompt_dir = Path("data/llm_input") / stamp / "prompts"
    llm_dir = Path("data/llm_output") / stamp
    output_dir = Path("output") / stamp

    print(f"=== {target_date} 运行检查 ===")
    print("\n[1/4] 官网采集")
    for key in ("gfqh", "citicsf", "gtjaqh"):
        data = load_json(raw_dir / f"{key}.json")
        items = data.get("items", [])
        valid = [item for item in items if item.get("source_title")]
        wrong_date = sum(item.get("source_date") != target_date for item in valid)
        missing_url = sum(not str(item.get("source_url", "")).startswith("http") for item in valid)
        empty_content = sum(not str(item.get("content", "")).strip() for item in valid)
        crawl = data.get("crawl", {})
        print(
            f"- {COMPANY_KEYS[key]}: status={crawl.get('status', 'missing')}, "
            f"有效条数={len(valid)}, 日期异常={wrong_date}, URL异常={missing_url}, 空正文={empty_content}"
        )

    normalized = load_json(normalized_path)
    print(f"\n[2/4] 标准化与 Prompt\n- documents.json: {len(normalized.get('documents', []))} 篇")
    for key, company in COMPANY_KEYS.items():
        prompt = prompt_dir / f"{key}.txt"
        size = len(prompt.read_text(encoding="utf-8")) if prompt.exists() else 0
        print(f"- {company}: Prompt={size} 字{'（不存在）' if not prompt.exists() else ''}")

    print("\n[3/4] 元宝处理")
    all_llm_ok = True
    for key, company in COMPANY_KEYS.items():
        data = load_json(llm_dir / f"{key}.json")
        status = data.get("processing_status", "missing")
        all_llm_ok = all_llm_ok and status == "ok"
        line = f"- {company}: status={status}, records={len(data.get('records', []))}"
        if data.get("error"):
            line += f", error={data['error']}"
        print(line)

    excel = output_dir / f"期货公司观点汇总_{stamp}.xlsx"
    print("\n[4/4] 最终结果")
    print(f"- Excel: {'存在' if excel.exists() else '不存在'} -> {excel}")
    print(f"- 完整性结论: {'四家公司均正常' if all_llm_ok and excel.exists() else '存在异常，请先处理上面的非 ok 状态'}")


if __name__ == "__main__":
    main()
