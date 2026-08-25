import unittest

from futures_view.finalize import consistency, judgement
from futures_view.llm.browser_llm import parse_json_response


class CoreTests(unittest.TestCase):
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
