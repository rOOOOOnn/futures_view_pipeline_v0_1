from __future__ import annotations
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse
from .base import BaseScraper
from ..models import RawResearchItem
from ..utils import clean_text, normalize_date_in_text, extract_pdf_text

class GFQHScraper(BaseScraper):
    key = "gfqh"; company = "广发期货"; start_url = "https://rd.gfqh.cn/RDInformation/menu/235"

    async def scrape(self):
        page = await self.new_page()
        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(3500)
        # 官网支持日期筛选；指定历史日期时无需人工翻页。
        try:
            await page.locator('#beginDate').fill(self.target_date)
            await page.locator('#endDate').fill(self.target_date)
            await page.locator('.query-btn').click()
            await page.wait_for_timeout(1800)
        except Exception:
            pass
        for _ in range(6):
            await page.mouse.wheel(0, 1200); await page.wait_for_timeout(500)
        links = await page.locator('[onclick*="article/"]').evaluate_all("""
            els => els.map(el => {
              const raw = el.getAttribute('onclick') || '';
              const m = raw.match(/article\\/(\\d+)/);
              const spans = [...el.querySelectorAll('span')].map(x => (x.innerText || '').trim()).filter(Boolean);
              return {href: m ? new URL('./article/' + m[1], document.baseURI).href : '',
                      text: (el.querySelector('.article-title')?.innerText || spans.find(x => !/^20\\d{2}-\\d{2}-\\d{2}$/.test(x)) || '').trim(),
                      date: (el.querySelector('.article-publish-date,.publish-date')?.innerText || '').trim()};
            })
        """)
        if not links:
            links = await page.locator('a[href*="/RDInformation/article/"]').evaluate_all(
                "els => els.map(a => ({href:a.href,text:(a.innerText||'').trim(),date:''}))")
        links = list({x.get('href'): x for x in links if x.get('href')}.values())[:120]
        if not links: await self.dump_debug(page, "no_article_links")
        items=[]; pdf_dir=self.root/"pdfs"/"gfqh"; pdf_dir.mkdir(parents=True, exist_ok=True)
        for idx,c in enumerate(links):
            dpage=await self.new_page()
            try:
                await dpage.goto(c['href'], wait_until="domcontentloaded", timeout=45_000); await dpage.wait_for_timeout(800)
                body=clean_text(await dpage.locator('body').inner_text()); d=c.get('date') or normalize_date_in_text(body)
                if d != self.target_date: continue
                title=c.get('text') or await dpage.title()
                anchors=await dpage.locator('a').evaluate_all("els => els.map(a => ({href:a.href||'',text:(a.innerText||'').trim()}))")
                data_urls=await dpage.locator('[data-url]').evaluate_all(
                    "els => els.map(e => ({href:e.getAttribute('data-url')||'',text:(e.getAttribute('data-name')||e.innerText||'').trim()}))")
                pdf_urls=[]
                for a in [*anchors, *data_urls]:
                    href=a.get('href') or ''; text=a.get('text') or ''
                    if '.pdf' in href.lower() or text.lower().endswith('.pdf'):
                        pdf_urls.append(urljoin(dpage.url, href))
                pdf_urls=list(dict.fromkeys(pdf_urls)); local=[]; texts=[]
                for j,u in enumerate(pdf_urls):
                    try:
                        resp=await self.context.request.get(u, timeout=45_000)
                        data=await resp.body()
                        if not data.startswith(b'%PDF'): continue
                        fn=re.sub(r'[<>:"/\\|?*]+','_',Path(urlparse(u).path).name or f'{idx}_{j}.pdf')
                        p=pdf_dir/fn; p.write_bytes(data); local.append(str(p))
                        t=extract_pdf_text(p)
                        if t: texts.append(t)
                    except Exception: pass
                content=clean_text('\n\n'.join(texts))
                items.append(RawResearchItem(
                    company=self.company, source_date=d, source_title=title, source_url=c['href'],
                    source_type='官方网站PDF', source_name='广发期货研究资讯', content_kind='pdf_text', content=content,
                    attachments=pdf_urls, local_files=local,
                    crawl_status='ok' if len(content)>=80 else ('pdf_text_too_short' if pdf_urls else 'no_pdf_found'),
                    notes='广发官网：详情页下载公开PDF，LLM分析PDF提取文本。'))
            except Exception:
                await self.dump_debug(dpage, f'detail_{idx}')
            finally:
                if not dpage.is_closed(): await dpage.close()
        if not items:
            items.append(RawResearchItem(self.company,self.target_date,'',self.start_url,'官方网站PDF','广发期货研究资讯','pdf_text','',crawl_status='unresolved_source_access',notes='未确认到当天公开PDF；不能解释为当天0条。'))
        await page.close(); return items
