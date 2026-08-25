from __future__ import annotations

import argparse
import json
from pathlib import Path

from .constants import COMPANY_KEYS
from .utils import shanghai_today


def main() -> None:
    parser = argparse.ArgumentParser(description="按公司生成四份 LLM Prompt")
    parser.add_argument("--date")
    parser.add_argument("--prompt", default="prompts/classifier_prompt.txt")
    parser.add_argument("--dzqh-prompt", default="prompts/dzqh_search_prompt.txt")
    parser.add_argument("--company", choices=["all", *COMPANY_KEYS], default="all")
    args = parser.parse_args()

    target_date = args.date or shanghai_today()
    stamp = target_date.replace("-", "")
    normalized_path = Path(f"data/normalized/{stamp}/documents.json")
    normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
    documents = normalized.get("documents", [])
    crawl_status = normalized.get("crawl_status", {})
    common_rules = Path(args.prompt).read_text(encoding="utf-8").strip()
    dzqh_rules = Path(args.dzqh_prompt).read_text(encoding="utf-8").strip()

    selected = list(COMPANY_KEYS) if args.company == "all" else [args.company]
    out_dir = Path(f"data/llm_input/{stamp}/prompts")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []

    for key in selected:
        company = COMPANY_KEYS[key]
        if key == "dzqh":
            body = dzqh_rules.replace("{{TARGET_DATE}}", target_date)
            source_count = 0
        else:
            company_docs = [d for d in documents if d.get("company_hint") == company]
            payload = {"target_date": target_date, "company": company, "source_documents": company_docs}
            body = common_rules + "\n\nINPUT:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
            source_count = len(company_docs)
        prompt_path = out_dir / f"{key}.txt"
        prompt_path.write_text(body, encoding="utf-8")
        manifest.append({"key": key, "company": company, "prompt_file": str(prompt_path), "source_count": source_count,
                         "crawl": crawl_status.get(key, {"status": "llm_public_search" if key == "dzqh" else "unknown"})})

    (out_dir / "manifest.json").write_text(
        json.dumps({"target_date": target_date, "prompts": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(out_dir)


if __name__ == "__main__":
    main()
