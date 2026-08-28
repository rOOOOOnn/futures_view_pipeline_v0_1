from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re


def _top_left(box: Any) -> tuple[float, float]:
    try:
        points = list(box)
        return min(float(point[1]) for point in points), min(float(point[0]) for point in points)
    except (TypeError, ValueError, IndexError):
        return 0.0, 0.0


def rapidocr_rows(result: Any) -> list[tuple[Any, str, float | None]]:
    """Normalize RapidOCR v3 and legacy outputs while retaining every OCR box."""
    if result is None:
        return []

    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if texts is None and isinstance(result, tuple):
        result = result[0]
    if texts is None and isinstance(result, list):
        rows = []
        for entry in result:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            box = entry[0]
            value = entry[1]
            if isinstance(value, (list, tuple)):
                text = str(value[0]) if value else ""
                score = float(value[1]) if len(value) > 1 else None
            else:
                text, score = str(value), None
            rows.append((box, text, score))
    else:
        texts = list(texts) if texts is not None else []
        boxes = list(boxes) if boxes is not None else [None] * len(texts)
        scores = list(scores) if scores is not None else [None] * len(texts)
        rows = list(zip(boxes, (str(text) for text in texts), scores))

    rows = [(box, str(text).strip(), float(score) if score is not None else None)
            for box, text, score in rows if str(text).strip()]
    rows.sort(key=lambda row: _top_left(row[0]))
    return rows


def normalize_rapidocr_result(result: Any) -> tuple[str, dict]:
    """Normalize RapidOCR v3 and legacy outputs into ordered text plus audit metadata."""
    rows = rapidocr_rows(result)
    score_values = [float(row[2]) for row in rows if row[2] is not None]
    metadata = {
        "line_count": len(rows),
        "average_score": round(sum(score_values) / len(score_values), 4) if score_values else None,
    }
    return "\n".join(row[1].strip() for row in rows), metadata


def _bounds(box: Any) -> tuple[float, float, float, float]:
    points = list(box) if box is not None else []
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _join_cell(entries: list[tuple[Any, str, float | None]]) -> str:
    entries.sort(key=lambda row: _top_left(row[0]))
    # OCR blocks usually represent wrapped Chinese lines; inserting spaces corrupts phrases
    # such as “逢高空” into “逢高 空”.
    return "".join(row[1].strip() for row in entries if row[1].strip())


def _horizontal_grid_lines(image) -> list[int]:
    import cv2
    import numpy as np

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mask = cv2.threshold(gray, 220, 255, cv2.THRESH_BINARY_INV)[1]
    width = image.shape[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, width // 20), 1))
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    profile = (horizontal > 0).sum(axis=1)
    candidates = np.where(profile >= width * 0.75)[0]
    if not len(candidates):
        return []
    groups = np.split(candidates, np.where(np.diff(candidates) > 1)[0] + 1)
    # Use the bottom edge of thick rules so header characters cannot leak into row 1.
    return [int(group[-1]) for group in groups if len(group)]


def extract_gfqh_daily_review(path: Path) -> tuple[str, dict]:
    """Extract the Guangfa daily-review PNG as table rows, not a flat OCR paragraph."""
    try:
        import cv2
        import numpy as np
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "缺少图片表格识别依赖；请重新运行 first_setup.ps1 安装 rapidocr、opencv 与 onnxruntime"
        ) from exc

    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法读取图片：{path}")
    result = RapidOCR()(image)
    entries = rapidocr_rows(result)
    width = image.shape[1]

    title_y = 0.0
    for box, text, _ in entries:
        if "全品种日评" in text:
            _, y1, _, y2 = _bounds(box)
            title_y = max(title_y, (y1 + y2) / 2)
    header_entries = []
    for row in entries:
        box, text, _ = row
        _, y1, _, y2 = _bounds(box)
        cy = (y1 + y2) / 2
        if cy > title_y and text in {"板块", "品种", "主力合约", "点评", "操作建议"}:
            header_entries.append(row)
    if len(header_entries) < 4:
        raise RuntimeError("未定位到‘全品种日评’表头，无法安全地按表格拆分 OCR")
    header_y = sum((_bounds(row[0])[1] + _bounds(row[0])[3]) / 2 for row in header_entries) / len(header_entries)
    grid_lines = _horizontal_grid_lines(image)
    top_candidates = [line for line in grid_lines if line >= header_y]
    if not top_candidates:
        raise RuntimeError("未检测到全品种表格横线")
    table_top = min(top_candidates)
    boundaries = [line for line in grid_lines if line >= table_top]
    boundaries = [line for index, line in enumerate(boundaries)
                  if index == 0 or line - boundaries[index - 1] >= 12]
    if len(boundaries) < 10:
        raise RuntimeError(f"全品种表格横线过少：{len(boundaries)}")

    # Column boundaries are stable proportions of the official daily-review template.
    columns = {
        "commodity": (0.055 * width, 0.132 * width),
        "contract": (0.132 * width, 0.229 * width),
        "comment": (0.229 * width, 0.653 * width),
        "advice": (0.653 * width, 0.995 * width),
    }
    from .commodities import canonical_commodity, normalize_contract, sector_for_commodity

    table_rows: list[dict[str, Any]] = []
    for row_top, row_bottom in zip(boundaries, boundaries[1:]):
        cells: dict[str, list] = {key: [] for key in columns}
        for entry in entries:
            x1, y1, x2, y2 = _bounds(entry[0])
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            if not (row_top + 1 < cy < row_bottom - 1):
                continue
            for name, (left, right) in columns.items():
                if left <= cx < right:
                    cells[name].append(entry)
                    break
        raw_commodity = _join_cell(cells["commodity"])
        contract = _join_cell(cells["contract"])
        comment = _join_cell(cells["comment"])
        advice = _join_cell(cells["advice"])
        if not any((raw_commodity, contract, comment, advice)):
            continue
        commodity = canonical_commodity(raw_commodity, contract)
        table_rows.append({
            "row": len(table_rows) + 1,
            "sector_hint": sector_for_commodity(commodity),
            "commodity": commodity,
            "commodity_ocr": raw_commodity,
            "contract": normalize_contract(contract, commodity),
            "comment": comment,
            "advice": advice,
            "scored": sector_for_commodity(commodity) is not None,
        })

    scores = [row[2] for row in entries if row[2] is not None]
    unknown = [row["commodity"] for row in table_rows if not row["sector_hint"]]
    incomplete = [row["row"] for row in table_rows
                  if not row["commodity"] or not (row["comment"] or row["advice"])]
    metadata = {
        "engine": "RapidOCR+OpenCV-table",
        "line_count": len(entries),
        "average_score": round(sum(scores) / len(scores), 4) if scores else None,
        "table_row_count": len(table_rows),
        "scored_row_count": sum(bool(row["scored"]) for row in table_rows),
        "unscored_commodities": unknown,
        "incomplete_rows": incomplete,
        "grid_boundaries": len(boundaries),
    }
    payload = {
        "format": "gfqh_daily_review_table_v1",
        "instructions": (
            "逐行处理 rows；sector 必须使用 sector_hint；scored=false 的行不输出；"
            "每行仅使用该行 comment 与 advice 判断观点，不得跨行拼接。"
        ),
        "rows": table_rows,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2), metadata


def extract_image_text(path: Path) -> tuple[str, dict]:
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "缺少图片文字识别依赖；请重新运行 first_setup.ps1 安装 rapidocr 与 onnxruntime"
        ) from exc

    result = RapidOCR()(str(path))
    text, metadata = normalize_rapidocr_result(result)
    metadata["engine"] = "RapidOCR"
    return text, metadata
