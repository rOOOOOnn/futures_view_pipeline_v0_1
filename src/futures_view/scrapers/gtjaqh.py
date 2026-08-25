from __future__ import annotations
import re
from urllib.parse import quote
from .base import BaseScraper
from ..models import RawResearchItem
from ..utils import clean_text, normalize_date_in_text

class GTJAQHScraper(BaseScraper):
    key='gtjaqh'; company='国泰海通期货'; start_url='https://www.gtjaqh.com/pc/report?catType=&page=1'
    async def scrape(self):
        items=[]; seen=set(); page=None
        target_slash=self.target_date.replace('-','/')
        for page_no in range(1, 9):
            page=await self.new_page()
            url=f'https://www.gtjaqh.com/pc/report?catType=&page={page_no}'
            try:
                await page.goto(url,wait_until='domcontentloaded',timeout=60_000); await page.wait_for_timeout(1200)
                links=await page.locator('a[href*="/pc/reportDetail/"]').evaluate_all("""
                    els => els.map(a => ({
                      href:a.href,
                      title:(a.querySelector('[class*="list_item_text"]')?.innerText || '').trim(),
                      date:(a.querySelector('[class*="list_item_date"]')?.innerText || '').trim(),
                      type:(a.querySelector('[class*="list_item_type"]')?.innerText || '').trim()
                    }))
                """)
                if not links and page_no == 1: await self.dump_debug(page,'no_links')
                page_dates={x.get('date') for x in links if x.get('date')}
                for x in links:
                    href=x.get('href'); d=(x.get('date') or '').replace('/','-')
                    if not href or href in seen or d != self.target_date: continue
                    seen.add(href); title=clean_text(x.get('title') or '')
                    if not title: continue
                    items.append(RawResearchItem(
                        company=self.company,source_date=d,source_title=title,source_url=href,
                        source_type='官方网站标题',source_name='国泰君安期货研究资讯',content_kind='title_only',
                        content=title,category=x.get('type') or None,crawl_status='ok_title_only',
                        notes='国泰官网公开页只提供标题/日期；全文在投研宝小程序。按用户要求，仅以明确标题作保守分类。'))
                if links and target_slash not in page_dates and any(d < target_slash for d in page_dates): break
            except Exception:
                await self.dump_debug(page,f'list_{page_no}')
                if page_no == 1: raise
                break
            finally:
                if page and not page.is_closed(): await page.close()
        if not items:
            items.append(RawResearchItem(self.company,self.target_date,'',self.start_url,'官方网站标题','国泰君安期货研究资讯','title_only','',crawl_status='unresolved_source_access',notes='未确认到当天公开标题。'))
        return items
