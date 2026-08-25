from __future__ import annotations
import json
from .base import BaseScraper
from ..models import RawResearchItem
from ..utils import clean_text

class CITICSFScraper(BaseScraper):
    key='citicsf'; company='中信期货'; start_url='https://inst.citicsf.com/research-report/researchReportQuery'

    async def _query_page(self, page_num: int, page_size: int = 100):
        compact=self.target_date.replace('-','')
        payload={
            'beginDate': compact, 'endDate': compact,
            'catIdList': [], 'pageNum': page_num, 'pageSize': page_size,
            'varietyList': [], 'subVarietyList': [], 'title': '',
            'userNameList': [], 'languages': 'all', 'reportTypes': ['normal','vip'],
        }
        response=await self.context.request.post(
            'https://inst.citicsf.com/icsp-data-provider/api/t0/reportsQuery',
            data=json.dumps(payload,ensure_ascii=False), headers={'Content-Type':'application/json'}, timeout=60_000)
        if not response.ok:
            raise RuntimeError(f'中信列表 API HTTP {response.status}: {(await response.text())[:500]}')
        obj=await response.json()
        if (obj.get('head') or {}).get('rtnCode') != '00000000':
            raise RuntimeError(f"中信列表 API 失败: {(obj.get('head') or {}).get('rtnMsg')}")
        return obj
    async def scrape(self):
        first=await self._query_page(1); total=int(first.get('count') or 0)
        rows=list(first.get('list') or [])
        page_size=100
        for page_num in range(2, min((total+page_size-1)//page_size, 20)+1):
            rows.extend((await self._query_page(page_num,page_size)).get('list') or [])
        target_compact=self.target_date.replace('-',''); items=[]
        for row in rows:
            rpt_date=str(row.get('rptDate') or '')[:8]
            if rpt_date != target_compact: continue
            authors='、'.join(x.get('researchName','') for x in (row.get('authorshipList') or []) if x.get('researchName')) or None
            varieties='、'.join(x.get('subVarietyName') or x.get('varietyName') or '' for x in (row.get('varietyAndSubVarietyList') or [])) or None
            rid=str(row.get('id') or '')
            url=f'{self.start_url}Detail?researchId={rid}&isTrue=true&fromMenu=ResearchReportQuery'
            items.append(RawResearchItem(
                company=self.company,source_date=self.target_date,source_title=clean_text(row.get('rptAllTitle') or ''),source_url=url,
                source_type='官方网站',source_name='中信期货机构服务平台',content_kind='full_text',
                content=clean_text(row.get('rptSummary') or ''),author=authors,category=varieties,source_id=rid,
                attachments=[str(x.get('attachmentId')) for x in (row.get('attachmentList') or []) if x.get('attachmentId')],
                crawl_status='ok' if row.get('rptSummary') else 'summary_text_empty',
                notes='中信官网公开 reportsQuery API；rptSummary 为官网报告摘要/正文。'))
        if not items:
            items.append(RawResearchItem(self.company,self.target_date,'',self.start_url,'官方网站','中信期货机构服务平台','full_text','',crawl_status='unresolved_source_access',notes='页面已打开但无法确认当天正文；不能解释为0条。'))
        return items
