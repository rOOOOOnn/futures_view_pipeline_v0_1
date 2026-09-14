from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.chart import Reference, ScatterChart, Series
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.comments import Comment
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

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
from .view_database import FIELD_NAMES, load_current_outlook, sync_output_history

REQUIRED_FIELDS = {
    "company", "sector", "commodity", "view", "view_date", "source_date",
    "source_type", "source_name", "source_title", "source_url", "evidence", "confidence",
}
SOURCE_PRIORITY = {"官方PDF": 6, "官方网站PDF": 6, "官方网站PDF摘要": 6, "官方网站": 6, "微信公众号": 5,
                   "财经网站转载": 3, "公开转载": 2, "公众号转载": 2, "官方网站标题": 1}
CONFIDENCE_PRIORITY = {"高": 3, "中": 2, "低": 1}

NAVY, BLUE, WHITE, BLACK = "1F4E78", "5B9BD5", "FFFFFF", "1F1F1F"
YELLOW, RED, GREEN, GRAY = "FFF2CC", "F4CCCC", "D9EAD3", "E7E6E6"
FONT = "Microsoft YaHei"
CHART_SECTORS = ("黑色系", "贵金属", "农产品", "有色金属", "能化板块")


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
        for field in FIELD_NAMES:
            record[field] = str(raw.get(field) or "").strip()
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


def _hide_series_line(series: Series) -> None:
    series.graphicalProperties.line.solidFill = WHITE
    series.graphicalProperties.line.width = 1000


def _style_marker_series(series: Series, symbol: str, size: int, color: str) -> None:
    _hide_series_line(series)
    series.marker.symbol = symbol
    series.marker.size = size
    series.marker.graphicalProperties.solidFill = color
    series.marker.graphicalProperties.line.noFill = True


