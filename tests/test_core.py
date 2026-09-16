import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from futures_view.finalize import (
    consistency,
    ensure_excel_output_available,
    judgement,
    prior_company_score,
)
from futures_view.build_prompts import (
    compact_citicsf_documents,
    compact_gfqh_documents,
    compact_gtja_documents,
    extract_citicsf_coverage_candidates,
    prefer_chinese_bilingual_text,
)
from futures_view.llm.api_llm import (
    _dzqh_processing_status,
    _merge_search_meta,
    _needs_dzqh_retry,
    _parse_dzqh_official_posts,
    _postprocess_records,
    _merge_by_commodity,
    aggregate_call_usage,
    call_with_model_fallback,
    configured_models,
    _search_results_evidence,
    citicsf_deterministic_coverage_records,
    enforce_commodity_sectors,
    filter_title_records,
    gfqh_deterministic_table_records,
    gtja_deterministic_title_records,
    response_schema,
)
from futures_view.source_validation import (
    dzqh_source_attribution_rejection,
    is_concrete_http_url,
    repair_source_urls,
    view_evidence_rejection,
)
from futures_view.commodities import (
    UNSCORED_COMMODITIES,
    canonical_commodity,
    normalize_contract,
    sector_for_commodity,
)
from futures_view.llm.browser_llm import (
    attachment_failure_response,
    content_limit_failure,
    parse_json_response,
)
from futures_view.ocr import normalize_rapidocr_result
from futures_view.scrapers.gfqh import GFQHScraper
from futures_view.scrapers.citicsf import CITICSFScraper
from futures_view.scrapers.dzqh import extract_pdf_outlook_digest, parse_official_posts
from futures_view.pipeline import crawl_result_status
from futures_view.title_rules import directional_view_override, title_only_rejection
from futures_view.utils import normalize_target_date
from futures_view.view_database import (
    connect_database,
    ingest_records,
    load_current_outlook,
    structured_fields,
)


