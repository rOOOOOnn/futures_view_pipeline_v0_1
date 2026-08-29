from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from ..constants import COMPANY_KEYS, CONFIDENCE_LEVELS, SECTORS, VIEWS
from ..build_prompts import extract_citicsf_coverage_candidates
from ..title_rules import (
    directional_view_override,
    infer_title_view,
    infer_title_view_for_commodity,
    relative_trade_view,
    title_only_rejection,
)
from .browser_llm import parse_json_response
from ..utils import load_yaml, save_json, shanghai_today
from ..commodities import (
    COMMODITY_SECTORS,
    UNSCORED_COMMODITIES,
    canonical_commodity,
    sector_for_commodity,
)
from ..source_validation import (
    dzqh_source_attribution_rejection,
    repair_source_urls,
    view_evidence_rejection,
)


REQUIRED_RECORD_FIELDS = {
    "company", "sector", "commodity", "view", "view_date", "source_date",
    "source_type", "source_name", "source_title", "source_url", "evidence", "confidence",
}


def response_schema(target_date: str, company: str) -> dict:
    string = {"type": "string", "minLength": 1}
    record_properties = {
        "company": {"type": "string", "enum": [company]},
        "sector": {"type": "string", "enum": SECTORS},
        "commodity": {
            "type": "string",
            "enum": sorted({*COMMODITY_SECTORS, *UNSCORED_COMMODITIES, "综合"}),
        },
        "view": {"type": "string", "enum": VIEWS},
        "view_date": {"type": "string", "enum": [target_date]},
        "source_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "source_type": string,
        "source_name": string,
        "source_title": string,
        "source_url": {"type": "string", "pattern": r"^https?://[^/\s]+/.+"},
        "evidence": string,
        "confidence": {"type": "string", "enum": sorted(CONFIDENCE_LEVELS)},
    }
    return {
        "type": "object",
        "properties": {
            "target_date": {"type": "string", "enum": [target_date]},
            "company": {"type": "string", "enum": [company]},
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": record_properties,
                    "required": list(record_properties),
                    "additionalProperties": False,
                },
            },
        },
        "required": ["target_date", "company", "records"],
        "additionalProperties": False,
    }


def preserve_existing_success(path: Path, target_date: str) -> bool:
    if not path.exists():
        return False
    try:
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return False
    return (existing.get("target_date") == target_date
            and str(existing.get("processing_status", "")).startswith("ok"))


def filter_title_records(records: list[dict]) -> tuple[list[dict], list[dict]]:
    accepted, rejected = [], []
    for record_index, record in enumerate(records, start=1):
        reason = title_only_rejection(record)
        if reason:
            rejected.append({
                "record_index": record_index,
                "commodity": record.get("commodity"),
                "view": record.get("view"),
                "evidence": record.get("evidence"),
                "reason": reason,
            })
        else:
            accepted.append(record)
    return accepted, rejected


