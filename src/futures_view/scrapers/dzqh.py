from __future__ import annotations

import html
import io
import json
import re
from pathlib import Path
from urllib.parse import quote, urlencode

from pypdf import PdfReader

from .base import BaseScraper
from ..models import RawResearchItem
from ..utils import clean_text


def _html_text(value: object) -> str:
    rendered = html.unescape(str(value or ""))
    rendered = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", rendered,
                      flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", rendered)
    text = re.sub(r"Reading Time:\s*(?:<?\s*\d+)?\s*minute", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def parse_official_posts(posts: list[dict], target_date: str) -> list[dict]:
    """Return exact-date East Futures reports and their official PDF links."""
    stamp = target_date.replace("-", "")
    results = []
    for post in posts:
        title = re.sub(
            r"\s+", " ",
            re.sub(r"<[^>]+>", " ", html.unescape(str((post.get("title") or {}).get("rendered", "")))),
        ).strip()
        published = str(post.get("date") or "")[:10]
        page_url = str(post.get("link") or "").strip()
        rendered = html.unescape(str((post.get("content") or {}).get("rendered", "")))
        excerpt = _html_text((post.get("excerpt") or {}).get("rendered", ""))
        pdf_match = re.search(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)', rendered, re.I)
        if (published != target_date or stamp not in title or "东证期货" not in title
                or not page_url or not pdf_match):
            continue
        pdf_url = quote(pdf_match.group(1).strip(), safe=":/%?=&")
        results.append({
            "post_id": str(post.get("id") or stamp),
            "source_date": published,
            "source_title": title,
            "source_url": page_url,
            "pdf_url": pdf_url,
            "excerpt": excerpt,
        })
    return results


def extract_pdf_text(data: bytes) -> tuple[str, int]:
    if not data.startswith(b"%PDF"):
        raise ValueError("响应不是 PDF 文件")
    reader = PdfReader(io.BytesIO(data))
    pages = [clean_text(page.extract_text() or "") for page in reader.pages]
    text = "\n\n".join(page for page in pages if page)
    if len(text) < 500:
        raise ValueError(f"PDF 可提取正文过短：{len(text)} 字")
    return text, len(reader.pages)


def extract_pdf_outlook_digest(text: str) -> str:
    """Keep only each commodity section's conclusion from an East morning report."""
    headings = list(re.finditer(r"(?m)^2\.\d+、[^\n.。]{2,100}$", text))
    parts: list[str] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.end():end]
        marker = section.find("总结与展望：")
        if marker < 0:
            continue
        summary = section[marker + len("总结与展望："):]
        summary = re.split(r"\n(?:上海东证期货有限公司|【分析师声明】)", summary, maxsplit=1)[0]
        summary = re.sub(
            r"综合晨报-日度报告\s*20\d{2}-\d{2}-\d{2}\s*\d+\s*期货研究报告",
            " ", summary,
        )
        summary = re.sub(r"\s+", " ", summary).strip()
        if summary:
            parts.append(f"{heading.group(0).strip()}\n总结与展望：{summary}")
    digest = "\n\n".join(parts)
    if len(parts) < 3 or len(digest) < 50:
        raise ValueError(f"PDF 总结与展望提取不足：{len(parts)} 节，{len(digest)} 字")
    return digest


class DZQHScraper(BaseScraper):
    key = "dzqh"
    company = "东证期货"
    start_url = "https://www.orientfutures.com.sg/zh/research/"
    api_url = "https://www.orientfutures.com.sg/wp-json/wp/v2/posts"

    async def _official_posts(self) -> list[dict]:
        params = urlencode({
            "search": self.target_date.replace("-", ""),
            "per_page": 10,
            "_fields": "id,date,link,title,content,excerpt",
        })
        response = await self.context.request.get(
            f"{self.api_url}?{params}", timeout=60_000,
            headers={"User-Agent": "Mozilla/5.0 futures-view-pipeline"},
        )
        if not response.ok:
            raise RuntimeError(f"东证官网文章 API HTTP {response.status}: {(await response.text())[:300]}")
        payload = json.loads(await response.text())
        if not isinstance(payload, list):
            raise RuntimeError("东证官网文章 API 返回格式异常")
        return payload

    async def scrape(self):
        posts = parse_official_posts(await self._official_posts(), self.target_date)
        if not posts:
            return []
        items = []
        errors = []
        pdf_dir = self.root / "pdfs" / self.key
        pdf_dir.mkdir(parents=True, exist_ok=True)
        for post in posts:
            try:
                response = await self.context.request.get(
                    post["pdf_url"], timeout=90_000,
                    headers={"User-Agent": "Mozilla/5.0 futures-view-pipeline"},
                )
                if not response.ok:
                    raise RuntimeError(f"PDF HTTP {response.status}")
                data = await response.body()
                full_text, page_count = extract_pdf_text(data)
                if self.target_date not in full_text or "东证期货" not in full_text:
                    raise ValueError("PDF 正文缺少目标日期或东证期货署名")
                digest = extract_pdf_outlook_digest(full_text)
                content_parts = []
                if post.get("excerpt"):
                    content_parts.append("官网摘要：\n" + post["excerpt"])
                content_parts.append("官方PDF各商品总结与展望：\n" + digest)
                text = "\n\n".join(content_parts)
                pdf_path = pdf_dir / f"dzqh_{post['post_id']}_{self.target_date.replace('-', '')}.pdf"
                pdf_path.write_bytes(data)
                items.append(RawResearchItem(
                    company=self.company,
                    source_date=post["source_date"],
                    source_title=post["source_title"],
                    source_url=post["source_url"],
                    source_type="官方网站PDF摘要",
                    source_name="东证期货新加坡官网",
                    content_kind="pdf_text",
                    content=text,
                    attachments=[post["pdf_url"]],
                    local_files=[str(pdf_path)],
                    crawl_status="ok",
                    source_id=post["post_id"],
                    notes=(f"东证官网 WordPress API 定位报告；PDF 共 {page_count} 页，"
                           f"本地仅保留官网摘要和各商品总结与展望，共 {len(text)} 字。"),
                ))
            except Exception as exc:
                errors.append(f"{post['source_title']}：{type(exc).__name__}: {exc}")
        if items:
            return items
        return [RawResearchItem(
            self.company, self.target_date, "", self.start_url,
            "官方网站PDF摘要", "东证期货新加坡官网", "pdf_text", "",
            crawl_status="unresolved_source_access",
            notes="官网已发现当天报告，但 PDF 下载或正文提取失败：" + " | ".join(errors),
        )]
