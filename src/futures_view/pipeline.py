from __future__ import annotations
import argparse, asyncio
from pathlib import Path
import json
from .browser import browser_context
from .utils import load_yaml, save_json, shanghai_today
from .scrapers.gfqh import GFQHScraper
from .scrapers.citicsf import CITICSFScraper
from .scrapers.gtjaqh import GTJAQHScraper

SCRAPERS={'gfqh':GFQHScraper,'citicsf':CITICSFScraper,'gtjaqh':GTJAQHScraper}


def crawl_result_status(items: list[dict]) -> dict:
    """Distinguish a genuine empty day from a scraper that could not read its source."""
    count = len([item for item in items if item.get('source_title')])
    if count:
        return {'status': 'ok', 'count': count}
    unresolved = next((
        str(item.get('crawl_status')) for item in items
        if item.get('crawl_status') == 'unresolved_source_access'
    ), None)
    if unresolved:
        return {'status': unresolved, 'count': 0}
    return {'status': 'no_source_reports', 'count': 0}

async def crawl(args):
    cfg=load_yaml(args.config); date=args.date or shanghai_today(); root=Path(f'data/raw/{date.replace("-","")}'); root.mkdir(parents=True,exist_ok=True)
    selected=list(SCRAPERS) if args.company == 'all' else [args.company]
    all_items=[]; statuses={}
    async with browser_context(headed=args.headed) as context:
        for position, key in enumerate(selected, start=1):
            cls=SCRAPERS[key]
            if not cfg['sources'].get(key,{}).get('enabled',True): continue
            print(f'[官网进度 {position}/{len(selected)}] START {key} {date}', flush=True)
            s=cls(context,date,root,debug=args.debug)
            try: items=await s.scrape()
            except Exception as e:
                print(f'[ERROR] {key}: {e}'); items=[]; statuses[key]={'status':'error','error':f'{type(e).__name__}: {e}'}
            arr=[x.to_dict() for x in items]
            if key not in statuses:
                statuses[key]=crawl_result_status(arr)
            save_json(root/f'{key}.json',{'target_date':date,'company':cfg['sources'][key]['company'],'crawl':statuses[key],'items':arr}); all_items.extend(arr)
            print(f'[官网进度 {position}/{len(selected)}] DONE {key}: {len(arr)}', flush=True)
    # 单公司调试时保留同日期其他公司的已有结果，再重建 all_raw。
    merged_items=[]; merged_status={}
    for key in SCRAPERS:
        path=root/f'{key}.json'
        if not path.exists(): continue
        obj=json.loads(path.read_text(encoding='utf-8'))
        merged_items.extend(obj.get('items',[])); merged_status[key]=obj.get('crawl',{'status':'unknown'})
    save_json(root/'all_raw.json',{'target_date':date,'crawl_status':merged_status,'items':merged_items}); print(root/'all_raw.json')

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',default='config.yaml'); ap.add_argument('--date'); ap.add_argument('--company',choices=['all',*SCRAPERS],default='all'); ap.add_argument('--headed',action='store_true'); ap.add_argument('--debug',action='store_true'); args=ap.parse_args()
    asyncio.run(crawl(args))
if __name__=='__main__': main()
