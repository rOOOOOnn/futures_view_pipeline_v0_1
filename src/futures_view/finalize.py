from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill

from .constants import (
    BEAR_THRESHOLD,
    BULL_THRESHOLD,
    COMPANIES,
    COMPANY_KEYS,
    CONFIDENCE_LEVELS,
    HIGH_CONSISTENCY_MAX_DIVERGENCE,
    MEDIUM_CONSISTENCY_MAX_DIVERGENCE,
    SECTORS,
    STRONG_BEAR_THRESHOLD,
    STRONG_BULL_THRESHOLD,
    VIEW_SCORE,
)
from .utils import save_json, shanghai_today
from .title_rules import title_only_rejection
from .commodities import canonical_commodity, sector_for_commodity
from .source_validation import (
    is_concrete_http_url,
    repair_source_urls,
    view_evidence_rejection,
)

REQUIRED_FIELDS = {
    "company", "sector", "commodity", "view", "view_date", "source_date",
    "source_type", "source_name", "source_title", "source_url", "evidence", "confidence",
}
SOURCE_PRIORITY = {"官方PDF": 6, "官方网站PDF": 6, "官方网站": 6, "微信公众号": 5,
                   "财经网站转载": 3, "公开转载": 2, "公众号转载": 2, "官方网站标题": 1}
CONFIDENCE_PRIORITY = {"高": 3, "中": 2, "低": 1}

NAVY, BLUE, WHITE, BLACK = "1F4E78", "5B9BD5", "FFFFFF", "1F1F1F"
YELLOW, RED, GREEN, GRAY = "FFF2CC", "F4CCCC", "D9EAD3", "E7E6E6"
FONT = "Microsoft YaHei"