class CoreTests(unittest.TestCase):
    def test_model_fallback_only_switches_on_http_402(self):
        class QuotaError(Exception):
            status_code = 402

        calls = []

        def operation(model):
            calls.append(model)
            if model == "hy3":
                raise QuotaError("quota")
            return "ok"

        value, model, attempts = call_with_model_fallback({
            "model": "hy3",
            "model_fallbacks": ["deepseek-v4-flash", "deepseek-v4-pro"],
        }, operation)
        self.assertEqual((value, model), ("ok", "deepseek-v4-flash"))
        self.assertEqual(calls, ["hy3", "deepseek-v4-flash"])
        self.assertEqual([item["model"] for item in attempts], ["hy3"])
        self.assertEqual(configured_models({
            "model": "hy3", "model_fallbacks": ["hy3", "deepseek-v4-flash"],
        }), ["hy3", "deepseek-v4-flash"])

        class OtherError(Exception):
            status_code = 429

        with self.assertRaises(OtherError):
            call_with_model_fallback({
                "model": "hy3", "model_fallbacks": ["deepseek-v4-flash"],
            }, lambda _: (_ for _ in ()).throw(OtherError("rate limit")))

    def test_history_carry_skips_missing_previous_day_and_uses_latest_sector_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "20260825").mkdir()
            (root / "20260825" / "gfqh.json").write_text(json.dumps({
                "records": [
                    {"sector": "黑色系", "score": 1},
                    {"sector": "黑色系", "score": -1},
                ],
            }), encoding="utf-8")
            (root / "20260826").mkdir()
            (root / "20260826" / "gfqh.json").write_text(json.dumps({
                "records": [{"sector": "农产品", "score": 2}],
            }), encoding="utf-8")
            self.assertEqual(
                prior_company_score(root, "20260827", "gfqh", "黑色系"),
                (0.0, "20260825"),
            )
            self.assertEqual(
                prior_company_score(root, "20260827", "gfqh", "贵金属"),
                (None, None),
            )

    def test_fixed_excel_output_refuses_lock_instead_of_making_update_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "20260827" / "期货公司观点汇总_20260827.xlsx"
            target.parent.mkdir()
            target.write_bytes(b"existing")
            with patch.object(Path, "open", side_effect=PermissionError("locked")):
                with self.assertRaisesRegex(RuntimeError, "不会另存‘更新版’"):
                    ensure_excel_output_available("2026-08-27", tmp)

    def test_search_result_evidence_keeps_url_title_and_snippet(self):
        payload = json.loads(_search_results_evidence({"search_results": [{
            "url": "https://example.com/report", "name": "东证晨报",
            "snippet": "碳酸锂短期震荡偏强", "site": "example.com",
        }]}))
        self.assertEqual(payload[0]["url"], "https://example.com/report")
        self.assertEqual(payload[0]["snippet"], "碳酸锂短期震荡偏强")

    def test_dzqh_retries_when_search_evidence_is_missing(self):
        accepted = [{"commodity": "铜"}]
        self.assertTrue(_needs_dzqh_retry(accepted, {"search_results": []}))
        self.assertTrue(_needs_dzqh_retry([], {
            "search_results": [{"url": "https://example.com/report"}],
        }))
        self.assertFalse(_needs_dzqh_retry(accepted, {
            "search_results": [{"url": "https://example.com/report"}],
        }))

    def test_dzqh_empty_result_is_not_a_search_failure(self):
        self.assertEqual(_dzqh_processing_status(0, []), (
            "llm_search_unavailable",
            "TokenHub 响应未包含 search_results，联网搜索未实际生效",
        ))
        self.assertEqual(_dzqh_processing_status(8, []), ("ok_api_empty", None))
        self.assertEqual(_dzqh_processing_status(8, [{"commodity": "铜"}]), (
            "ok_api", None,
        ))

    def test_dzqh_official_posts_are_filtered_by_exact_date(self):
        posts = [{
            "date": "2026-09-08T10:00:08",
            "link": "https://www.orientfutures.com.sg/research/report-20260908/",
            "title": {"rendered": "东证期货 日度报告 综合晨报 20260908"},
            "content": {"rendered": "<p>能源化工（原油）</p><p>油价短期偏强。</p>"},
        }, {
            "date": "2026-09-09T09:25:46",
            "link": "https://www.orientfutures.com.sg/research/report-20260909/",
            "title": {"rendered": "东证期货 日度报告 综合晨报 20260909"},
            "content": {"rendered": "<p>次日报告</p>"},
        }]
        results = _parse_dzqh_official_posts(posts, "2026-09-08")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["site"], "东证期货新加坡官网")
        self.assertIn("油价短期偏强", results[0]["snippet"])

    def test_dzqh_crawler_extracts_official_pdf_link_for_exact_date(self):
        posts = [{
            "id": 37939,
            "date": "2026-09-11T09:30:00",
            "link": "https://www.orientfutures.com.sg/research/report/",
            "title": {"rendered": "东证期货 日度报告 综合晨报 20260911"},
            "content": {"rendered": (
                '<a href="https://www.orientfutures.com.sg/wp-content/uploads/'
                '2026/09/东证期货_综合晨报_20260911.pdf">下载PDF</a>'
            )},
            "excerpt": {"rendered": "<p>油价短期偏强。</p>"},
        }]
        reports = parse_official_posts(posts, "2026-09-11")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["post_id"], "37939")
        self.assertIn("%E4%B8%9C%E8%AF%81", reports[0]["pdf_url"])
        self.assertEqual(reports[0]["excerpt"], "油价短期偏强。")
        self.assertEqual(parse_official_posts(posts, "2026-09-10"), [])

    def test_dzqh_pdf_digest_keeps_only_commodity_outlooks(self):
        text = """目录
2.1、黑色金属（铁矿石）................ 6
2.2、能源化工（原油）................ 7
2.1、黑色金属（铁矿石）
新闻和长篇点评不应保留。
总结与展望：短期矿价震荡偏弱。
2.2、能源化工（原油）
更多长篇资讯不应保留。
总结与展望：短期油价震荡偏强。
2.3、农产品（玉米）
供需信息。
总结与展望：玉米区间运行。"""
        digest = extract_pdf_outlook_digest(text)
        self.assertIn("短期矿价震荡偏弱", digest)
        self.assertIn("短期油价震荡偏强", digest)
        self.assertIn("玉米区间运行", digest)
        self.assertNotIn("长篇点评", digest)
        self.assertNotIn("................", digest)

    def test_dzqh_pdf_digest_accepts_investment_advice_layout(self):
        text = """目录
2.1、黑色金属（焦煤/焦炭）................ 6
2.2、能源化工（原油）................ 7
2.1、黑色金属（焦煤/焦炭）
新闻和长篇点评不应保留。
投资建议：短期供需偏紧，焦煤高位震荡。
2.2、能源化工（原油）
更多长篇资讯不应保留。
投资建议：油价高位震荡。
2.3、农产品（豆粕）
供需信息不应保留。
投资建议：豆粕震荡偏弱。"""
        digest = extract_pdf_outlook_digest(text)
        self.assertIn("短期供需偏紧，焦煤高位震荡", digest)
        self.assertIn("油价高位震荡", digest)
        self.assertIn("豆粕震荡偏弱", digest)
        self.assertNotIn("长篇点评", digest)
        self.assertNotIn("................", digest)

    def test_search_meta_prefers_first_source_and_deduplicates_urls(self):
        official = {"search_results": [{"url": "https://example.com/1", "name": "官网"}]}
        search = {"search_results": [
            {"url": "https://example.com/1", "name": "重复"},
            {"url": "https://example.com/2", "name": "补充"},
        ]}
        merged = _merge_search_meta(official, search)
        self.assertEqual([item["name"] for item in merged["search_results"]], ["官网", "补充"])

    def test_gtja_prompt_compaction_removes_duplicate_title_text(self):
        defaults, documents = compact_gtja_documents([{
            "source_date": "2026-08-26", "source_title": "锌：偏强震荡",
            "source_url": "https://example.com/1", "source_text": "锌：偏强震荡",
            "notes": "重复说明", "company_hint": "国泰海通期货",
        }])
        self.assertEqual(documents, [{
            "source_date": "2026-08-26", "source_title": "锌：偏强震荡",
            "source_url": "https://example.com/1",
        }])
        self.assertEqual(defaults["source_content_kind"], "title_only")

    def test_gfqh_prompt_compaction_keeps_only_scored_model_fields(self):
        defaults, documents = compact_gfqh_documents([{
            "source_date": "2026-09-11", "source_title": "广发期货日评20260911",
            "source_url": "https://example.com/gf", "source_text": json.dumps({
                "format": "gfqh_daily_review_table_v1",
                "rows": [{
                    "row": 1, "sector_hint": "有色金属", "commodity": "铜",
                    "commodity_ocr": "铜", "contract": "CU2610",
                    "comment": "库存下降", "advice": "逢低做多", "scored": True,
                }, {
                    "row": 2, "commodity": "股指", "comment": "上涨",
                    "advice": "", "scored": False,
                }],
            }, ensure_ascii=False),
        }])
        rows = json.loads(documents[0]["source_text"])["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"sector_hint", "commodity", "comment", "advice"})
        self.assertEqual(defaults["source_type"], "官方网站PNG")

    def test_citic_prompt_compaction_prefers_complete_chinese_copy(self):
        english = "Copper supply remains tight. " * 80
        chinese = "2025年以来铜矿供应偏紧，冶炼产能增长较快。" * 30
        combined = english + "\n\n" + chinese
        self.assertEqual(prefer_chinese_bilingual_text(combined), chinese)
        self.assertEqual(prefer_chinese_bilingual_text("铜价震荡偏强。"), "铜价震荡偏强。")
        defaults, documents, candidates = compact_citicsf_documents([{
            "company_hint": "中信期货", "source_date": "2026-09-11",
            "source_type": "官方网站", "source_name": "中信期货机构服务平台",
            "source_title": "铜日报", "source_url": "https://example.com/citic",
            "source_text": combined + "\n铜：震荡偏强",
            "document_id": "drop-me", "crawl_status": "ok", "notes": "drop-me",
        }])
        self.assertEqual(set(documents[0]), {
            "source_date", "source_title", "source_url", "source_text",
        })
        self.assertEqual(defaults["source_name"], "中信期货机构服务平台")
        self.assertEqual(set(candidates[0]), {
            "commodity_hint", "sector_hint", "statement", "source_title",
        })

    def test_api_schema_requires_strict_records(self):
        schema = response_schema("2026-08-26", "国泰海通期货")
        self.assertFalse(schema["additionalProperties"])
        item = schema["properties"]["records"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(item["properties"]["company"]["enum"], ["国泰海通期货"])
        self.assertIn("粕类", item["properties"]["commodity"]["enum"])
        self.assertIn("综合", item["properties"]["commodity"]["enum"])
        self.assertIn("[^/\\s]+", item["properties"]["source_url"]["pattern"])
        self.assertIn("supply", item["required"])
        self.assertIn("medium_outlook", item["required"])

    def test_current_view_updates_fields_independently_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "views.sqlite3"
            common = {
                "company": "广发期货", "sector": "黑色系", "commodity": "铁矿石",
                "view": "看多", "score": 1.0, "source_date": "2026-09-10",
                "source_type": "官方网站", "source_name": "广发期货",
                "source_title": "日报", "source_url": "https://example.com/report",
                "confidence": "高",
            }
            first = {**common, "view_date": "2026-09-10", "evidence": "发运回升；港口库存去化",
                     "supply": "澳巴发运回升", "demand": "", "inventory": "港口库存去化",
                     "core_logic": "供应回升但库存去化", "medium_outlook": ""}
            second = {**common, "view_date": "2026-09-11", "source_date": "2026-09-11",
                      "evidence": "铁水开始回落", "supply": "", "demand": "铁水开始回落",
                      "inventory": "", "core_logic": "需求边际走弱", "medium_outlook": "震荡"}
            with closing(connect_database(database)) as connection:
                ingest_records(connection, [first, second, second])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM view_history").fetchone()[0], 2)
            view = load_current_outlook(database, "2026-09-11")[0]
            self.assertEqual(view["supply"], "澳巴发运回升")
            self.assertEqual(view["demand"], "铁水开始回落")
            self.assertEqual(view["inventory"], "港口库存去化")
            self.assertEqual(view["medium_outlook"], "震荡")
            self.assertEqual(view["last_update"], "2026-09-11")

    def test_legacy_field_inference_does_not_cross_labelled_commodities(self):
        fields = structured_fields({
            "commodity": "硅铁",
            "evidence": "锰硅：原料成本与库存变动支撑；硅铁供应仍有回升预期",
        })
        self.assertEqual(fields["supply"], "硅铁供应仍有回升预期")
        self.assertEqual(fields["inventory"], "")

    def test_source_url_repair_uses_crawler_document(self):
        documents = [{
            "company_hint": "中信期货", "source_title": "农业日报",
            "source_url": "https://inst.citicsf.com/report/123",
            "source_type": "官方网站", "source_name": "中信官网",
        }]
        accepted, rejected = repair_source_urls([{
            "commodity": "生猪", "source_title": "农业日报", "source_url": "https://",
        }], documents, "中信期货")
        self.assertFalse(rejected)
        self.assertEqual(accepted[0]["source_url"], "https://inst.citicsf.com/report/123")
        self.assertFalse(is_concrete_http_url("https://"))
        self.assertFalse(is_concrete_http_url("https://example.com"))

    def test_citic_coverage_candidates_find_explicit_subheadings(self):
        candidates = extract_citicsf_coverage_candidates([{
            "company_hint": "中信期货", "source_date": "2026-08-27",
            "source_text": "生猪：供需宽松，猪价走弱\n棉花：区间运行\n风险因素：天气变化",
            "source_title": "农业日报", "source_url": "https://example.com/report",
            "source_type": "官方网站", "source_name": "中信官网",
        }])
        self.assertEqual([item["commodity_hint"] for item in candidates], ["生猪", "棉花"])

    def test_citic_explicit_coverage_is_recovered_without_another_api_call(self):
        documents = [{
            "company_hint": "中信期货", "source_date": "2026-08-27",
            "source_text": "生猪：供需宽松，猪价走弱\n棉花：区间运行",
            "source_title": "农业日报", "source_url": "https://example.com/report",
            "source_type": "官方网站", "source_name": "中信官网",
        }]
        records = citicsf_deterministic_coverage_records(documents, "2026-08-27")
        self.assertEqual(
            [(item["commodity"], item["view"]) for item in records],
            [("生猪", "看空"), ("棉花", "中性")],
        )

    def test_neutral_requires_explicit_neutral_evidence(self):
        self.assertIsNone(view_evidence_rejection({"view": "中性", "evidence": "价格区间震荡"}))
        self.assertIsNone(view_evidence_rejection({"view": "中性", "evidence": "钯：窄幅交投"}))
        self.assertIsNone(view_evidence_rejection({
            "view": "中性", "evidence": "短期主力合约或在13.8-15万元/吨运行",
        }))
        self.assertIsNotNone(view_evidence_rejection({
            "view": "中性", "evidence": "期货计价升水，但现货仍承压",
        }))
        self.assertIsNone(view_evidence_rejection({
            "view": "看多",
            "evidence": "主力合约短期仍有支撑，但上方空间仍受终端需求压制",
        }))

    def test_directional_oscillation_overrides_plain_neutral(self):
        bullish = [
            "甲醇震荡偏强；观望为主",
            "多晶硅期货震荡回升；观望",
            "下方仍有支撑，但上方空间受需求压制",
            "后续仍有提涨预期，但上涨空间受到压制",
            "供应端收缩政策预期对价格有支撑，但过剩预期限制上方高度",
        ]
        bearish = [
            "中期维持震荡偏弱，等待逢高做空机会",
            "需求疲软，弱势难改，盘面趋弱；暂观望",
            "现货小幅下调，价格延续调整；短期观望",
            "短期矿价高位有所调整。预计矿价下方空间可控",
        ]
        self.assertTrue(all(directional_view_override(text) == "看多" for text in bullish))
        self.assertTrue(all(directional_view_override(text) == "看空" for text in bearish))
        self.assertIsNone(directional_view_override("价格维持高位宽幅震荡，暂观望"))

    def test_postprocess_corrects_directional_neutral(self):
        record = {
            "company": "中信期货", "sector": "黑色系", "commodity": "铁矿石",
            "view": "中性", "view_date": "2026-08-27", "source_date": "2026-08-27",
            "source_type": "官方网站", "source_name": "中信",
            "source_title": "黑色日报", "source_url": "https://example.com/report",
            "evidence": "中期维持震荡偏弱，等待逢高做空机会", "confidence": "高",
        }
        accepted, rejected = _postprocess_records([record], [], "中信期货")
        self.assertFalse(rejected)
        self.assertEqual(accepted[0]["view"], "看空")

    def test_api_postprocess_rejects_model_field_drift(self):
        accepted, rejected = _postprocess_records([{
            "company": "中信期货", "sector": "农产品", "commodity": "生猪",
            "view_direction": "看空", "view_date": "2026-08-27",
            "source_date": "2026-08-27", "source_type": "官方网站",
            "source_name": "中信", "source_title": "农业日报",
            "source_url": "https://example.com/report", "evidence": "猪价走弱",
            "confidence": "高",
        }], [], "中信期货")
        self.assertFalse(accepted)
        self.assertIn("缺少必填字段", rejected[0]["reason"])

    def test_dzqh_attribution_must_exist_in_real_page(self):
        record = {"source_url": "https://example.com/report"}
        self.assertIsNone(dzqh_source_attribution_rejection(
            record, "来源：东证期货；豆粕短期偏强"
        ))
        self.assertIsNotNone(dzqh_source_attribution_rejection(
            record, "来源：兴业期货；纯碱价格偏弱"
        ))

    def test_title_rules_cover_clear_gtja_direction_phrases(self):
        cases = [
            ("看空", "纯碱趋势仍有压力"),
            ("看空", "燃料油短期转弱"),
            ("看空", "低硫燃料油继续回撤"),
            ("看空", "铂中枢下移"),
            ("看空", "焦炭价格冲高回落"),
            ("中性", "碳酸锂宽幅波动"),
            ("看多", "铜库存减少，支撑价格"),
            ("看多", "钢材近月因成本推升走强"),
            ("看多", "苯乙烯驱动有所转强"),
            ("看多", "主力合约短期仍有支撑，但上方空间受压"),
        ]
        for view, evidence in cases:
            self.assertIsNone(title_only_rejection({
                "source_type": "官方网站标题", "view": view, "evidence": evidence,
            }))

    def test_gtja_python_supplement_splits_multi_commodity_title(self):
        records = gtja_deterministic_title_records([{
            "company_hint": "国泰海通期货", "source_date": "2026-08-27",
            "source_type": "官方网站标题", "source_name": "国泰官网",
            "source_title": "燃料油：短期转弱 低硫燃料油：继续回撤",
            "source_url": "https://example.com/report/1",
        }], "2026-08-27")
        self.assertEqual(
            [(item["commodity"], item["view"]) for item in records],
            [("燃料油", "看空"), ("低硫燃料油", "看空")],
        )

    def test_gtja_python_supplement_keeps_every_explicit_multi_title_leg(self):
        documents = [
            {
                "company_hint": "国泰海通期货", "source_date": "2026-08-27",
                "source_type": "官方网站标题", "source_name": "国泰官网",
                "source_title": "黄金：短暂下破4600美元后反弹 白银：宽幅震荡",
                "source_url": "https://example.com/report/2",
            },
            {
                "company_hint": "国泰海通期货", "source_date": "2026-08-27",
                "source_type": "官方网站标题", "source_name": "国泰官网",
                "source_title": "对二甲苯：短期不追空 PTA：多PX空PTA，反套持有 MEG：单边不追空",
                "source_url": "https://example.com/report/3",
            },
        ]
        records = gtja_deterministic_title_records(documents, "2026-08-27")
        self.assertEqual(
            {(item["commodity"], item["view"]) for item in records},
            {("黄金", "看多"), ("白银", "中性"), ("PX", "看多"), ("PTA", "看空")},
        )

    def test_gtja_unknown_related_label_does_not_contaminate_known_commodity(self):
        records = gtja_deterministic_title_records([{
            "company_hint": "国泰海通期货", "source_date": "2026-08-27",
            "source_type": "官方网站标题", "source_name": "国泰官网",
            "source_title": "LPG：内外逻辑存分化 丙烯：短期现实偏强",
            "source_url": "https://example.com/report/4",
        }], "2026-08-27")
        self.assertEqual(records, [])

    def test_title_only_requires_explicit_matching_direction(self):
        base = {"source_type": "官方网站标题", "view": "中性"}
        self.assertIsNotNone(title_only_rejection({**base, "evidence": "螺纹钢：关注利润变动"}))
        self.assertIsNone(title_only_rejection({**base, "evidence": "螺纹钢：区间震荡"}))
        self.assertIsNotNone(title_only_rejection({
            "source_type": "官方网站标题", "view": "看多", "evidence": "铝：大幅拉升",
        }))
        self.assertIsNone(title_only_rejection({
            "source_type": "官方网站标题", "view": "看空", "evidence": "尿素：震荡承压",
        }))

    def test_filter_title_records_keeps_rejection_audit(self):
        accepted, rejected = filter_title_records([
            {"source_type": "官方网站标题", "view": "中性", "commodity": "锡", "evidence": "锡：横盘震荡"},
            {"source_type": "官方网站标题", "view": "看多", "commodity": "铝", "evidence": "铝：大幅拉升"},
        ])
        self.assertEqual([item["commodity"] for item in accepted], ["锡"])
        self.assertEqual(rejected[0]["commodity"], "铝")
        self.assertIn("没有明确支持", rejected[0]["reason"])

    def test_parse_json_object_with_surrounding_text(self):
        raw = '前文 {"target_date":"2026-08-25","company":"东证期货","records":[]} 后文'
        obj = parse_json_response(raw, "2026-08-25", "东证期货")
        self.assertEqual(obj["company"], "东证期货")
        self.assertEqual(obj["records"], [])

    def test_parse_legacy_array(self):
        obj = parse_json_response("[]", "2026-08-25", "广发期货")
        self.assertEqual(obj["target_date"], "2026-08-25")
        self.assertEqual(obj["company"], "广发期货")

    def test_parse_prefers_last_matching_response_over_prompt_schema(self):
        raw = (
            '用户提示里的示例 {"records":[]}\n'
            '模型回复 {"target_date":"2026-08-25","company":"东证期货",'
            '"records":[{"sector":"黑色系"}]}'
        )
        obj = parse_json_response(raw, "2026-08-25", "东证期货")
        self.assertEqual(obj["records"], [{"sector": "黑色系"}])

    def test_parse_repairs_known_yuanbao_long_json_damage(self):
        raw = (
            '{"target_date":"2026-08-26","company":"广发期货","records":['
            '{"source_url":"httpshttps://example.com/report",confidence":"高"}]}'
        )
        obj = parse_json_response(raw, "2026-08-26", "广发期货")
        self.assertEqual(obj["records"][0]["confidence"], "高")
        self.assertEqual(obj["records"][0]["source_url"], "https://example.com/report")

    def test_attachment_failure_response(self):
        self.assertTrue(attachment_failure_response("我未检测到您提到的附件，请重新上传该文件。"))
        self.assertTrue(attachment_failure_response("当前对话中没有收到任何上传的文件。"))
        self.assertFalse(attachment_failure_response('{"target_date":"2026-08-26","records":[]}'))

    def test_content_limit_failure(self):
        self.assertEqual(
            content_limit_failure("超出字数限制，元宝已阅读54%"),
            "元宝附件超出字数限制，仅读取54%",
        )
        self.assertIsNone(content_limit_failure("元宝已阅读100%"))

    def test_gfqh_only_accepts_daily_review_title(self):
        self.assertTrue(GFQHScraper.is_daily_review_title("广发期货日评20260811"))
        self.assertFalse(GFQHScraper.is_daily_review_title("广发期货期现日报汇总20260811"))
        self.assertFalse(GFQHScraper.is_daily_review_title("20260811广发期货能源化工周报汇总"))

    def test_citic_title_date_cannot_leak_future_report(self):
        self.assertTrue(CITICSFScraper.title_matches_target("日报20260827", "20260827"))
        self.assertTrue(CITICSFScraper.title_matches_target("没有显式日期", "20260827"))
        self.assertFalse(CITICSFScraper.title_matches_target("20260828 Daily Note", "20260827"))

    def test_normalize_rapidocr_legacy_result_orders_lines(self):
        result = [
            ([[0, 20], [10, 20], [10, 30], [0, 30]], ("第二行", 0.8)),
            ([[0, 0], [10, 0], [10, 10], [0, 10]], ("第一行", 0.9)),
        ]
        text, meta = normalize_rapidocr_result(result)
        self.assertEqual(text, "第一行\n第二行")
        self.assertEqual(meta["line_count"], 2)
        self.assertEqual(meta["average_score"], 0.85)

    def test_gfqh_contract_repairs_ocr_commodity_and_sector(self):
        self.assertEqual(canonical_commodity("胡二2", "EG2610"), "乙二醇")
        self.assertEqual(canonical_commodity("铁矿", "12701"), "铁矿石")
        self.assertEqual(canonical_commodity("", "JD2610"), "鸡蛋")
        self.assertEqual(sector_for_commodity("乙二醇"), "能化板块")
        self.assertEqual(normalize_contract("12701", "铁矿石"), "I2701")
        self.assertEqual(normalize_contract("A02610", "氧化铝"), "AO2610")
        self.assertEqual(normalize_contract("LH26011", "生猪"), "LH2601")

    def test_daily_report_aliases_are_canonicalized(self):
        self.assertEqual(canonical_commodity("蛋白粕"), "粕类")
        self.assertEqual(canonical_commodity("化工"), "综合")
        self.assertEqual(canonical_commodity("化工综合"), "综合")
        self.assertEqual(canonical_commodity("螺纹"), "螺纹钢")
        self.assertEqual(canonical_commodity("硅锰"), "锰硅")
        self.assertEqual(canonical_commodity("丙烯"), "丙烯")
        self.assertIsNone(sector_for_commodity("丙烯"))
        self.assertEqual(canonical_commodity("黑色系综合"), "综合")
        self.assertEqual(canonical_commodity("有色金属板块"), "综合")
        self.assertEqual(canonical_commodity("集运指数（欧线）"), "集运欧线")
        self.assertEqual(canonical_commodity("集装箱运价"), "集运欧线")
        self.assertEqual(canonical_commodity("干散货航运运费"), "干散货运价")
        self.assertEqual(canonical_commodity("BDI"), "干散货运价")
        self.assertIn("干散货运价", UNSCORED_COMMODITIES)
        self.assertEqual(canonical_commodity("郑糖"), "白糖")
        self.assertEqual(canonical_commodity("沪铜"), "铜")
        self.assertEqual(canonical_commodity("豆二"), "豆二")
        self.assertEqual(sector_for_commodity("豆二"), "农产品")
        self.assertEqual(canonical_commodity("", "B2701"), "豆二")
        self.assertEqual(canonical_commodity("双胶纸"), "胶版印刷纸")
        self.assertEqual(canonical_commodity("", "OP2701"), "胶版印刷纸")
        self.assertEqual(canonical_commodity("", "SP2701"), "纸浆")
        self.assertEqual(canonical_commodity("", "LG2701"), "原木")
        self.assertEqual(sector_for_commodity("胶版印刷纸"), "农产品")
        self.assertEqual(sector_for_commodity("纸浆"), "能化板块")
        self.assertEqual(sector_for_commodity("原木"), "农产品")
        self.assertEqual(sector_for_commodity("不锈钢"), "黑色系")
        self.assertEqual(sector_for_commodity("玻璃"), "黑色系")
        self.assertEqual(sector_for_commodity("纯碱"), "黑色系")
        self.assertEqual(canonical_commodity("豆一"), "豆一")
        self.assertEqual(canonical_commodity("", "NR2701"), "20号胶")

    def test_empty_crawl_is_not_confused_with_unresolved_access(self):
        self.assertEqual(crawl_result_status([]), {
            "status": "no_source_reports", "count": 0,
        })
        self.assertEqual(crawl_result_status([{
            "source_title": "", "crawl_status": "unresolved_source_access",
        }]), {
            "status": "unresolved_source_access", "count": 0,
        })

    def test_target_date_requires_zero_padded_iso_format(self):
        self.assertEqual(normalize_target_date("2026-09-04"), "2026-09-04")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            normalize_target_date("2026-09-4")

    def test_sector_summary_records_are_not_false_duplicates(self):
        base = {"commodity": "综合", "view_date": "2026-08-27"}
        merged = _merge_by_commodity(
            [{**base, "sector": "能化板块"}],
            [{**base, "sector": "有色金属"}],
        )
        self.assertEqual(len(merged), 2)

    def test_usage_aggregation_includes_retries_and_search_calls(self):
        usage = aggregate_call_usage([
            {"usage": {"prompt_tokens": 100, "completion_tokens": 20,
                        "prompt_tokens_details": {"cached_tokens": 30}}},
            {"usage": {"prompt_tokens": 50, "completion_tokens": 10,
                        "tool_usage": {"web_search_call": 3}}},
        ])
        self.assertEqual(usage, {
            "prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180,
            "cached_tokens": 30, "web_search_calls": 3,
        })

    def test_gfqh_structured_table_supplements_only_explicit_views(self):
        source = {
            "rows": [
                {"scored": True, "commodity": "钢材", "contract": "RB2610",
                 "comment": "表需偏弱", "advice": "短期谨慎"},
                {"scored": True, "commodity": "短纤", "contract": "PF2610",
                 "comment": "加工费压缩", "advice": "区间震荡"},
                {"scored": True, "commodity": "玉米", "contract": "C2701",
                 "comment": "供强需弱", "advice": "谨慎操作"},
            ]
        }
        docs = [{
            "company_hint": "广发期货", "source_date": "2026-08-26",
            "source_text": json.dumps(source, ensure_ascii=False),
            "source_type": "官方网站PNG", "source_name": "广发期货研究资讯",
            "source_title": "广发期货日评20260826",
            "source_url": "https://rd.gfqh.cn/RDInformation/article/8560",
        }]
        records = gfqh_deterministic_table_records(docs, "2026-08-26")
        self.assertEqual(
            [(item["commodity"], item["view"]) for item in records],
            [("钢材", "看空"), ("短纤", "中性")],
        )

    def test_api_enforces_python_commodity_sector(self):
        accepted, rejected = enforce_commodity_sectors([
            {"commodity": "乙二醇", "sector": "农产品"},
            {"commodity": "综合", "sector": "黑色系"},
            {"commodity": "工业硅", "sector": "有色金属"},
        ])
        self.assertEqual(accepted[0]["sector"], "能化板块")
        self.assertEqual(accepted[1]["sector"], "黑色系")
        self.assertEqual(len(rejected), 1)

    def test_judgement_thresholds(self):
        self.assertEqual(judgement(1.25, 1.0), "一致看多")
        self.assertEqual(judgement(1.25, 1.5), "偏多")
        self.assertEqual(judgement(0.0, 3.0), "中性")
        self.assertEqual(judgement(-1.25, 1.0), "一致看空")

    def test_consistency_requires_two_companies(self):
        self.assertEqual(consistency(0.0, 1), "无数据")
        self.assertEqual(consistency(1.0, 4), "高")
        self.assertEqual(consistency(2.0, 4), "中")
        self.assertEqual(consistency(3.0, 4), "低")


if __name__ == "__main__":
    unittest.main()
