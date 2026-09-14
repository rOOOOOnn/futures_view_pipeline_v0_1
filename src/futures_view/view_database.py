from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .commodities import canonical_commodity, sector_for_commodity
from .constants import COMPANY_KEYS, VIEW_SCORE


FIELD_NAMES = ("supply", "demand", "inventory", "core_logic", "medium_outlook")
FIELD_LABELS = {
    "supply": "供给",
    "demand": "需求",
    "inventory": "库存",
    "core_logic": "核心逻辑",
    "medium_outlook": "中期展望",
}
SOURCE_PRIORITY = {
    "官方PDF": 6,
    "官方网站PDF": 6,
    "官方网站PDF摘要": 6,
    "官方网站": 6,
    "官方网站PNG": 6,
    "微信公众号": 5,
    "财经网站转载": 3,
    "公开转载": 2,
    "公众号转载": 2,
    "官方网站标题": 1,
}
CONFIDENCE_PRIORITY = {"高": 3, "中": 2, "低": 1}

_SUPPLY = re.compile(r"供给|供应|产量|开工|检修|复产|减产|进口|发运|矿端|产能|到港")
_DEMAND = re.compile(r"需求|消费|成交|采购|下游|终端|铁水|旺季|补库|订单")
_INVENTORY = re.compile(r"库存|去库|累库|库销比|仓单|社库|厂库|港口库存")
_MEDIUM = re.compile(r"中期|中长期|长期|展望")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _matching_clauses(evidence: str, pattern: re.Pattern[str], commodity: str) -> str:
    clauses = [part.strip(" ，,；;。") for part in re.split(r"[；;。\n]+", evidence)]
    matches = []
    for part in clauses:
        if not part or not pattern.search(part):
            continue
        label = re.match(r"^([^：:]{1,16})[：:]", part)
        if label:
            labelled_commodity = canonical_commodity(label.group(1).strip())
            if (sector_for_commodity(labelled_commodity)
                    and labelled_commodity != canonical_commodity(commodity)):
                continue
        matches.append(part)
    return "；".join(dict.fromkeys(matches))


def structured_fields(record: dict[str, Any]) -> dict[str, str]:
    """Return explicit model fields, with conservative evidence-only legacy fallback."""
    evidence = _clean(record.get("evidence"))
    commodity = _clean(record.get("commodity"))
    values = {name: _clean(record.get(name)) for name in FIELD_NAMES}
    if evidence:
        values["supply"] = values["supply"] or _matching_clauses(evidence, _SUPPLY, commodity)
        values["demand"] = values["demand"] or _matching_clauses(evidence, _DEMAND, commodity)
        values["inventory"] = values["inventory"] or _matching_clauses(evidence, _INVENTORY, commodity)
        values["core_logic"] = values["core_logic"] or evidence
        values["medium_outlook"] = values["medium_outlook"] or _matching_clauses(
            evidence, _MEDIUM, commodity
        )
    return values


