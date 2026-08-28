from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ocr import extract_gfqh_daily_review
from .utils import save_json, shanghai_today


def main() -> None:
    parser = argparse.ArgumentParser(description="用表格 OCR 重新处理已下载的广发日评 PNG")
    parser.add_argument("--date")
    parser.add_argument("--image", help="可选；不传时读取 gfqh.json 记录的本地图片")
    args = parser.parse_args()

    target_date = args.date or shanghai_today()
    stamp = target_date.replace("-", "")
    raw_dir = Path("data/raw") / stamp
    company_path = raw_dir / "gfqh.json"
    company_data = json.loads(company_path.read_text(encoding="utf-8-sig"))
    items = company_data.get("items", [])
    if not items:
        raise SystemExit(f"没有可重处理的广发原始记录：{company_path}")
    item = items[0]
    image_path = Path(args.image) if args.image else Path(item.get("local_files", [""])[0])
    if not image_path.exists():
        raise SystemExit(f"广发日评 PNG 不存在：{image_path}")

    content, metadata = extract_gfqh_daily_review(image_path)
    item["content"] = content
    item["content_kind"] = "image_ocr"
    item["crawl_status"] = "ok"
    item["notes"] = (
        "仅使用‘交易品种=综合’下的‘广发期货日评’PNG超级汇总图；"
        f"OCR={metadata['engine']}，文字块={metadata['line_count']}，"
        f"表格行={metadata['table_row_count']}，计分行={metadata['scored_row_count']}，"
        f"非计分品种={metadata['unscored_commodities']}，"
        f"不完整行={metadata['incomplete_rows']}，平均置信度={metadata['average_score']}。"
    )
    save_json(company_path, company_data)

    all_path = raw_dir / "all_raw.json"
    if all_path.exists():
        all_data = json.loads(all_path.read_text(encoding="utf-8-sig"))
        replaced = False
        for index, existing in enumerate(all_data.get("items", [])):
            if (existing.get("company") == "广发期货"
                    and existing.get("source_date") == target_date):
                all_data["items"][index] = item
                replaced = True
        if not replaced:
            all_data.setdefault("items", []).append(item)
        save_json(all_path, all_data)

    audit = {
        "target_date": target_date,
        "image": str(image_path),
        **metadata,
        "rows": json.loads(content)["rows"],
    }
    audit_path = raw_dir / "gfqh_ocr_audit.json"
    save_json(audit_path, audit)
    print(f"[广发表格OCR] 表格行={metadata['table_row_count']}，"
          f"计分行={metadata['scored_row_count']}，非计分={metadata['unscored_commodities']}")
    print(company_path)
    print(audit_path)


if __name__ == "__main__":
    main()
