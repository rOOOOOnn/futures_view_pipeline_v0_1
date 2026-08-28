from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .constants import COMPANY_KEYS
from .finalize import validate_company_file
from .llm.browser_llm import parse_json_response
from .utils import save_json


def main() -> None:
    parser = argparse.ArgumentParser(description="导入手工复制的 LLM JSON 回复")
    parser.add_argument("--date", required=True)
    parser.add_argument("--company", choices=COMPANY_KEYS, required=True)
    parser.add_argument("--input", required=True)
    args = parser.parse_args()

    stamp = args.date.replace("-", "")
    company = COMPANY_KEYS[args.company]
    input_path = Path(args.input)
    raw = input_path.read_text(encoding="utf-8-sig")
    result = parse_json_response(raw, args.date, company)
    if result.get("target_date") != args.date or result.get("company") != company:
        raise SystemExit("回复的 target_date/company 与命令参数不一致，已停止导入")

    out_dir = Path("data/llm_output") / stamp
    parts_dir = out_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = Path("data/llm_input") / stamp / "prompts" / f"{args.company}.txt"
    prompt_hash = hashlib.sha256(prompt_path.read_bytes()).hexdigest() if prompt_path.exists() else ""
    crawl = {}
    raw_path = Path("data/raw") / stamp / f"{args.company}.json"
    if raw_path.exists():
        raw_source = json.loads(raw_path.read_text(encoding="utf-8-sig"))
        crawl_meta = raw_source.get("crawl", {})
        crawl = {
            "status": crawl_meta.get("status", "unknown"),
            "count": crawl_meta.get("count", len(raw_source.get("items", []))),
        }

    part_result = {
        **result,
        "processing_status": "ok_manual_import",
        "crawl": crawl,
        "part_index": 1,
        "part_count": 1,
        "prompt_file": str(prompt_path),
        "prompt_sha256": prompt_hash,
        "manual_import_file": str(input_path),
        "raw_response": raw,
    }
    part_path = parts_dir / f"{args.company}_part01.json"
    save_json(part_path, part_result)
    company_result = {
        "target_date": args.date,
        "company": company,
        "records": result["records"],
        "processing_status": "ok_manual_import",
        "crawl": crawl,
        "parts": [{
            "part_index": 1,
            "status": "ok_manual_import",
            "records": len(result["records"]),
            "result_file": str(part_path),
        }],
        "errors": [],
    }
    company_path = out_dir / f"{args.company}.json"
    save_json(company_path, company_result)
    validated = validate_company_file(company_path, args.company, args.date)
    if validated["rejected"]:
        raise SystemExit(
            f"已解析 {len(result['records'])} 条，但有 {len(validated['rejected'])} 条未通过字段校验："
            + "；".join(validated["rejected"][:5])
        )
    (parts_dir / f"error_{args.company}_part01.json").unlink(missing_ok=True)
    (out_dir / f"error_{args.company}_part01.png").unlink(missing_ok=True)
    print(f"导入成功：{company_path} | {len(validated['records'])} 条，0 条拒绝")


if __name__ == "__main__":
    main()
