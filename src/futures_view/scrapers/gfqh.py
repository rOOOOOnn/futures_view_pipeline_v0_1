from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .base import BaseScraper
from ..models import RawResearchItem
from ..ocr import extract_gfqh_daily_review
from ..utils import clean_text, normalize_date_in_text


class GFQHScraper(BaseScraper):
    key = "gfqh"
    company = "广发期货"
    start_url = "https://rd.gfqh.cn/RDInformation/menu/235"
    comprehensive_category_id = "100009"

    @staticmethod
    def is_daily_review_title(title: str) -> bool:
        compact = re.sub(r"\s+", "", title or "")
        return bool(re.fullmatch(r"广发期货日评\d{8}", compact)) and "期现日报汇总" not in compact

    async def scrape(self):
        page = await self.new_page()
        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2500)
        try:
            await page.locator("#beginDate").fill(self.target_date)
            await page.locator("#endDate").fill(self.target_date)
            comprehensive = page.locator(
                f'li.query-item[data-id="{self.comprehensive_category_id}"]')
            if await comprehensive.count():
                await comprehensive.first.click()
                await page.wait_for_timeout(500)
            await page.locator(".query-btn").click()
            await page.wait_for_timeout(1800)
        except Exception as exc:
            print(f"[广发筛选警告] 日期/综合筛选未完全生效：{type(exc).__name__}: {exc}", flush=True)

        links = await page.locator('[onclick*="article/"]').evaluate_all("""
            els => els.map(el => {
              const raw = el.getAttribute('onclick') || '';
              const m = raw.match(/article\/(\d+)/);
              const spans = [...el.querySelectorAll('span')].map(x => (x.innerText || '').trim()).filter(Boolean);
              return {href: m ? new URL('./article/' + m[1], document.baseURI).href : '',
                      text: (el.querySelector('.article-title')?.innerText || spans.find(x => !/^20\d{2}-\d{2}-\d{2}$/.test(x)) || '').trim(),
                      date: (el.querySelector('.article-publish-date,.publish-date')?.innerText || '').trim()};
            })
        """)
        links = list({item.get("href"): item for item in links if item.get("href")}.values())
        links = [item for item in links if self.is_daily_review_title(item.get("text", ""))]
        if not links:
            await self.dump_debug(page, "no_daily_review")
            await page.close()
            return []

        image_dir = self.root / "images" / "gfqh"
        image_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for index, candidate in enumerate(links, start=1):
            print(f"[广发日评 {index}/{len(links)}] {candidate.get('text')}", flush=True)
            detail = await self.new_page()
            try:
                await detail.goto(candidate["href"], wait_until="domcontentloaded", timeout=45_000)
                await detail.wait_for_timeout(800)
                body = clean_text(await detail.locator("body").inner_text())
                source_date = candidate.get("date") or normalize_date_in_text(body)
                title = candidate.get("text") or await detail.title()
                if source_date != self.target_date or not self.is_daily_review_title(title):
                    continue
                if "综合" not in body:
                    print(f"[广发跳过] 详情页未确认交易品种为综合：{candidate['href']}", flush=True)
                    continue

                attachments = await detail.locator("[data-url]").evaluate_all("""
                    els => els.map(e => ({
                      url: e.getAttribute('data-url') || '',
                      name: e.getAttribute('data-name') || (e.innerText || '').trim()
                    }))
                """)
                pngs = []
                for attachment in attachments:
                    url = urljoin(detail.url, attachment.get("url") or "")
                    name = attachment.get("name") or Path(urlparse(url).path).name
                    filename_match = re.search(r"([^\r\n]+?\.png)", name, flags=re.IGNORECASE)
                    if filename_match:
                        name = filename_match.group(1).strip()
                    if url and (url.lower().endswith(".png") or name.lower().endswith(".png")):
                        pngs.append((url, name))
                pngs = list(dict.fromkeys(pngs))
                if not pngs:
                    items.append(RawResearchItem(
                        self.company, source_date, title, candidate["href"], "官方网站PNG",
                        "广发期货研究资讯", "image_ocr", "", category="综合",
                        crawl_status="daily_review_png_missing",
                        notes="已找到广发期货日评，但详情页没有PNG超级汇总附件。"))
                    continue

                png_url, attachment_name = pngs[0]
                safe_name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", attachment_name).strip(" ._")
                if not safe_name.lower().endswith(".png"):
                    safe_name += ".png"
                local_path = image_dir / safe_name
                response = await self.context.request.get(png_url, timeout=60_000)
                image_bytes = await response.body()
                if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError(f"PNG附件响应格式异常：{png_url}")
                local_path.write_bytes(image_bytes)
                print(f"[广发OCR] {local_path.name} | {len(image_bytes) / 1024:.1f} KB", flush=True)
                ocr_text, ocr_meta = await asyncio.to_thread(extract_gfqh_daily_review, local_path)
                content = clean_text(ocr_text)
                status = "ok" if len(content) >= 80 else "image_ocr_too_short"
                items.append(RawResearchItem(
                    company=self.company,
                    source_date=source_date,
                    source_title=title,
                    source_url=candidate["href"],
                    source_type="官方网站PNG",
                    source_name="广发期货研究资讯",
                    content_kind="image_ocr",
                    content=content,
                    category="综合",
                    attachments=[png_url],
                    local_files=[str(local_path)],
                    crawl_status=status,
                    notes=("仅使用‘交易品种=综合’下的‘广发期货日评’PNG超级汇总图；"
                           f"OCR={ocr_meta.get('engine')}，文字块={ocr_meta.get('line_count')}，"
                           f"表格行={ocr_meta.get('table_row_count')}，计分行={ocr_meta.get('scored_row_count')}，"
                           f"非计分品种={ocr_meta.get('unscored_commodities')}，"
                           f"不完整行={ocr_meta.get('incomplete_rows')}，"
                           f"平均置信度={ocr_meta.get('average_score')}。不使用PDF和期现日报汇总。"),
                ))
            except Exception as exc:
                print(f"[广发日评错误] {candidate.get('href')} | {type(exc).__name__}: {exc}", flush=True)
                await self.dump_debug(detail, f"daily_review_{index}")
            finally:
                if not detail.is_closed():
                    await detail.close()
        await page.close()
        return items