def enforce_commodity_sectors(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Use the deterministic Python taxonomy; never trust an LLM sector guess."""
    accepted, rejected = [], []
    for record_index, record in enumerate(records, start=1):
        item = dict(record)
        commodity = canonical_commodity(str(item.get("commodity", "")))
        expected = sector_for_commodity(commodity)
        if commodity == "综合" and item.get("sector") in SECTORS:
            accepted.append(item)
            continue
        if expected is None:
            rejected.append({
                "record_index": record_index,
                "commodity": item.get("commodity"),
                "reason": "品种不属于固定七个评分板块或无法确定板块",
            })
            continue
        item["commodity"] = commodity
        item["sector"] = expected
        accepted.append(item)
    return accepted, rejected


def _call_model(client, api: dict, target_date: str, company: str,
                prompt: str, extra_body: dict, max_tokens: int | None = None
                ) -> tuple[dict, str, dict]:
    response = client.chat.completions.create(
        model=str(api.get("model", "hy3")),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens or int(api.get("max_output_tokens", 20000)),
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "futures_company_views",
                "strict": True,
                "schema": response_schema(target_date, company),
            },
        },
        extra_body=extra_body,
    )
    raw = response.choices[0].message.content or ""
    result = parse_json_response(raw, target_date, company)
    usage = response.usage.model_dump() if response.usage else {}
    return result, raw, usage


def _call_text_model(client, api: dict, prompt: str, extra_body: dict
                     ) -> tuple[str, dict, dict]:
    """Run discovery without JSON schema so TokenHub can actually use web search."""
    response = client.chat.completions.create(
        model=str(api.get("model", "hy3")),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=int(api.get("max_output_tokens", 20000)),
        extra_body=extra_body,
    )
    raw = response.choices[0].message.content or ""
    usage = response.usage.model_dump() if response.usage else {}
    response_data = response.model_dump()
    message_data = (response_data.get("choices") or [{}])[0].get("message") or {}
    search_meta = {
        "search_results": message_data.get("search_results") or [],
        "search_info": response_data.get("search_info"),
    }
    return raw, usage, search_meta


def _search_results_evidence(search_meta: dict) -> str:
    """Preserve tool-returned URLs/snippets for the separate classification call."""
    results = search_meta.get("search_results") or []
    compact = [
        {
            "url": item.get("url"),
            "title": item.get("name"),
            "snippet": item.get("snippet"),
            "site": item.get("site"),
        }
        for item in results
        if item.get("url") and (item.get("name") or item.get("snippet"))
    ]
    return json.dumps(compact, ensure_ascii=False, indent=2)


def _postprocess_records(records: list[dict], documents: list[dict], company: str
                         ) -> tuple[list[dict], list[dict]]:
    structurally_valid = []
    rejected = []
    for record_index, item in enumerate(records, start=1):
        if not isinstance(item, dict):
            rejected.append({"record_index": record_index, "reason": "记录不是 JSON object"})
            continue
        missing = REQUIRED_RECORD_FIELDS - set(item)
        empty = [field for field in REQUIRED_RECORD_FIELDS if field in item and not item[field]]
        reasons = []
        if missing:
            reasons.append(f"缺少必填字段 {sorted(missing)}")
        if empty:
            reasons.append(f"必填字段为空 {sorted(empty)}")
        if item.get("company") != company:
            reasons.append(f"company 必须为 {company}")
        if item.get("view") not in VIEWS:
            reasons.append("view 不属于固定五档")
        if item.get("sector") not in SECTORS:
            reasons.append("sector 不属于固定七板块")
        if item.get("confidence") not in CONFIDENCE_LEVELS:
            reasons.append("confidence 非法")
        if reasons:
            rejected.append({
                "record_index": record_index,
                "commodity": item.get("commodity"),
                "reason": "；".join(reasons),
            })
        else:
            normalized_item = dict(item)
            if normalized_item.get("view") == "中性":
                directional = directional_view_override(
                    str(normalized_item.get("evidence") or "")
                )
                if directional:
                    normalized_item["view"] = directional
            structurally_valid.append(normalized_item)

    accepted, title_rejected = filter_title_records(structurally_valid)
    rejected.extend(title_rejected)
    accepted, sector_rejected = enforce_commodity_sectors(accepted)
    rejected.extend(sector_rejected)
    evidence_accepted = []
    for record_index, item in enumerate(accepted, start=1):
        reason = view_evidence_rejection(item)
        if reason:
            rejected.append({
                "record_index": record_index,
                "commodity": item.get("commodity"),
                "view": item.get("view"),
                "reason": reason,
            })
        else:
            evidence_accepted.append(item)
    accepted = evidence_accepted
    accepted, url_rejected = repair_source_urls(accepted, documents, company)
    rejected.extend(url_rejected)
    if company == "东证期货":
        source_verified = []
        for record_index, item in enumerate(accepted, start=1):
            reason = dzqh_source_attribution_rejection(item)
            if reason:
                rejected.append({
                    "record_index": record_index,
                    "commodity": item.get("commodity"),
                    "source_url": item.get("source_url"),
                    "reason": reason,
                })
            else:
                source_verified.append(item)
        accepted = source_verified
    return accepted, rejected


def _merge_by_commodity(primary: list[dict], supplement: list[dict]) -> list[dict]:
    merged = list(primary)
    seen = {
        (
            item.get("sector"),
            canonical_commodity(str(item.get("commodity", ""))),
            item.get("view_date"),
        )
        for item in primary
    }
    for item in supplement:
        key = (
            item.get("sector"),
            canonical_commodity(str(item.get("commodity", ""))),
            item.get("view_date"),
        )
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def aggregate_call_usage(calls: list[dict]) -> dict:
    """Return the real total across retries, supplements and web discovery."""
    prompt_tokens = completion_tokens = cached_tokens = web_search_calls = 0
    for call in calls:
        usage = call.get("usage") or {}
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        cached_tokens += int(details.get("cached_tokens") or usage.get("cache_read_tokens") or 0)
        tool_usage = usage.get("tool_usage") or {}
        web_search_calls += int(tool_usage.get("web_search_call") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cached_tokens": cached_tokens,
        "web_search_calls": web_search_calls,
    }


_GTJA_TITLE_LABELS = {
    "热轧卷板": "热卷", "铸造铝合金": "铝合金", "对二甲苯": "PX",
    "低硫燃料油": "低硫燃料油", "集运指数（欧线）": "集运欧线",
    "集运指数(欧线)": "集运欧线", "MEG": "乙二醇",
}
def gfqh_deterministic_table_records(documents: list[dict], target_date: str) -> list[dict]:
    """Recover explicit directions from the already structured Guangfa daily table."""
    records = []
    for doc in documents:
        if doc.get("company_hint") != "广发期货" or doc.get("source_date") != target_date:
            continue
        try:
            payload = json.loads(str(doc.get("source_text") or ""))
        except (json.JSONDecodeError, TypeError):
            continue
        for row in payload.get("rows", []):
            if not row.get("scored"):
                continue
            commodity = canonical_commodity(
                str(row.get("commodity") or ""), str(row.get("contract") or "")
            )
            sector = sector_for_commodity(commodity)
            evidence = "；".join(
                text for text in (
                    str(row.get("comment") or "").strip(),
                    str(row.get("advice") or "").strip(),
                ) if text
            )
            view = infer_title_view(evidence)
            if not sector or not view:
                continue
            records.append({
                "company": "广发期货", "sector": sector,
                "commodity": commodity, "view": view,
                "view_date": target_date, "source_date": target_date,
                "source_type": doc.get("source_type") or "官方网站PNG",
                "source_name": doc.get("source_name") or "广发期货研究资讯",
                "source_title": doc.get("source_title"),
                "source_url": doc.get("source_url"),
                "evidence": evidence, "confidence": "高",
            })
    return records


def citicsf_deterministic_coverage_records(
    documents: list[dict], target_date: str
) -> list[dict]:
    """Recover only direction phrases that Python can prove from CITIC subheadings."""
    records = []
    for item in extract_citicsf_coverage_candidates(documents):
        if item.get("source_date") != target_date:
            continue
        evidence = str(item.get("statement") or "").strip()
        view = infer_title_view(evidence)
        if not view:
            continue
        records.append({
            "company": "中信期货", "sector": item["sector_hint"],
            "commodity": item["commodity_hint"], "view": view,
            "view_date": target_date, "source_date": target_date,
            "source_type": item.get("source_type") or "官方网站",
            "source_name": item.get("source_name") or "中信期货机构服务平台",
            "source_title": item.get("source_title"),
            "source_url": item.get("source_url"),
            "evidence": evidence, "confidence": "高",
        })
    return records


def _gtja_title_segments(title: str) -> list[tuple[str, str]]:
    # Split every short label before a colon, including a non-scored related
    # product (e.g. 丙烯). Filtering happens after splitting; this prevents the
    # related product's direction from leaking into the preceding futures item.
    marker = re.compile(
        r"(?:^|(?<=\s))([A-Za-z][A-Za-z0-9（）()]{0,20}|[\u4e00-\u9fff（）()]{1,14})\s*[：:]"
    )
    matches = list(marker.finditer(title))
    if not matches:
        commodity = canonical_commodity(title)
        return [(commodity, title)] if sector_for_commodity(commodity) else []
    segments = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(title)
        label = match.group(1)
        commodity = canonical_commodity(_GTJA_TITLE_LABELS.get(label, label))
        segments.append((commodity, title[match.start():end].strip()))
    return segments


def gtja_deterministic_title_records(documents: list[dict], target_date: str) -> list[dict]:
    records = []
    for doc in documents:
        if doc.get("company_hint") != "国泰海通期货" or doc.get("source_date") != target_date:
            continue
        title = str(doc.get("source_title") or "")
        segments = _gtja_title_segments(title)
        emitted: set[tuple[str, str]] = set()
        for commodity, evidence in segments:
            sector = sector_for_commodity(commodity)
            view = infer_title_view_for_commodity(evidence, commodity)
            if not sector or not view:
                continue
            emitted.add((commodity, view))
            records.append({
                "company": "国泰海通期货", "sector": sector,
                "commodity": commodity, "view": view,
                "view_date": target_date, "source_date": target_date,
                "source_type": doc.get("source_type") or "官方网站标题",
                "source_name": doc.get("source_name") or "国泰君安期货研究资讯",
                "source_title": title, "source_url": doc.get("source_url"),
                "evidence": evidence, "confidence": "低",
            })
        # A relative-value instruction can sit in another commodity's segment,
        # for example “对二甲苯：… PTA：多PX空PTA”. Recover both named legs.
        for commodity, _ in segments:
            sector = sector_for_commodity(commodity)
            view = relative_trade_view(title, commodity)
            if not sector or not view or (commodity, view) in emitted:
                continue
            emitted.add((commodity, view))
            records.append({
                "company": "国泰海通期货", "sector": sector,
                "commodity": commodity, "view": view,
                "view_date": target_date, "source_date": target_date,
                "source_type": doc.get("source_type") or "官方网站标题",
                "source_name": doc.get("source_name") or "国泰君安期货研究资讯",
                "source_title": title, "source_url": doc.get("source_url"),
                "evidence": title, "confidence": "低",
            })
    return records


def _citic_missing_candidates(documents: list[dict], records: list[dict]) -> list[dict]:
    present = {canonical_commodity(str(item.get("commodity", ""))) for item in records}
    return [
        item for item in extract_citicsf_coverage_candidates(documents)
        if item.get("commodity_hint") not in present
    ]


def _citic_supplement_prompt(target_date: str, candidates: list[dict]) -> str:
    payload = {
        "target_date": target_date,
        "company": "中信期货",
        "coverage_candidates": candidates,
    }
    return (
        "你是中信期货观点覆盖复核器。下面是 Python 从中信官网当天正文逐行提取、"
        "但首轮结果未覆盖的‘品种：观点’候选项。逐项判断未来方向；只输出方向明确的项，"
        "不得搜索、补写或改写来源 URL。五档仅限强烈看多、看多、中性、看空、强烈看空；"
        "普通偏强/偏弱分别是看多/看空；只有不带方向修饰的纯区间或纯震荡才是中性。"
        "有支撑但上方空间或高度受限属于震荡偏多，判为看多；方向修饰和交易建议优先于‘震荡’。"
        "所有来源字段必须原样复制候选项。"
        "这些候选项来自日报的观点汇总栏，‘走弱’和‘跟随大盘下行’属于未来方向并应判为看空；"
        "仅写已经收涨/收跌的历史行情不得输出；同一句同时存在相反期现信号且无法确定期货方向时不得输出。"
        "company=中信期货，view_date 和 source_date 均须为目标日期，sector 使用 sector_hint，"
        "commodity 使用 commodity_hint。只返回规定 JSON object。\n\nINPUT:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _dzqh_retry_prompt(target_date: str, original_prompt: str) -> str:
    return original_prompt + (
        "\n\n首次检索没有得到记录，现在执行一次强制补充检索。必须先精确搜索微信公众号"
        f"‘东证期货微咨询’和可能的名称‘东证期货微资讯’在 {target_date} 当天的文章，"
        "并以文章标题继续搜索同花顺、新浪财经等可靠完整转载。重点尝试‘东证期货 晨报/日报/"
        "商品策略 + 日期’。只有完成这些检索仍没有可核实的当天未来方向，才返回 records=[]。"
    )


def _dzqh_discovery_prompt(target_date: str, focused: bool = False) -> str:
    base = (
        f"请联网检索 {target_date} 当天的东证期货商品期货观点。第一优先搜索微信公众号"
        "‘东证期货微咨询’，并同时尝试名称‘东证期货微资讯’。请打开当天文章，列出真实文章标题、"
        "具体 URL、发布日期和支持未来价格方向的原文。公众号不可访问时，再以文章标题搜索同花顺、"
        "新浪财经等明确署名东证期货的完整转载。不得使用东方证券普通证券研究，不得猜 URL。"
    )
    if focused:
        base += (
            "这是第二次补充检索：请分别组合日期的 YYYY-MM-DD、YYYYMMDD、中文写法与‘东证期货"
            " 晨报 日报 商品策略 豆粕 黑色 有色 能化 贵金属 碳酸锂 多晶硅’搜索，并打开结果正文。"
        )
    return base + "本步只做资料发现，不做 JSON；把查到的来源与观点原文完整列出，查不到也说明检索过的入口。"


def main() -> None:
    parser = argparse.ArgumentParser(description="通过腾讯 TokenHub API 处理四家公司 Prompt")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--date")
    parser.add_argument("--company", choices=["all", *COMPANY_KEYS], default="all")
    parser.add_argument("--dry-run", action="store_true", help="只显示任务和字符数，不调用 API")
    parser.add_argument("--revalidate-existing", action="store_true",
                        help="只对现有API结果重做标题硬校验，不调用API")
    args = parser.parse_args()

    target_date = args.date or shanghai_today()
    stamp = target_date.replace("-", "")
    cfg = load_yaml(args.config)
    api = cfg.get("llm_api", {})
    prompt_dir = Path("data/llm_input") / stamp / "prompts"
    manifest_path = prompt_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    normalized_path = Path("data/normalized") / stamp / "documents.json"
    normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
    documents = normalized.get("documents", [])
    entries = manifest.get("prompts", [])
    if args.company != "all":
        entries = [entry for entry in entries if entry.get("key") == args.company]

    print(f"[TokenHub] 日期={target_date}，任务数={len(entries)}，模型={api.get('model', 'hy3')}")
    for entry in entries:
        prompt = Path(entry["prompt_file"]).read_text(encoding="utf-8")
        print(f"- {entry['company']}: {len(prompt)} 字，联网搜索={'是' if entry['key'] == 'dzqh' and api.get('dzqh_web_search', True) else '否'}")
    if args.dry_run:
        return

    if args.revalidate_existing:
        out_dir = Path("data/llm_output") / stamp
        for entry in entries:
            company_path = out_dir / f"{entry['key']}.json"
            data = json.loads(company_path.read_text(encoding="utf-8-sig"))
            candidates = list(data.get("records", []))
            raw_response = str(data.get("raw_response") or "")
            if raw_response:
                try:
                    raw_result = parse_json_response(raw_response, target_date, entry["company"])
                    candidates = _merge_by_commodity(candidates, raw_result.get("records", []))
                except (ValueError, json.JSONDecodeError):
                    pass
            accepted, rejected = _postprocess_records(candidates, documents, entry["company"])
            if entry["key"] in {"gfqh", "citicsf", "gtjaqh"}:
                deterministic_builders = {
                    "gfqh": gfqh_deterministic_table_records,
                    "citicsf": citicsf_deterministic_coverage_records,
                    "gtjaqh": gtja_deterministic_title_records,
                }
                deterministic = deterministic_builders[entry["key"]](documents, target_date)
                deterministic, deterministic_rejected = _postprocess_records(
                    deterministic, documents, entry["company"]
                )
                accepted = _merge_by_commodity(accepted, deterministic)
                rejected.extend(deterministic_rejected)
            data["records"] = accepted
            data["api_rejected"] = rejected
            if str(data.get("processing_status", "")).startswith("ok_api"):
                data["processing_status"] = "ok_api_revalidated"
            for part in data.get("parts", []):
                part["records"] = len(accepted)
            save_json(company_path, data)
            print(f"[重校验] {entry['company']}: 保留={len(accepted)}，拒绝={len(rejected)}，未调用API")
        return

    env_name = str(api.get("api_key_env", "TENCENT_TOKENHUB_API_KEY"))
    api_key = os.environ.get(env_name)
    key_file = Path(str(api.get("api_key_file", "secrets/tokenhub_api_key.txt")))
    if not api_key and key_file.exists():
        api_key = key_file.read_text(encoding="utf-8-sig").strip()
    if not api_key:
        raise SystemExit(
            f"缺少 API Key：请设置环境变量 {env_name}，或执行 .\\save_api_key.ps1"
        )
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("缺少 openai 依赖，请重新执行 .\\first_setup.ps1") from exc

    client = OpenAI(api_key=api_key, base_url=str(api["base_url"]))
    search_base_url = str(api.get("search_base_url", api["base_url"]))
    search_key = os.environ.get(str(api.get("search_api_key_env", "")))
    search_key_file = Path(str(api.get(
        "search_api_key_file", "secrets/tokenhub_search_api_key.txt"
    )))
    if not search_key and search_key_file.exists():
        search_key = search_key_file.read_text(encoding="utf-8-sig").strip()
    if not search_key and search_base_url == str(api["base_url"]):
        search_key = api_key
    search_client = (
        OpenAI(api_key=search_key, base_url=search_base_url) if search_key else None
    )
    out_dir = Path("data/llm_output") / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for index, entry in enumerate(entries, start=1):
        key, company = entry["key"], entry["company"]
        company_path = out_dir / f"{key}.json"
        source_count = int(entry.get("source_count", 0))
        if key != "dzqh" and source_count == 0:
            status = "no_source_reports"
            if not preserve_existing_success(company_path, target_date):
                save_json(company_path, {
                    "target_date": target_date, "company": company, "records": [],
                    "processing_status": status, "crawl": entry.get("crawl", {}),
                })
            summaries.append({"key": key, "status": status, "records": 0})
            print(f"[{index}/{len(entries)}] {company}: 无当日来源，不调用 API")
            continue

        prompt = Path(entry["prompt_file"]).read_text(encoding="utf-8")
        search_enabled = key == "dzqh" and bool(api.get("dzqh_web_search", True))
        extra_body: dict = {"thinking": {"type": "disabled"}}
        if search_enabled:
            extra_body["web_search_options"] = {
                "enable": True,
                "search_source": str(api.get("dzqh_search_source", "standard")),
            }
        print(f"[{index}/{len(entries)}] {company}: 调用中…")
        try:
            if key == "dzqh":
                if search_client is None:
                    raise RuntimeError(
                        "东证联网检索需要广州 TokenHub API Key；先在广州站开通联网搜索，"
                        "再执行 .\\save_api_key.ps1 -Search"
                    )
                discovery, discovery_usage, discovery_search = _call_text_model(
                    search_client, api, _dzqh_discovery_prompt(target_date), extra_body
                )
                classify_prompt = (
                    prompt + "\n\n以下是刚完成的联网资料发现结果。只能使用其中可核实的真实来源，"
                    "按上面的 JSON 规则分类：\n" + discovery
                    + "\n\n以下 SEARCH_RESULTS 是搜索工具直接返回的真实标题、URL 和摘要；"
                    "它们比上面的概述优先。摘要中只要同时有东证署名、目标日期、品种和"
                    "明确未来方向，就应逐项输出，不得因为无法打开微信公众号而返回空数组：\n"
                    + _search_results_evidence(discovery_search)
                )
                result, raw, usage = _call_model(
                    client, api, target_date, company, classify_prompt,
                    {"thinking": {"type": "disabled"}},
                    max_tokens=8000,
                )
                raw = "--- DISCOVERY ---\n" + discovery + "\n\n--- CLASSIFICATION ---\n" + raw
                calls = [
                    {"kind": "dzqh_discovery", "usage": discovery_usage,
                     "search": discovery_search},
                    {"kind": "primary_classification", "usage": usage},
                ]
            else:
                result, raw, usage = _call_model(
                    client, api, target_date, company, prompt, extra_body
                )
                calls = [{"kind": "primary", "usage": usage}]

            if key == "citicsf" and not result.get("records"):
                print(f"[{index}/{len(entries)}] {company}: 首轮异常为空，自动重试一次…")
                retry_result, retry_raw, retry_usage = _call_model(
                    client, api, target_date, company,
                    prompt + "\n\n上一次响应异常地返回了空数组。请重新逐篇、逐小标题检查全部来源后输出。",
                    extra_body,
                )
                calls.append({"kind": "citicsf_primary_retry", "usage": retry_usage})
                raw = raw + "\n\n--- PRIMARY RETRY ---\n" + retry_raw
                result = retry_result

            if key == "dzqh" and not result.get("records"):
                print(f"[{index}/{len(entries)}] {company}: 首轮为 0，按公众号优先规则补充检索…")
                retry_discovery, retry_discovery_usage, retry_search = _call_text_model(
                    search_client, api, _dzqh_discovery_prompt(target_date, focused=True), extra_body
                )
                retry_prompt = (
                    _dzqh_retry_prompt(target_date, prompt)
                    + "\n\n第二轮联网资料发现结果：\n" + retry_discovery
                    + "\n\n第二轮 SEARCH_RESULTS 原始证据：\n"
                    + _search_results_evidence(retry_search)
                )
                retry_result, retry_raw, retry_usage = _call_model(
                    client, api, target_date, company, retry_prompt,
                    {"thinking": {"type": "disabled"}},
                    max_tokens=8000,
                )
                calls.extend([
                    {"kind": "dzqh_focused_discovery", "usage": retry_discovery_usage,
                     "search": retry_search},
                    {"kind": "dzqh_focused_classification", "usage": retry_usage},
                ])
                raw = (
                    raw + "\n\n--- FOCUSED DISCOVERY ---\n" + retry_discovery
                    + "\n\n--- FOCUSED CLASSIFICATION ---\n" + retry_raw
                )
                result = retry_result

            accepted, rejected = _postprocess_records(
                result.get("records", []), documents, company
            )

            if key == "gfqh":
                deterministic_records = gfqh_deterministic_table_records(documents, target_date)
                deterministic_records, deterministic_rejected = _postprocess_records(
                    deterministic_records, documents, company
                )
                accepted = _merge_by_commodity(accepted, deterministic_records)
                rejected.extend(deterministic_rejected)
                calls.append({
                    "kind": "gfqh_python_table_supplement",
                    "candidates": len(deterministic_records),
                    "usage": {},
                })

            if key == "gtjaqh":
                deterministic_records = gtja_deterministic_title_records(documents, target_date)
                deterministic_records, deterministic_rejected = _postprocess_records(
                    deterministic_records, documents, company
                )
                accepted = _merge_by_commodity(accepted, deterministic_records)
                rejected.extend(deterministic_rejected)
                calls.append({
                    "kind": "gtja_python_title_supplement",
                    "candidates": len(deterministic_records),
                    "usage": {},
                })

            if key == "citicsf":
                missing = _citic_missing_candidates(documents, accepted)
                if missing:
                    print(f"[{index}/{len(entries)}] {company}: 首轮仍漏 {len(missing)} 个正文小标题，补充复核…")
                    extra_no_search = {"thinking": {"type": "disabled"}}
                    supplement, supplement_raw, supplement_usage = _call_model(
                        client, api, target_date, company,
                        _citic_supplement_prompt(target_date, missing), extra_no_search,
                        max_tokens=6000,
                    )
                    supplement_records, supplement_rejected = _postprocess_records(
                        supplement.get("records", []), documents, company
                    )
                    accepted = _merge_by_commodity(accepted, supplement_records)
                    rejected.extend(supplement_rejected)
                    calls.append({
                        "kind": "citicsf_coverage_supplement",
                        "candidates": len(missing),
                        "usage": supplement_usage,
                    })
                    raw = raw + "\n\n--- COVERAGE SUPPLEMENT ---\n" + supplement_raw

                deterministic_records = citicsf_deterministic_coverage_records(
                    documents, target_date
                )
                deterministic_records, deterministic_rejected = _postprocess_records(
                    deterministic_records, documents, company
                )
                accepted = _merge_by_commodity(accepted, deterministic_records)
                rejected.extend(deterministic_rejected)
                calls.append({
                    "kind": "citicsf_python_coverage_supplement",
                    "candidates": len(deterministic_records),
                    "usage": {},
                })

            dzqh_search_result_count = 0
            if key == "dzqh":
                dzqh_search_result_count = sum(
                    len((call.get("search") or {}).get("search_results") or [])
                    for call in calls
                )
                if dzqh_search_result_count == 0:
                    if accepted:
                        rejected.append({
                            "reason": "API 未返回 search_results，拒绝无法证明来自真实联网搜索的记录",
                            "record_count": len(accepted),
                        })
                    accepted = []

            result["records"] = accepted
            status = "ok_api"
            errors: list[dict] = []
            if key == "dzqh":
                if dzqh_search_result_count == 0:
                    status = "llm_search_unavailable"
                    errors.append({
                        "error": "TokenHub 响应未包含 search_results，联网搜索未实际生效"
                    })
                elif not accepted:
                    status = "llm_search_empty"
                    errors.append({
                        "error": "东证两轮联网检索均未取得带真实文章 URL 的当天可靠观点"
                    })
            aggregate_usage = aggregate_call_usage(calls)
            output = {
                **result,
                "processing_status": status,
                "crawl": entry.get("crawl", {}),
                "api": {
                    "provider": api.get("provider", "tokenhub"),
                    "model": api.get("model", "hy3"),
                    "web_search": search_enabled,
                    "usage": aggregate_usage,
                    "calls": calls,
                },
                "raw_response": raw,
                "api_rejected": rejected,
                "parts": [{"part_index": 1, "status": status, "records": len(result["records"])}],
                "errors": errors,
            }
            save_json(company_path, output)
            (out_dir / f"error_{key}_api.json").unlink(missing_ok=True)
            summaries.append({"key": key, "status": status, "records": len(result["records"]), "usage": aggregate_usage})
            print(f"[{index}/{len(entries)}] {company}: 完成，{len(result['records'])} 条，"
                  f"校验拒绝={len(rejected)}，总用量={aggregate_usage}")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            save_json(out_dir / f"error_{key}_api.json", {
                "target_date": target_date, "company": company, "error": error,
            })
            preserved = preserve_existing_success(company_path, target_date)
            if not preserved:
                save_json(company_path, {
                    "target_date": target_date, "company": company, "records": [],
                    "processing_status": "llm_api_error", "crawl": entry.get("crawl", {}),
                    "errors": [{"error": error}],
                })
            summaries.append({"key": key, "status": "preserved_existing_success" if preserved else "llm_api_error", "error": error})
            print(f"[{index}/{len(entries)}] {company}: 失败 | {error}")

    save_json(out_dir / "api_run_summary.json", {"target_date": target_date, "companies": summaries})


if __name__ == "__main__":
    main()