def add_divergence_chart(ws, target_date: str, rows: list[dict[str, Any]]) -> None:
    """Add the editable institution-score range chart below the summary table."""
    row_by_sector = {row["sector"]: row for row in rows}
    chart_rows = [row_by_sector[sector] for sector in CHART_SECTORS if sector in row_by_sector]
    if not chart_rows:
        return

    summary_row_by_sector = {row["sector"]: index for index, row in enumerate(rows, start=4)}
    chart_summary_rows = [summary_row_by_sector[row["sector"]] for row in chart_rows]
    if chart_summary_rows != list(range(chart_summary_rows[0], chart_summary_rows[0] + len(chart_rows))):
        raise ValueError("分歧图所需板块在汇总表中必须连续排列")

    helper_headers = [
        "纵向位置", *[f"{company}位置" for company in COMPANIES], "均值位置",
        "最低分", "最高分", "区间起点纵坐标", "区间终点纵坐标", "标签横坐标",
        "零轴横坐标", "零轴纵坐标", "刻度横坐标", "刻度纵坐标",
    ]
    for column, value in enumerate(helper_headers, start=11):
        ws.cell(3, column, value)

    all_scores: list[float] = []
    for helper_index, row in enumerate(chart_rows):
        excel_row = chart_summary_rows[helper_index]
        y_position = len(chart_rows) - helper_index
        scores = [row["scores"][company] for company in COMPANIES]
        available = [float(score) for score in scores if score is not None]
        all_scores.extend(available)

        duplicate_counts = Counter(float(score) for score in scores if score is not None)
        seen: Counter[float] = Counter()
        jittered_positions = []
        for score in scores:
            if score is None:
                jittered_positions.append(y_position)
                continue
            numeric_score = float(score)
            seen[numeric_score] += 1
            offset = (seen[numeric_score] - (duplicate_counts[numeric_score] + 1) / 2) * 0.12
            jittered_positions.append(y_position + offset)

        minimum = min(available) if available else None
        maximum = max(available) if available else None
        helper_values = [
            y_position, *jittered_positions, y_position + 0.24, minimum, maximum,
            y_position, y_position, None, None, None,
        ]
        for column, value in enumerate(helper_values, start=11):
            ws.cell(excel_row, column, value)

    data_minimum = min(all_scores) if all_scores else -2.0
    data_maximum = max(all_scores) if all_scores else 2.0
    score_span = max(data_maximum - data_minimum, 1.0)
    left_padding = min(0.50, max(0.20, 0.32 * score_span))
    right_padding = min(0.25, max(0.10, 0.08 * score_span))
    label_padding = min(0.25, max(0.10, 0.12 * score_span))
    axis_minimum = min(data_minimum, 0.0) - left_padding
    axis_maximum = max(data_maximum, 0.0) + right_padding
    label_x = min(data_minimum, 0.0) - label_padding

    for excel_row in chart_summary_rows:
        ws.cell(excel_row, 21, label_x)
    ws.cell(chart_summary_rows[0], 22, 0.0)
    ws.cell(chart_summary_rows[0] + 1, 22, 0.0)
    ws.cell(chart_summary_rows[0], 23, 0.55)
    ws.cell(chart_summary_rows[0] + 1, 23, len(chart_rows) + 0.55)
    tick_start = math.ceil(axis_minimum / 0.5) * 0.5
    tick_end = math.floor(axis_maximum / 0.5) * 0.5
    tick_values = [
        tick_start + index * 0.5
        for index in range(int(round((tick_end - tick_start) / 0.5)) + 1)
    ]
    for index, tick_value in enumerate(tick_values, start=chart_summary_rows[0]):
        ws.cell(index, 24, tick_value)
        ws.cell(index, 25, 0.55)

    chart = ScatterChart()
    chart.style = 2
    chart.scatterStyle = "lineMarker"
    chart.title = f"机构观点与分歧 | {target_date}"
    chart.height = 12.5
    chart.width = 24
    chart.legend = None
    chart.roundedCorners = False
    chart.graphical_properties = GraphicalProperties()
    chart.graphical_properties.line.solidFill = "D9D9D9"
    chart.visible_cells_only = False
    chart.display_blanks = "gap"

    chart.x_axis.title = "观点评分（越高越偏多，0为中性）"
    chart.x_axis.axPos = "b"
    chart.x_axis.scaling.min = axis_minimum
    chart.x_axis.scaling.max = axis_maximum
    chart.x_axis.majorUnit = 0.5
    chart.x_axis.numFmt = "0.0"
    chart.x_axis.majorTickMark = "none"
    chart.x_axis.tickLblPos = "none"
    chart.x_axis.crossesAt = 0.45
    chart.x_axis.spPr = GraphicalProperties()
    chart.x_axis.spPr.line.noFill = True
    chart.x_axis.majorGridlines.spPr = GraphicalProperties()
    chart.x_axis.majorGridlines.spPr.line.solidFill = "EEEEEE"
    chart.x_axis.majorGridlines.spPr.line.width = 6668
    chart.y_axis.scaling.min = 0.45
    chart.y_axis.scaling.max = len(chart_rows) + 0.65
    chart.y_axis.axPos = "l"
    chart.y_axis.majorUnit = 1
    chart.y_axis.delete = True
    chart.y_axis.majorGridlines = None
    chart.y_axis.majorTickMark = "none"
    chart.y_axis.crossesAt = axis_minimum
    chart.y_axis.spPr = GraphicalProperties()
    chart.y_axis.spPr.line.noFill = True

    for excel_row, row in zip(chart_summary_rows, chart_rows):
        x_values = Reference(ws, min_col=17, max_col=18, min_row=excel_row, max_row=excel_row)
        y_values = Reference(ws, min_col=19, max_col=20, min_row=excel_row, max_row=excel_row)
        series = Series(y_values, x_values, title=f"{row['sector']}区间")
        series.marker.symbol = "none"
        series.graphicalProperties.line.solidFill = "B9BEC4"
        series.graphicalProperties.line.width = 11000
        chart.series.append(series)

    zero_x = Reference(
        ws, min_col=22, min_row=chart_summary_rows[0], max_row=chart_summary_rows[0] + 1
    )
    zero_y = Reference(
        ws, min_col=23, min_row=chart_summary_rows[0], max_row=chart_summary_rows[0] + 1
    )
    zero_series = Series(zero_y, zero_x, title="中性基准")
    zero_series.marker.symbol = "none"
    zero_series.graphicalProperties.line.solidFill = "B9BEC4"
    zero_series.graphicalProperties.line.width = 11000
    chart.series.append(zero_series)

    first_row, last_row = chart_summary_rows[0], chart_summary_rows[-1]
    for company_index, company in enumerate(COMPANIES):
        x_values = Reference(ws, min_col=2 + company_index, min_row=first_row, max_row=last_row)
        y_values = Reference(ws, min_col=12 + company_index, min_row=first_row, max_row=last_row)
        series = Series(y_values, x_values, title=company)
        _style_marker_series(series, "circle", 6, "7A7D80")
        chart.series.append(series)

    mean_x = Reference(ws, min_col=6, min_row=first_row, max_row=last_row)
    mean_y = Reference(ws, min_col=16, min_row=first_row, max_row=last_row)
    mean_series = Series(mean_y, mean_x, title="机构均值")
    _style_marker_series(mean_series, "diamond", 9, "2865C5")
    mean_series.dLbls = DataLabelList()
    mean_series.dLbls.showCatName = True
    mean_series.dLbls.showSerName = False
    mean_series.dLbls.showVal = False
    mean_series.dLbls.showLegendKey = False
    mean_series.dLbls.showPercent = False
    mean_series.dLbls.showBubbleSize = False
    mean_series.dLbls.numFmt = "0.0"
    mean_series.dLbls.dLblPos = "t"
    chart.series.append(mean_series)

    for excel_row, row in zip(chart_summary_rows, chart_rows):
        label_x_values = Reference(ws, min_col=21, min_row=excel_row, max_row=excel_row)
        label_y_values = Reference(ws, min_col=11, min_row=excel_row, max_row=excel_row)
        label_series = Series(label_y_values, label_x_values, title=row["sector"])
        _hide_series_line(label_series)
        label_series.marker.symbol = "circle"
        label_series.marker.size = 2
        label_series.marker.graphicalProperties.solidFill = WHITE
        label_series.marker.graphicalProperties.line.noFill = True
        label_series.dLbls = DataLabelList()
        label_series.dLbls.showSerName = True
        label_series.dLbls.showCatName = False
        label_series.dLbls.showVal = False
        label_series.dLbls.showLegendKey = False
        label_series.dLbls.showPercent = False
        label_series.dLbls.showBubbleSize = False
        label_series.dLbls.dLblPos = "l"
        chart.series.append(label_series)

    for index, tick_value in enumerate(tick_values, start=chart_summary_rows[0]):
        tick_x_values = Reference(ws, min_col=24, min_row=index, max_row=index)
        tick_y_values = Reference(ws, min_col=25, min_row=index, max_row=index)
        tick_series = Series(tick_y_values, tick_x_values, title=f"{tick_value:.1f}")
        _hide_series_line(tick_series)
        tick_series.marker.symbol = "circle"
        tick_series.marker.size = 2
        tick_series.marker.graphicalProperties.solidFill = WHITE
        tick_series.marker.graphicalProperties.line.noFill = True
        tick_series.dLbls = DataLabelList()
        tick_series.dLbls.showSerName = True
        tick_series.dLbls.showCatName = False
        tick_series.dLbls.showVal = False
        tick_series.dLbls.showLegendKey = False
        tick_series.dLbls.showPercent = False
        tick_series.dLbls.showBubbleSize = False
        tick_series.dLbls.dLblPos = "b"
        chart.series.append(tick_series)

    ws.add_chart(chart, "A12")
    ws.merge_cells("A38:I38")
    ws["A38"] = "灰点：机构评分    蓝色菱形：机构均值    灰线：最低分至最高分"
    ws.merge_cells("A39:I39")
    ws["A39"] = "重合点仅上下错开，横向评分保持原值。"
    ws.merge_cells("A40:I40")
    ws["A40"] = "图表比较五个大类板块；碳酸锂和多晶硅保留在上方汇总表中。"
    for row_number in (38, 39, 40):
        ws.cell(row_number, 1).font = Font(name=FONT, size=9, color="666666")
        ws.cell(row_number, 1).alignment = Alignment(vertical="center")
    ws.column_dimensions.group("K", "Y", hidden=True)


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
    add_divergence_chart(ws, target_date, rows)


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


