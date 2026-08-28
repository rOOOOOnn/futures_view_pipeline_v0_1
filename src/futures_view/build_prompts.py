from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .constants import COMPANY_KEYS
from .commodities import canonical_commodity, sector_for_commodity
from .utils import shanghai_today


_CITIC_LABEL_ALIASES = {"蛋白粕": "粕类", "双粕": "粕类"}
_DIRECTION_TERMS = re.compile(
    r"偏强|偏弱|走强|走弱|上涨|下跌|上行|下行|反弹|回落|承压|支撑|"
    r"做多|做空|低多|逢高空|震荡|区间|观望|整理|强势|弱势"
)


def extract_citicsf_coverage_candidates(documents: list[dict]) -> list[dict]:
    """Find explicit ``品种：观点`` lines that the first LLM pass must cover."""
    candidates: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for doc in documents:
        if doc.get("company_hint") != "中信期货":
            continue
        for raw_line in str(doc.get("source_text") or "").splitlines():
            line = raw_line.strip()
            match = re.match(r"^([^：:]{1,16})[：:]\s*(.{2,})$", line)
            if not match or not _DIRECTION_TERMS.search(match.group(2)):
                continue
            label, statement = match.group(1).strip(), match.group(2).strip()
            commodity = canonical_commodity(_CITIC_LABEL_ALIASES.get(label, label))
            sector = sector_for_commodity(commodity)
            if not sector:
                continue
            key = (commodity, statement, str(doc.get("source_title") or ""))
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "commodity_hint": commodity,
                "sector_hint": sector,
                "statement": f"{label}：{statement}",
                "source_date": doc.get("source_date"),
                "source_type": doc.get("source_type"),
                "source_name": doc.get("source_name"),
                "source_title": doc.get("source_title"),
                "source_url": doc.get("source_url"),
            })
    return candidates


def compact_gtja_documents(documents: list[dict]) -> tuple[dict, list[dict]]:
    """Title-only reports share metadata; emit it once instead of 55 times."""
    defaults = {
        "source_type": "官方网站标题",
        "source_name": "国泰君安期货研究资讯",
        "source_content_kind": "title_only",
        "confidence": "低",
        "notes": "仅标题同时明确商品与未来方向时输出；模糊标题不输出。",
    }
    compact = [
        {
            "source_date": doc.get("source_date"),
            "source_title": doc.get("source_title"),
            "source_url": doc.get("source_url"),
        }
        for doc in documents
    ]
    return defaults, compact


def main() -> None:
    parser = argparse.ArgumentParser(description="按公司生成四份 LLM Prompt")
    parser.add_argument("--date")
    parser.add_argument("--prompt", default="prompts/classifier_prompt.txt")
    parser.add_argument("--dzqh-prompt", default="prompts/dzqh_search_prompt.txt")
    parser.add_argument("--company", choices=["all", *COMPANY_KEYS], default="all")
    parser.add_argument("--config", default="config.yaml")
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
            parts = [(body, 0)]
        else:
            company_docs = [
                d for d in documents
                if d.get("company_hint") == company and d.get("source_date") == target_date
            ]
            doc_groups = [company_docs]
            parts = []
            for group in doc_groups:
                payload = {"target_date": target_date, "company": company}
                if key == "gtjaqh":
                    defaults, compact_docs = compact_gtja_documents(group)
                    payload["source_defaults"] = defaults
                    payload["source_documents"] = compact_docs
                else:
                    payload["source_documents"] = group
                    if key == "citicsf":
                        payload["coverage_candidates"] = extract_citicsf_coverage_candidates(group)
                body = common_rules + "\n\nINPUT:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
                parts.append((body, len(group)))
        part_count = len(parts)
        for part_index, (body, source_count) in enumerate(parts, start=1):
            suffix = f"_part{part_index:02d}" if part_count > 1 else ""
            prompt_path = out_dir / f"{key}{suffix}.txt"
            prompt_path.write_text(body, encoding="utf-8")
            manifest.append({
                "key": key,
                "company": company,
                "prompt_file": str(prompt_path),
                "source_count": source_count,
                "part_index": part_index,
                "part_count": part_count,
                "crawl": crawl_status.get(
                    key, {"status": "llm_public_search" if key == "dzqh" else "unknown"}),
            })

    (out_dir / "manifest.json").write_text(
        json.dumps({"target_date": target_date, "prompts": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(out_dir)


if __name__ == "__main__":
    main()
