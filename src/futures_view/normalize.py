from __future__ import annotations
import argparse, json, hashlib
from pathlib import Path
from .utils import save_json, shanghai_today

VALID_KINDS={'full_text','pdf_text','title_only','public_reprint'}

def normalize_item(x: dict, target_date: str) -> dict:
    content=(x.get('content') or '').strip()
    title=(x.get('source_title') or '').strip()
    base=f"{x.get('company')}|{x.get('source_url')}|{title}|{target_date}"
    return {
        'document_id': hashlib.sha1(base.encode('utf-8')).hexdigest()[:16],
        'target_date': target_date,
        'company_hint': x.get('company'),
        'source_date': x.get('source_date'),
        'source_type': x.get('source_type'),
        'source_name': x.get('source_name'),
        'source_title': title,
        'source_url': x.get('source_url'),
        'source_content_kind': x.get('content_kind') if x.get('content_kind') in VALID_KINDS else 'full_text',
        'source_text': content,
        'author': x.get('author'),
        'category_hint': x.get('category'),
        'crawl_status': x.get('crawl_status'),
        'notes': x.get('notes'),
        'source_id': x.get('source_id'),
    }

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--date'); ap.add_argument('--raw'); args=ap.parse_args()
    date=args.date or shanghai_today(); raw=Path(args.raw or f'data/raw/{date.replace("-","")}/all_raw.json')
    obj=json.loads(raw.read_text(encoding='utf-8')) if raw.exists() else {'items': [], 'crawl_status': {}}
    docs=[]
    for x in obj.get('items',[]):
        if x.get('source_title') and x.get('crawl_status') in {'ok','ok_title_only'}:
            docs.append(normalize_item(x,date))
    out=Path(f'data/normalized/{date.replace("-","")}/documents.json'); save_json(out,{'target_date':date,'crawl_status':obj.get('crawl_status',{}),'documents':docs})
    jsonl=Path(f'data/llm_input/{date.replace("-","")}/documents.jsonl'); jsonl.parent.mkdir(parents=True,exist_ok=True)
    jsonl.write_text('\n'.join(json.dumps(d,ensure_ascii=False) for d in docs)+'\n',encoding='utf-8')
    print(out); print(jsonl)
if __name__=='__main__': main()