def parse_date(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("日期不是字符串")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def record_quality(record: dict[str, Any]) -> tuple[int, int, int]:
    return (SOURCE_PRIORITY.get(str(record.get("source_type")), 0),
            CONFIDENCE_PRIORITY.get(str(record.get("confidence")), 0),
            len(str(record.get("evidence", ""))))


def validate_company_file(path: Path, key: str, target_date: str,
                          documents: list[dict] | None = None) -> dict[str, Any]:
    company = COMPANY_KEYS[key]
    if not path.exists():
        return {"target_date": target_date, "company": company, "records": [],
                "processing_status": "missing_llm_output", "rejected": [f"缺少输入文件 {path}"]}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    status = data.get("processing_status", "unknown")
    rejected: list[str] = [
        "API校验："
        + str(item.get("commodity") or "未知品种")
        + "："
        + str(item.get("reason") or "未说明原因")
        for item in data.get("api_rejected", [])
    ]
    candidates: list[dict[str, Any]] = []
    repaired_records, url_rejected = repair_source_urls(
        data.get("records", []), documents or [], company
    )
    rejected.extend(
        f"URL校验：{item.get('commodity')}：{item.get('reason')}"
        for item in url_rejected
    )
    for index, raw in enumerate(repaired_records, start=1):
        reasons = []
        if not isinstance(raw, dict):
            reasons.append("不是 object")
        else:
            missing = REQUIRED_FIELDS - set(raw)
            if missing: reasons.append(f"缺字段 {sorted(missing)}")
            empty = [field for field in REQUIRED_FIELDS if field in raw and not raw[field]]
            if empty: reasons.append(f"空字段 {empty}")
        if not reasons:
            if raw["company"] != company: reasons.append(f"company 应为 {company}")
            if raw["sector"] not in SECTORS: reasons.append("sector 非法")
            if raw["view"] not in VIEW_SCORE: reasons.append("view 非法")
            if raw["confidence"] not in CONFIDENCE_LEVELS: reasons.append("confidence 非法")
            if raw["commodity"] == "碳酸锂" and raw["sector"] != "碳酸锂": reasons.append("碳酸锂板块错误")
            if raw["commodity"] == "多晶硅" and raw["sector"] != "多晶硅": reasons.append("多晶硅板块错误")
            canonical = canonical_commodity(str(raw["commodity"]))
            expected_sector = sector_for_commodity(canonical)
            if canonical == "综合" and raw["sector"] in SECTORS: pass
            elif expected_sector is None: reasons.append("品种不属于固定七个评分板块")
            elif raw["sector"] != expected_sector: reasons.append(f"sector 应为 {expected_sector}")
            try:
                if parse_date(raw["view_date"]) != target_date: reasons.append("view_date 非目标日")
                if parse_date(raw["source_date"]) > target_date: reasons.append("source_date 晚于目标日")
            except ValueError as exc: reasons.append(str(exc))
            if not is_concrete_http_url(raw["source_url"]): reasons.append("source_url 不是具体文章 URL")
            evidence_reason = view_evidence_rejection(raw)
            if evidence_reason: reasons.append(evidence_reason)
            title_reason = title_only_rejection(raw)
            if title_reason: reasons.append(title_reason)
        if reasons:
            rejected.append(f"第 {index} 条：{'；'.join(reasons)}")
            continue
        record = {field: raw[field] for field in REQUIRED_FIELDS}
        record["score"] = VIEW_SCORE[record["view"]]
        candidates.append(record)

    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in candidates:
        groups[(record["company"], record["sector"], record["commodity"], record["view_date"])].append(record)
    records = [max(group, key=record_quality) for group in groups.values()]
    return {"target_date": target_date, "company": company, "records": records,
            "processing_status": status, "crawl": data.get("crawl", {}), "rejected": rejected,
            "input_record_count": len(data.get("records", []))}


def company_sector_scores(records: list[dict[str, Any]]) -> dict[str, float]:
    groups: dict[str, list[float]] = defaultdict(list)
    for record in records:
        groups[record["sector"]].append(float(record["score"]))
    return {sector: sum(values) / len(values) for sector, values in groups.items()}


def prior_company_score(output_root: Path, stamp: str, key: str, sector: str) -> tuple[float | None, str | None]:
    for folder in sorted((p for p in output_root.iterdir() if p.is_dir() and p.name.isdigit() and p.name < stamp),
                         key=lambda p: p.name, reverse=True) if output_root.exists() else []:
        path = folder / f"{key}.json"
        if not path.exists(): continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig"))
            scores = company_sector_scores(obj.get("records", []))
            if sector in scores: return scores[sector], folder.name
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return None, None


def consistency(divergence: float | None, count: int) -> str:
    if divergence is None or count < 2: return "无数据"
    if divergence <= HIGH_CONSISTENCY_MAX_DIVERGENCE: return "高"
    if divergence <= MEDIUM_CONSISTENCY_MAX_DIVERGENCE: return "中"
    return "低"


def judgement(average: float | None, divergence: float | None) -> str:
    if average is None: return "无数据"
    if average >= STRONG_BULL_THRESHOLD and divergence is not None and divergence <= 1: return "一致看多"
    if average >= BULL_THRESHOLD: return "偏多"
    if average > BEAR_THRESHOLD: return "中性"
    if average <= STRONG_BEAR_THRESHOLD and divergence is not None and divergence <= 1: return "一致看空"
    return "偏空"


def build_matrix(company_data: dict[str, dict[str, Any]], output_root: Path, stamp: str) -> list[dict[str, Any]]:
    today_scores = {key: company_sector_scores(data["records"]) for key, data in company_data.items()}
    rows = []
    for sector in SECTORS:
        scores, dates, carried = {}, {}, {}
        for key, company in COMPANY_KEYS.items():
            score = today_scores[key].get(sector)
            if score is not None:
                scores[company], dates[company], carried[company] = score, stamp, False
            else:
                score, date_stamp = prior_company_score(output_root, stamp, key, sector)
                scores[company], dates[company], carried[company] = score, date_stamp, score is not None
        available = [value for value in scores.values() if value is not None]
        average = sum(available) / len(available) if available else None
        divergence = max(available) - min(available) if len(available) >= 2 else None
        rows.append({"sector": sector, "scores": scores, "dates": dates, "carried": carried,
                     "count": len(available), "average": average, "divergence": divergence,
                     "consistency": consistency(divergence, len(available)),
                     "judgement": judgement(average, divergence)})
    return rows


def style_row(cells, fill=NAVY, color=WHITE):
    for cell in cells:
        cell.fill = PatternFill("solid", fgColor=fill)
        cell.font = Font(name=FONT, bold=True, color=color)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def add_summary_sheet(wb: Workbook, target_date: str, rows: list[dict[str, Any]], company_data: dict[str, dict[str, Any]]):
    ws = wb.active; ws.title = "期货观点汇总"; ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:I1"); ws["A1"] = f"期货公司板块观点评分矩阵 | {target_date}"
    style_row(ws[1]); ws["A1"].font = Font(name=FONT, bold=True, color=WHITE, size=16); ws.row_dimensions[1].height = 34
    ws.merge_cells("A2:I2"); ws["A2"] = "公司板块得分为该公司在该板块全部商品观点的等权平均；黄色为历史沿用，灰色为无可用观点。"
    ws["A2"].fill = PatternFill("solid", fgColor="D9EAF7"); ws["A2"].font = Font(name=FONT, color="666666")
    headers = ["板块", *COMPANIES, "板块平均", "多空分歧", "一致性", "综合判断"]
    ws.append(headers); style_row(ws[3], BLUE)
    for row_index, row in enumerate(rows, start=4):
        ws.append([row["sector"], *(row["scores"][company] for company in COMPANIES), row["average"],
                   row["divergence"], row["consistency"], row["judgement"]])
        for col_index, company in enumerate(COMPANIES, start=2):
            cell = ws.cell(row_index, col_index); key = next(k for k,v in COMPANY_KEYS.items() if v == company)
            if cell.value is None:
                cell.fill = PatternFill("solid", fgColor=GRAY)
                cell.comment = Comment(f"当日无观点且无历史观点。处理状态：{company_data[key]['processing_status']}", "Codex")
            elif row["carried"][company]:
                cell.fill = PatternFill("solid", fgColor=YELLOW)
                reason = company_data[key]["processing_status"]
                cell.comment = Comment(f"沿用最近观点，来源日期：{row['dates'][company]}；当日处理状态：{reason}", "Codex")
            else:
                cell.comment = Comment(f"当日观点，来源日期：{target_date}", "Codex")
    end = 3 + len(rows)
    for row in ws.iter_rows(min_row=4, max_row=end):
        for cell in row:
            cell.font = Font(name=FONT, color=BLACK); cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for col in [2,3,4,5,6,7]:
        for cell in ws.iter_cols(min_col=col, max_col=col, min_row=4, max_row=end):
            for item in cell: item.number_format = "0.0"
    ws.conditional_formatting.add(f"B4:F{end}", ColorScaleRule(start_type="num", start_value=-2, start_color=RED,
        mid_type="num", mid_value=0, mid_color=YELLOW, end_type="num", end_value=2, end_color=GREEN))
    ws.conditional_formatting.add(f"G4:G{end}", ColorScaleRule(start_type="min", start_color=GREEN,
        mid_type="percentile", mid_value=50, mid_color=YELLOW, end_type="max", end_color=RED))
    widths = {"A":14,"B":15,"C":15,"D":18,"E":15,"F":13,"G":13,"H":11,"I":14}
    for column, width in widths.items(): ws.column_dimensions[column].width = width
    ws.freeze_panes = "B4"; ws.auto_filter.ref = f"A3:I{end}"


def add_detail_sheet(wb: Workbook, company_data: dict[str, dict[str, Any]]):
    ws = wb.create_sheet("当日观点明细"); ws.sheet_view.showGridLines = False
    headers = ["期货公司","板块","品种","观点","分数","观点日期","来源日期","置信度","证据","来源类型","来源名称","来源标题","来源URL"]
    ws.append(headers); style_row(ws[1])
    for key in COMPANY_KEYS:
        for record in company_data[key]["records"]:
            ws.append([record["company"],record["sector"],record["commodity"],record["view"],record["score"],
                       record["view_date"],record["source_date"],record["confidence"],record["evidence"],
                       record["source_type"],record["source_name"],record["source_title"],record["source_url"]])
    for row in ws.iter_rows(min_row=2):
        for cell in row: cell.font=Font(name=FONT,size=10); cell.alignment=Alignment(vertical="top",wrap_text=True)
        row[12].hyperlink=row[12].value; row[12].style="Hyperlink"; row[12].alignment=Alignment(vertical="top",wrap_text=True)
        ws.row_dimensions[row[0].row].height=62
    widths=[16,12,14,11,9,13,13,10,54,16,20,46,42]
    for i,width in enumerate(widths,1): ws.column_dimensions[chr(64+i)].width=width
    ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions


def add_quality_sheet(wb: Workbook, company_data: dict[str, dict[str, Any]]):
    ws=wb.create_sheet("运行与数据质量"); ws.sheet_view.showGridLines=False
    headers=["期货公司","处理状态","采集状态","有效观点数","拒绝记录数","说明"]
    ws.append(headers); style_row(ws[1])
    for key,company in COMPANY_KEYS.items():
        data=company_data[key]; crawl=data.get("crawl",{}); status=data["processing_status"]
        note="正常" if str(status).startswith("ok") else "状态非 ok；汇总表可能沿用历史观点，单元格批注含来源日期。"
        ws.append([company,status,crawl.get("status",""),len(data["records"]),len(data["rejected"]),note])
    start=ws.max_row+3; ws.cell(start,1,"公司 × 板块当日观点条数"); ws.merge_cells(start_row=start,start_column=1,end_row=start,end_column=8); style_row(ws[start])
    ws.append(["公司",*SECTORS]); style_row(ws[ws.max_row],BLUE)
    for key,company in COMPANY_KEYS.items():
        counts=Counter(r["sector"] for r in company_data[key]["records"]); ws.append([company,*(counts[s] for s in SECTORS)])
        for cell in ws[ws.max_row][1:]:
            if cell.value==0: cell.fill=PatternFill("solid",fgColor=RED)
    for row in ws.iter_rows():
        for cell in row: cell.font=Font(name=FONT,bold=cell.font.bold,color=cell.font.color); cell.alignment=Alignment(vertical="top",wrap_text=True)
    ws.column_dimensions["A"].width=20; ws.column_dimensions["B"].width=20; ws.column_dimensions["C"].width=18
    for col in "DEFGH": ws.column_dimensions[col].width=18
    ws.freeze_panes="A2"


def canonical_excel_path(target_date: str, output_root: str | Path = "output") -> Path:
    stamp = target_date.replace("-", "")
    return Path(output_root) / stamp / f"期货公司观点汇总_{stamp}.xlsx"


def ensure_excel_output_available(target_date: str, output_root: str | Path = "output") -> Path:
    """Fail before paid/long-running stages when the fixed output is open in Excel."""
    excel = canonical_excel_path(target_date, output_root)
    if not excel.exists():
        return excel
    try:
        with excel.open("r+b"):
            pass
    except PermissionError as exc:
        raise RuntimeError(
            f"[输出文件被占用] 请关闭 Excel 中的文件后重新运行：{excel}。"
            "程序不会另存‘更新版’，以保证每天只有一个固定结果文件。"
        ) from exc
    return excel


def main() -> None:
    parser=argparse.ArgumentParser(description="校验四家公司 JSON，计算评分并输出日期 Excel")
    parser.add_argument("--date"); parser.add_argument("--input-root",default="data/llm_output"); parser.add_argument("--output-root",default="output")
    args=parser.parse_args(); target_date=args.date or shanghai_today(); stamp=target_date.replace("-","")
    input_dir=Path(args.input_root)/stamp; output_root=Path(args.output_root); output_dir=output_root/stamp; output_dir.mkdir(parents=True,exist_ok=True)
    try:
        requested_excel=ensure_excel_output_available(target_date, output_root)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    normalized_path=Path("data/normalized")/stamp/"documents.json"
    documents=(json.loads(normalized_path.read_text(encoding="utf-8")).get("documents", [])
               if normalized_path.exists() else [])
    company_data={}
    for key in COMPANY_KEYS:
        data=validate_company_file(input_dir/f"{key}.json",key,target_date,documents); company_data[key]=data
        final_obj={k:v for k,v in data.items() if k not in {"rejected","input_record_count"}}
        final_obj["schema_version"]="1.0"; save_json(output_dir/f"{key}.json",final_obj)
    rows=build_matrix(company_data,output_root,stamp)
    wb=Workbook(); add_summary_sheet(wb,target_date,rows,company_data); add_detail_sheet(wb,company_data); add_quality_sheet(wb,company_data)
    excel=requested_excel
    wb.save(excel)
    save_json(output_dir/f"summary_{stamp}.json",{
        "target_date":target_date,"rows":rows,"excel_file":str(excel),
        "generated_at":datetime.now().isoformat(timespec="seconds"),
    })
    print(excel)


if __name__ == "__main__":
    main()
