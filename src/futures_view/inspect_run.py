from __future__ import annotations

import argparse
import json
from pathlib import Path

from openpyxl import load_workbook

from .commodities import UNSCORED_COMMODITIES, canonical_commodity, sector_for_commodity
from .constants import COMPANY_KEYS, VIEW_SCORE
from .llm.api_llm import (
    citicsf_deterministic_coverage_records,
    gfqh_deterministic_table_records,
    gtja_deterministic_title_records,
)
from .source_validation import is_concrete_http_url
from .title_rules import directional_view_override
from .utils import save_json, shanghai_today


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
    parser.add_argument("--strict", action="store_true", help="发现稳定规则异常时返回非零退出码")
    args = parser.parse_args()
    target_date = args.date or shanghai_today()
    stamp = target_date.replace("-", "")
    raw_dir = Path("data/raw") / stamp
    normalized_path = Path("data/normalized") / stamp / "documents.json"
    prompt_dir = Path("data/llm_input") / stamp / "prompts"
    llm_dir = Path("data/llm_output") / stamp
    output_dir = Path("output") / stamp
    issues: list[str] = []
    warnings: list[str] = []

    print(f"=== {target_date} 运行检查 ===")
    print("\n[1/4] 官网采集")
    for key in COMPANY_KEYS:
        data = load_json(raw_dir / f"{key}.json")
        items = data.get("items", [])
        valid = [item for item in items if item.get("source_title")]
        wrong_date = sum(item.get("source_date") != target_date for item in valid)
        missing_url = sum(not str(item.get("source_url", "")).startswith("http") for item in valid)
        empty_content = sum(not str(item.get("content", "")).strip() for item in valid)
        crawl = data.get("crawl", {})
        if crawl.get("status") not in {"ok", "no_source_reports"}:
            issues.append(f"{COMPANY_KEYS[key]}采集状态异常：{crawl.get('status', 'missing')}")
        if wrong_date or missing_url or empty_content:
            issues.append(
                f"{COMPANY_KEYS[key]}原始数据异常：日期={wrong_date}, URL={missing_url}, 空正文={empty_content}"
            )
        print(
            f"- {COMPANY_KEYS[key]}: status={crawl.get('status', 'missing')}, "
            f"有效条数={len(valid)}, 日期异常={wrong_date}, URL异常={missing_url}, 空正文={empty_content}"
        )

    normalized = load_json(normalized_path)
    documents = normalized.get("documents", [])
    print(f"\n[2/4] 标准化与 Prompt\n- documents.json: {len(normalized.get('documents', []))} 篇")
    manifest = load_json(prompt_dir / "manifest.json")
    for key, company in COMPANY_KEYS.items():
        entries = [entry for entry in manifest.get("prompts", []) if entry.get("key") == key]
        sizes = []
        for entry in entries:
            prompt = Path(entry.get("prompt_file", ""))
            sizes.append(len(prompt.read_text(encoding="utf-8")) if prompt.exists() else 0)
        print(f"- {company}: Prompt任务={len(entries)}, 合计={sum(sizes)} 字, 最大单份={max(sizes, default=0)} 字")

    print("\n[3/4] 模型处理")
    all_llm_ok = True
    llm_data = {}
    for key, company in COMPANY_KEYS.items():
        data = load_json(llm_dir / f"{key}.json")
        llm_data[key] = data
        status = data.get("processing_status", "missing")
        status_ok = str(status).startswith("ok") or status == "no_source_reports"
        all_llm_ok = all_llm_ok and status_ok
        if not status_ok:
            issues.append(f"{company} LLM状态异常：{status}")
        for rejected in data.get("api_rejected", []):
            reason = str(rejected.get("reason") or "")
            commodity = canonical_commodity(str(rejected.get("commodity") or ""))
            if (
                "无法确定板块" in reason
                and commodity not in UNSCORED_COMMODITIES
                and not sector_for_commodity(commodity)
            ):
                issues.append(f"{company}存在未映射品种：{rejected.get('commodity')}")
        parts = data.get("parts", [])
        parts_ok = sum(str(part.get("status", "")).startswith("ok") for part in parts)
        line = f"- {company}: status={status}, records={len(data.get('records', []))}"
        if parts:
            line += f", parts={parts_ok}/{len(parts)}"
        if data.get("error"):
            line += f", error={data['error']}"
        elif data.get("errors"):
            line += ", errors=" + " | ".join(error.get("error", "") for error in data["errors"])
        print(line)

    summary = load_json(output_dir / f"summary_{stamp}.json")
    excel = Path(summary.get("excel_file") or output_dir / f"期货公司观点汇总_{stamp}.xlsx")
    final_current = summary.get("target_date") == target_date and bool(summary.get("generated_at"))
    final_data = {key: load_json(output_dir / f"{key}.json") for key in COMPANY_KEYS}
    final_records = {
        key: data.get("records", []) for key, data in final_data.items()
    }
    all_records = [record for records in final_records.values() for record in records]
    duplicate_count = len(all_records) - len({
        (
            record.get("company"), record.get("sector"),
            canonical_commodity(str(record.get("commodity") or "")),
            record.get("view_date"),
        )
        for record in all_records
    })
    invalid_count = sum(
        record.get("view_date") != target_date
        or record.get("source_date") != target_date
        or record.get("view") not in VIEW_SCORE
        or not is_concrete_http_url(record.get("source_url"))
        for record in all_records
    )
    neutral_with_tilt = [
        record for record in all_records
        if record.get("view") == "中性"
        and directional_view_override(str(record.get("evidence") or ""))
    ]
    if duplicate_count:
        issues.append(f"最终结果存在 {duplicate_count} 条公司/板块/品种/日期重复")
    if invalid_count:
        issues.append(f"最终结果存在 {invalid_count} 条日期、观点枚举或URL异常")
    if neutral_with_tilt:
        labels = ",".join(
            f"{record.get('company')}/{record.get('commodity')}"
            for record in neutral_with_tilt[:10]
        )
        issues.append(f"存在 {len(neutral_with_tilt)} 条带明确多空倾向却标为中性的记录：{labels}")

    deterministic_builders = {
        "gfqh": gfqh_deterministic_table_records,
        "citicsf": citicsf_deterministic_coverage_records,
        "gtjaqh": gtja_deterministic_title_records,
    }
    coverage = {}
    for key, builder in deterministic_builders.items():
        expected = builder(documents, target_date)
        actual_keys = {
            (canonical_commodity(str(record.get("commodity") or "")), record.get("view_date"))
            for record in final_records[key]
        }
        missing = sorted({
            canonical_commodity(str(record.get("commodity") or ""))
            for record in expected
            if (canonical_commodity(str(record.get("commodity") or "")), target_date) not in actual_keys
        })
        coverage[key] = {"explicit_candidates": len(expected), "missing": missing}
        if missing:
            issues.append(f"{COMPANY_KEYS[key]}遗漏明确方向品种：{','.join(missing)}")

    excel_detail_rows = None
    if excel.exists():
        try:
            workbook = load_workbook(excel, read_only=True, data_only=True)
            excel_detail_rows = workbook["当日观点明细"].max_row - 1
            workbook.close()
            if excel_detail_rows != len(all_records):
                issues.append(
                    f"Excel明细={excel_detail_rows}，JSON记录={len(all_records)}，两者不一致"
                )
        except Exception as exc:
            issues.append(f"Excel无法读取：{type(exc).__name__}: {exc}")
    else:
        issues.append("最终Excel不存在")
    print("\n[4/4] 最终结果")
    print(f"- Excel: {'本次已生成' if excel.exists() and final_current else '不存在或为旧版本'} -> {excel}")
    if summary.get("generated_at"):
        print(f"- 生成时间: {summary['generated_at']}")
    if not final_current:
        issues.append("最终汇总不是本目标日期的本次结果")
    print(f"- JSON总记录: {len(all_records)}, 重复={duplicate_count}, 非法={invalid_count}")
    print(f"- Excel明细: {excel_detail_rows if excel_detail_rows is not None else '无法读取'}")
    for key, result in coverage.items():
        print(
            f"- {COMPANY_KEYS[key]}明确规则覆盖: {result['explicit_candidates']}，"
            f"遗漏={','.join(result['missing']) if result['missing'] else '0'}"
        )
    conclusion_ok = all_llm_ok and excel.exists() and final_current and not issues
    print(f"- 完整性结论: {'通过自动质量门禁' if conclusion_ok else '未通过自动质量门禁'}")
    for issue in issues:
        print(f"  [错误] {issue}")
    for warning in warnings:
        print(f"  [提醒] {warning}")

    output_dir.mkdir(parents=True, exist_ok=True)
    save_json(output_dir / f"quality_report_{stamp}.json", {
        "target_date": target_date,
        "passed": conclusion_ok,
        "record_counts": {key: len(records) for key, records in final_records.items()},
        "total_records": len(all_records),
        "duplicate_count": duplicate_count,
        "invalid_count": invalid_count,
        "neutral_with_directional_tilt": len(neutral_with_tilt),
        "excel_detail_rows": excel_detail_rows,
        "coverage": coverage,
        "issues": issues,
        "warnings": warnings,
    })
    if args.strict and not conclusion_ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