def _record_id(record: dict[str, Any]) -> str:
    identity = "|".join(_clean(record.get(key)) for key in (
        "company", "sector", "commodity", "view_date", "source_url", "source_title",
        "evidence", "view",
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _quality(record: dict[str, Any]) -> int:
    return (
        SOURCE_PRIORITY.get(_clean(record.get("source_type")), 0) * 100
        + CONFIDENCE_PRIORITY.get(_clean(record.get("confidence")), 0) * 10
        + min(len(_clean(record.get("evidence"))), 9)
    )


def connect_database(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS view_history (
            record_id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            sector TEXT NOT NULL,
            commodity TEXT NOT NULL,
            view TEXT NOT NULL,
            score REAL NOT NULL,
            view_date TEXT NOT NULL,
            source_date TEXT NOT NULL,
            supply TEXT NOT NULL DEFAULT '',
            demand TEXT NOT NULL DEFAULT '',
            inventory TEXT NOT NULL DEFAULT '',
            core_logic TEXT NOT NULL DEFAULT '',
            medium_outlook TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL,
            confidence TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            quality_rank INTEGER NOT NULL,
            inserted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_view_history_date
            ON view_history(view_date, company, commodity);
        CREATE TABLE IF NOT EXISTS current_company_views (
            company TEXT NOT NULL,
            commodity TEXT NOT NULL,
            sector TEXT NOT NULL,
            view TEXT NOT NULL,
            score REAL NOT NULL,
            view_date TEXT NOT NULL,
            source_date TEXT NOT NULL,
            confidence TEXT NOT NULL,
            source_title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            quality_rank INTEGER NOT NULL,
            PRIMARY KEY (company, commodity)
        );
        CREATE TABLE IF NOT EXISTS current_view_fields (
            commodity TEXT NOT NULL,
            field_name TEXT NOT NULL,
            field_value TEXT NOT NULL,
            company TEXT NOT NULL,
            sector TEXT NOT NULL,
            view_date TEXT NOT NULL,
            source_date TEXT NOT NULL,
            confidence TEXT NOT NULL,
            source_title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            quality_rank INTEGER NOT NULL,
            PRIMARY KEY (commodity, field_name)
        );
        CREATE VIEW IF NOT EXISTS current_view AS
        WITH commodities AS (
            SELECT commodity FROM current_view_fields
            UNION SELECT commodity FROM current_company_views
        )
        SELECT
            c.commodity,
            MAX(CASE WHEN f.field_name='supply' THEN f.field_value END) AS supply,
            MAX(CASE WHEN f.field_name='demand' THEN f.field_value END) AS demand,
            MAX(CASE WHEN f.field_name='inventory' THEN f.field_value END) AS inventory,
            MAX(CASE WHEN f.field_name='core_logic' THEN f.field_value END) AS core_logic,
            MAX(CASE WHEN f.field_name='medium_outlook' THEN f.field_value END) AS medium_outlook,
            (SELECT AVG(v.score) FROM current_company_views v
             WHERE v.commodity=c.commodity) AS direction_score,
            MAX(f.view_date) AS last_field_update
        FROM commodities c
        LEFT JOIN current_view_fields f ON f.commodity=c.commodity
        GROUP BY c.commodity;
        """
    )
    return connection


def ingest_records(connection: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for record in records:
        fields = structured_fields(record)
        score = float(record.get("score", VIEW_SCORE.get(_clean(record.get("view")), 0.0)))
        values = {
            "record_id": _record_id(record),
            "company": _clean(record.get("company")),
            "sector": _clean(record.get("sector")),
            "commodity": _clean(record.get("commodity")),
            "view": _clean(record.get("view")),
            "score": score,
            "view_date": _clean(record.get("view_date")),
            "source_date": _clean(record.get("source_date")),
            "evidence": _clean(record.get("evidence")),
            "confidence": _clean(record.get("confidence")),
            "source_type": _clean(record.get("source_type")),
            "source_name": _clean(record.get("source_name")),
            "source_title": _clean(record.get("source_title")),
            "source_url": _clean(record.get("source_url")),
            "inserted_at": now,
            **fields,
        }
        if not values["company"] or not values["commodity"] or not values["view_date"]:
            continue
        quality = _quality(record)
        values["quality_rank"] = quality
        connection.execute(
            """
            INSERT OR IGNORE INTO view_history (
                record_id, company, sector, commodity, view, score, view_date, source_date,
                supply, demand, inventory, core_logic, medium_outlook, evidence, confidence,
                source_type, source_name, source_title, source_url, quality_rank, inserted_at
            ) VALUES (
                :record_id, :company, :sector, :commodity, :view, :score, :view_date, :source_date,
                :supply, :demand, :inventory, :core_logic, :medium_outlook, :evidence, :confidence,
                :source_type, :source_name, :source_title, :source_url, :quality_rank, :inserted_at
            )
            """,
            values,
        )
        connection.execute(
            """
            INSERT INTO current_company_views (
                company, commodity, sector, view, score, view_date, source_date, confidence,
                source_title, source_url, quality_rank
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company, commodity) DO UPDATE SET
                sector=excluded.sector, view=excluded.view, score=excluded.score,
                view_date=excluded.view_date, source_date=excluded.source_date,
                confidence=excluded.confidence, source_title=excluded.source_title,
                source_url=excluded.source_url, quality_rank=excluded.quality_rank
            WHERE excluded.view_date > current_company_views.view_date
               OR (excluded.view_date = current_company_views.view_date
                   AND excluded.quality_rank > current_company_views.quality_rank)
            """,
            (values["company"], values["commodity"], values["sector"], values["view"], score,
             values["view_date"], values["source_date"], values["confidence"],
             values["source_title"], values["source_url"], quality),
        )
        for field_name, field_value in fields.items():
            if not field_value:
                continue
            connection.execute(
                """
                INSERT INTO current_view_fields (
                    commodity, field_name, field_value, company, sector, view_date, source_date,
                    confidence, source_title, source_url, quality_rank
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(commodity, field_name) DO UPDATE SET
                    field_value=excluded.field_value, company=excluded.company,
                    sector=excluded.sector, view_date=excluded.view_date,
                    source_date=excluded.source_date, confidence=excluded.confidence,
                    source_title=excluded.source_title, source_url=excluded.source_url,
                    quality_rank=excluded.quality_rank
                WHERE excluded.view_date > current_view_fields.view_date
                   OR (excluded.view_date = current_view_fields.view_date
                       AND excluded.quality_rank > current_view_fields.quality_rank)
                """,
                (values["commodity"], field_name, field_value, values["company"], values["sector"],
                 values["view_date"], values["source_date"], values["confidence"],
                 values["source_title"], values["source_url"], quality),
            )
    connection.commit()


def sync_output_history(output_root: str | Path, database_path: str | Path,
                        through_stamp: str) -> None:
    root = Path(output_root)
    with closing(connect_database(database_path)) as connection:
        if not root.exists():
            return
        for folder in sorted(path for path in root.iterdir()
                             if path.is_dir() and re.fullmatch(r"\d{8}", path.name)
                             and path.name <= through_stamp):
            records: list[dict[str, Any]] = []
            for key in COMPANY_KEYS:
                path = folder / f"{key}.json"
                if not path.exists():
                    continue
                try:
                    records.extend(json.loads(path.read_text(encoding="utf-8-sig")).get("records", []))
                except (OSError, json.JSONDecodeError, TypeError):
                    continue
            ingest_records(connection, records)


def _direction(average: float | None) -> str:
    if average is None:
        return "无数据"
    if average >= 1.25:
        return "强烈偏多"
    if average >= 0.5:
        return "偏多"
    if average > -0.5:
        return "中性"
    if average > -1.25:
        return "偏空"
    return "强烈偏空"


def load_current_outlook(database_path: str | Path, as_of_date: str) -> list[dict[str, Any]]:
    as_of = date.fromisoformat(as_of_date)
    with closing(connect_database(database_path)) as connection:
        commodities = [row[0] for row in connection.execute(
            """
            SELECT DISTINCT commodity FROM view_history WHERE view_date <= ?
            ORDER BY commodity
            """,
            (as_of_date,),
        )]
        result = []
        for commodity in commodities:
            history_rows = connection.execute(
                """
                SELECT * FROM view_history
                WHERE commodity=? AND view_date<=?
                ORDER BY view_date DESC, quality_rank DESC
                """,
                (commodity, as_of_date),
            ).fetchall()
            field_map: dict[str, dict[str, Any]] = {}
            for field_name in FIELD_NAMES:
                row = next((item for item in history_rows if _clean(item[field_name])), None)
                if row:
                    field_map[field_name] = {**dict(row), "field_value": row[field_name]}
            latest_by_company: dict[str, sqlite3.Row] = {}
            for row in history_rows:
                latest_by_company.setdefault(row["company"], row)
            view_rows = list(latest_by_company.values())
            scores = [float(row["score"]) for row in view_rows]
            average = sum(scores) / len(scores) if scores else None
            dates = [row["view_date"] for row in view_rows]
            dates.extend(row["view_date"] for row in field_map.values())
            last_update = max(dates) if dates else None
            age_days = (as_of - date.fromisoformat(last_update)).days if last_update else None
            sector = next((row["sector"] for row in field_map.values()), None)
            if not sector and view_rows:
                sector = view_rows[0]["sector"]
            sources = sorted({row["company"] for row in (*field_map.values(), *view_rows)})
            result.append({
                "commodity": commodity,
                "sector": sector or "",
                **{name: field_map.get(name, {}).get("field_value", "") for name in FIELD_NAMES},
                "direction": _direction(average),
                "score": average,
                "last_update": last_update,
                "age_days": age_days,
                "freshness": (
                    "正常" if age_days is not None and age_days <= 3 else
                    "关注" if age_days is not None and age_days <= 7 else
                    "偏旧" if age_days is not None and age_days <= 14 else "陈旧"
                ),
                "sources": "、".join(sources),
                "field_sources": field_map,
            })
        return result