def _age_fill(age_days: int | None) -> PatternFill:
    if age_days is None or age_days <= 3:
        return PatternFill(fill_type=None)
    if age_days <= 7:
        return PatternFill("solid", fgColor=YELLOW)
    if age_days <= 14:
        return PatternFill("solid", fgColor="FCE5CD")
    return PatternFill("solid", fgColor=RED)


def add_midterm_sheet(wb: Workbook, target_date: str,
                      current_views: list[dict[str, Any]]) -> None:
    """Create the commodity-level current snapshot requested for medium-term review."""
    ws = wb.create_sheet("中期展望", 1)
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = "D00000"
    red = "D00000"
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells("A1:A2")
    ws.merge_cells("B1:E1")
    ws.merge_cells("F1:F2")
    ws.merge_cells("G1:J1")
    ws["A1"] = "品种"
    ws["B1"] = "核心变量"
    ws["F1"] = "中期展望"
    ws["G1"] = "时效与来源"
    for column, value in enumerate(("供给", "需求", "库存", "核心逻辑"), start=2):
        ws.cell(2, column, value)
    for column, value in enumerate(("方向", "更新时间", "数据年龄", "观点来源"), start=7):
        ws.cell(2, column, value)
    for row in ws.iter_rows(min_row=1, max_row=2, min_col=1, max_col=10):
        for cell in row:
            cell.fill = PatternFill("solid", fgColor=red)
            cell.font = Font(name=FONT, bold=True, color=WHITE, size=12)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border
    ws.row_dimensions[1].height = 25
    ws.row_dimensions[2].height = 25

    sector_order = {sector: index for index, sector in enumerate(SECTORS)}
    ordered = sorted(current_views, key=lambda item: (
        sector_order.get(str(item.get("sector")), 999), str(item.get("commodity")),
    ))
    field_columns = {
        "supply": 2, "demand": 3, "inventory": 4,
        "core_logic": 5, "medium_outlook": 6,
    }
    for row_number, item in enumerate(ordered, start=3):
        ws.cell(row_number, 1, item.get("commodity"))
        for field_name, column in field_columns.items():
            cell = ws.cell(row_number, column, item.get(field_name) or "")
            source = (item.get("field_sources") or {}).get(field_name)
            if source:
                source_date = str(source.get("view_date") or "")
                age = ((date.fromisoformat(target_date) - date.fromisoformat(source_date)).days
                       if source_date else None)
                cell.fill = _age_fill(age)
                cell.comment = Comment(
                    f"来源机构：{source.get('company', '')}\n"
                    f"观点日期：{source_date}\n"
                    f"来源标题：{source.get('source_title', '')}\n"
                    f"来源URL：{source.get('source_url', '')}",
                    "Codex",
                )
        score = item.get("score")
        direction = item.get("direction") or "无数据"
        ws.cell(row_number, 7, f"{direction} ({score:+.1f})" if score is not None else direction)
        ws.cell(row_number, 8, item.get("last_update"))
        ws.cell(row_number, 9, item.get("age_days"))
        ws.cell(row_number, 10, item.get("sources"))
        ws.cell(row_number, 9).fill = _age_fill(item.get("age_days"))
        if score is not None:
            ws.cell(row_number, 7).fill = PatternFill(
                "solid", fgColor=GREEN if score >= 0.5 else RED if score <= -0.5 else YELLOW
            )
        for cell in ws[row_number][:10]:
            cell.font = Font(name=FONT, color=BLACK, size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = border
        ws.row_dimensions[row_number].height = 72

    widths = {"A": 14, "B": 34, "C": 34, "D": 34, "E": 48,
              "F": 34, "G": 16, "H": 14, "I": 12, "J": 30}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    end = max(ws.max_row, 2)
    ws.freeze_panes = "B3"
    ws.auto_filter.ref = f"A2:J{end}"
    ws.sheet_view.zoomScale = 80


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
    parser.add_argument("--database", default="data/view_database.sqlite3")
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
        final_obj["schema_version"]="2.0"; save_json(output_dir/f"{key}.json",final_obj)
    rows=build_matrix(company_data,output_root,stamp)
    sync_output_history(output_root, args.database, stamp)
    current_views = load_current_outlook(args.database, target_date)
    save_json(output_dir/f"current_view_{stamp}.json", {
        "as_of_date": target_date, "views": current_views,
    })
    wb=Workbook(); add_summary_sheet(wb,target_date,rows,company_data); add_midterm_sheet(wb,target_date,current_views); add_detail_sheet(wb,company_data); add_quality_sheet(wb,company_data)
    excel=requested_excel
    wb.save(excel)
    save_json(output_dir/f"summary_{stamp}.json",{
        "target_date":target_date,"rows":rows,"excel_file":str(excel),
        "database_file":str(args.database),"current_view_count":len(current_views),
        "generated_at":datetime.now().isoformat(timespec="seconds"),
    })
    print(excel)


if __name__ == "__main__":
    main()
