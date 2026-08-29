from __future__ import annotations

import re
from functools import lru_cache
from html import unescape
from urllib.request import Request, urlopen
from urllib.parse import urlparse


def is_concrete_http_url(value: object) -> bool:
    """Reject model placeholders such as ``https://`` and host-only URLs."""
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.path not in {"", "/"}
    )


def _title_key(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def view_evidence_rejection(record: dict) -> str | None:
    """Enforce the project's strict rule that neutral is not merely uncertain."""
    evidence = str(record.get("evidence") or "")
    if record.get("view") == "中性":
        if re.search(r"震荡|区间|宽幅波动|窄幅交投|盘整|整理|观望|横盘|多空均衡|供需均衡|暂无趋势", evidence):
            return None
        return "中性观点缺少区间、震荡、观望或均衡等明确证据"

    return None


@lru_cache(maxsize=64)
def _public_page_text(url: str) -> str:
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{urlparse(url).scheme}://{urlparse(url).netloc}/",
    })
    with urlopen(request, timeout=20) as response:
        body = response.read(2_000_000)
        charset = response.headers.get_content_charset() or "utf-8"
    html = body.decode(charset, errors="ignore")
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def dzqh_source_attribution_rejection(record: dict, page_text: str | None = None) -> str | None:
    """Verify East Futures attribution from the real page, not LLM metadata."""
    url = str(record.get("source_url") or "")
    host = (urlparse(url).hostname or "").lower()
    if any(name in host for name in ("orientfutures", "dzqh")):
        return None
    try:
        text = page_text if page_text is not None else _public_page_text(url)
    except Exception as exc:
        return f"无法打开东证公开来源进行署名核验：{type(exc).__name__}"
    if re.search(r"东证期货|上海东证期货|东证衍生品研究院", text):
        return None
    return "真实网页未出现东证期货署名，疑似混入其他机构观点"


def repair_source_urls(
    records: list[dict], documents: list[dict], company: str
) -> tuple[list[dict], list[dict]]:
    """Replace LLM URLs with crawler URLs and reject unresolved placeholders.

    For the three crawler-backed companies, the normalized source document is
    authoritative.  A unique containment match is allowed because an LLM may
    omit a report-title prefix.  East Futures has no local document catalog, so
    its searched URL must independently pass the concrete-URL check.
    """
    company_documents = [
        doc for doc in documents if doc.get("company_hint") == company
    ]
    exact: dict[str, list[dict]] = {}
    for doc in company_documents:
        exact.setdefault(_title_key(doc.get("source_title")), []).append(doc)

    accepted: list[dict] = []
    rejected: list[dict] = []
    for record_index, original in enumerate(records, start=1):
        item = dict(original)
        title = _title_key(item.get("source_title"))
        matches = exact.get(title, [])
        if not matches and title:
            matches = [
                doc for doc in company_documents
                if title in _title_key(doc.get("source_title"))
                or _title_key(doc.get("source_title")) in title
            ]
        if len(matches) == 1:
            source = matches[0]
            item["source_url"] = source.get("source_url")
            item["source_title"] = source.get("source_title") or item.get("source_title")
            item["source_type"] = source.get("source_type") or item.get("source_type")
            item["source_name"] = source.get("source_name") or item.get("source_name")

        if is_concrete_http_url(item.get("source_url")):
            accepted.append(item)
        else:
            rejected.append({
                "record_index": record_index,
                "commodity": item.get("commodity"),
                "source_title": item.get("source_title"),
                "source_url": item.get("source_url"),
                "reason": "source_url 不是可定位到具体文章的真实 URL",
            })
    return accepted, rejected
