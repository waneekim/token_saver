import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab

class UsageTests(unittest.TestCase):
    def test_openai_cache_and_reasoning_not_double_counted(self):
        usage = lab.normalize_usage("openai", {"input_tokens": 1000, "output_tokens": 300,
                    "input_tokens_details": {"cached_tokens": 800},
                    "output_tokens_details": {"reasoning_tokens": 200}})
        self.assertEqual(usage["total"], 1300)
        self.assertEqual(usage["cache_read"], 800)
        self.assertEqual(usage["reasoning"], 200)

    def test_chat_completions_format(self):
        usage = lab.normalize_usage("openai", {"usage": {"prompt_tokens": 100, "completion_tokens": 30,
                          "total_tokens": 130, "prompt_tokens_details": {"cached_tokens": 50}}})
        self.assertEqual(usage["total"], 130)

    def test_anthropic_input_components_are_added(self):
        usage = lab.normalize_usage("anthropic", {"input_tokens": 100, "output_tokens": 50,
                         "cache_read_input_tokens": 700, "cache_creation_input_tokens": 200})
        self.assertEqual(usage["input_total"], 1000)
        self.assertEqual(usage["total"], 1050)

    def test_canonical_includes_subsets(self):
        usage = lab.normalize_usage("canonical", {"input_total": 100, "output_total": 10, "cache_read": 50})
        self.assertEqual(usage["total"], 110)

    def test_invalid_counts_rejected(self):
        for value in (-1, 2.5, True, "100"):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                lab.normalize_usage("canonical", {"input_total": value, "output_total": 2})

    def test_inconsistent_subset_rejected(self):
        with self.assertRaises(ValueError):
            lab.normalize_usage("canonical", {"input_total": 10, "output_total": 2, "reasoning": 3})

    def test_inconsistent_openai_total_rejected(self):
        with self.assertRaises(ValueError):
            lab.normalize_usage("openai", {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 13})

    def test_missing_calls_prevent_exact_total(self):
        records = [{"usage": {"total": 10}, "cost_usd": 0.1}, {"usage": None, "cost_usd": None}]
        result = lab.totals(records)
        self.assertIsNone(result["tokens"])
        self.assertIsNone(result["cost_usd"])
        self.assertEqual(result["known_tokens"], 10)
        self.assertEqual(result["missing_usage_calls"], 1)

    def test_empty_ledger_is_not_zero_token_success(self):
        self.assertIsNone(lab.totals([])["tokens"])
