import argparse
import concurrent.futures
import contextlib
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab
import terminal_relay as relay


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="token saver ")
        self.root = Path(self.temporary.name)
        (self.root / "mission.md").write_text("Implement the fixture task")
        fake = self.root / "fake_cli.py"
        fake.write_text(
            "import json,sys,time\nfrom pathlib import Path\n"
            "response=Path(sys.argv[1]); prompt=sys.stdin.read()\n"
            "value=json.dumps({'status':'pass','failures':[]}) if response.suffix=='.json' else 'Concise fixture packet'\n"
            "response.write_text(value)\n"
            "print(json.dumps({'type':'thread.started','thread_id':str(time.time_ns())}))\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':50,'output_tokens':10,'reasoning_output_tokens':5}}))\n")
        config = {"max_implementation_attempts": 3, "packet_byte_limit": 8192,
                  "roles": {role: {"model_id": "fixture-model", "argv": [sys.executable, str(fake), "{response}", "{model}"]} for role in relay.ROLES}}
        self.config = self.root / "config.json"
        relay.write(self.config, config)
        self.directory = relay.initialize(self.root, self.config, "mission.md")

    def tearDown(self):
        self.temporary.cleanup()

    def quiet_worker(self, role):
        with contextlib.redirect_stdout(io.StringIO()):
            relay.worker(self.root, role, once=True)

    def finish(self, role, status="pass", returncode=0):
        state = relay.claim(self.directory, role)
        filename = {"planner": "plan.md", "implementer": "implementation.md", "reviewer": "review.json"}[role]
        value = json.dumps({"status": status, "failures": [] if status == "pass" else ["fixture failure"]}) if role == "reviewer" else "fixture packet"
        (self.directory / filename).write_text(value)
        return relay.complete(self.directory, role, state["sequence"], returncode)

    def test_idle_worker_does_not_invoke_cli(self):
        self.quiet_worker("reviewer")
        self.assertEqual(list((self.directory / "logs").iterdir()), [])
        self.assertEqual(relay.read(self.directory / "state.json")["sequence"], 0)

    def test_only_one_worker_can_claim(self):
        state = relay.claim(self.directory, "planner")
        self.assertIsNotNone(state)
        self.assertIsNone(relay.claim(self.directory, "planner"))
        self.assertIsNone(relay.claim(self.directory, "implementer"))

    def test_mock_pipeline_ends_at_external_quality_gate(self):
        for role in relay.ROLES:
            self.quiet_worker(role)
        state = relay.read(self.directory / "state.json")
        self.assertEqual(state["phase"], "ready_for_quality_evaluation")
        self.assertEqual(state["implementation_attempts"], 1)
        self.assertEqual(len(state["history"]), 3)
        self.assertEqual(len(list((self.directory / "logs").glob("*.jsonl"))), 3)
        usage = lab.codex_log_usage(self.directory / "logs" / "001-planner.jsonl")
        self.assertEqual(usage["usage"]["total"], 110)

    def test_failures_retry_then_request_redesign(self):
        self.finish("planner")
        for attempt in range(1, 4):
            self.finish("implementer")
            state = self.finish("reviewer", "fail")
            self.assertEqual(state["implementation_attempts"], attempt)
            self.assertEqual(state["phase"], "implementer" if attempt < 3 else "needs_redesign")

    def test_stale_worker_cannot_complete(self):
        state = relay.claim(self.directory, "planner")
        with self.assertRaises(ValueError):
            relay.complete(self.directory, "planner", state["sequence"] + 1, 0)

    def test_nonzero_cli_exit_blocks_handoff(self):
        state = self.finish("planner", returncode=1)
        self.assertEqual(state["phase"], "blocked")
        self.assertIn("CLI exit 1", state["history"][-1]["error"])

    def test_oversized_packet_not_truncated(self):
        state = relay.claim(self.directory, "planner")
        response = self.directory / "plan.md"
        response.write_text("x" * 8193)
        result = relay.complete(self.directory, "planner", state["sequence"], 0)
        self.assertEqual(result["phase"], "blocked")
        self.assertEqual(response.stat().st_size, 8193)

    def test_invalid_review_cannot_pass(self):
        self.finish("planner")
        self.finish("implementer")
        state = relay.claim(self.directory, "reviewer")
        (self.directory / "review.json").write_text(json.dumps({"status": "pass", "failures": ["failed test"]}))
        result = relay.complete(self.directory, "reviewer", state["sequence"], 0)
        self.assertEqual(result["phase"], "blocked")

    def test_concurrent_planner_workers_launch_only_once(self):
        with patch("builtins.print"), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(relay.worker, self.root, "planner", True) for _ in range(2)]
            for future in futures:
                future.result()
        state = relay.read(self.directory / "state.json")
        self.assertEqual(state["sequence"], 1)
        self.assertEqual(state["phase"], "implementer")
        self.assertEqual(len(list((self.directory / "logs").glob("*.jsonl"))), 1)

    def test_independent_quality_failure_continues_same_run(self):
        for role in relay.ROLES:
            self.quiet_worker(role)
        feedback = self.root / "feedback.json"
        relay.write(feedback, {"status": "fail", "failures": ["fixture boundary failure"]})
        state = relay.quality_failure(self.root, feedback)
        self.assertEqual(state["phase"], "implementer")
        for role in ("implementer", "reviewer"):
            self.quiet_worker(role)
        state = relay.read(self.directory / "state.json")
        self.assertEqual(state["phase"], "ready_for_quality_evaluation")
        self.assertEqual(state["sequence"], 5)
        self.assertEqual(len(list((self.directory / "logs").glob("*.jsonl"))), 5)

    def test_quality_feedback_cannot_interrupt_active_work(self):
        relay.claim(self.directory, "planner")
        feedback = self.root / "feedback.json"
        relay.write(feedback, {"status": "fail", "failures": ["fixture"]})
        with self.assertRaises(ValueError):
            relay.quality_failure(self.root, feedback)


class NativeUsageTests(unittest.TestCase):
    def log(self, entries):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "events.jsonl"
        path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        return path

    def test_completed_thread_is_counted_once_with_subsets(self):
        path = self.log([{"type": "thread.started", "thread_id": "fixture-1"},
                         {"type": "item.completed", "item": {"type": "command_execution"}},
                         {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 300, "reasoning_output_tokens": 200}}])
        result = lab.codex_log_usage(path)
        self.assertEqual(result["request_id"], "codex-thread:fixture-1")
        self.assertEqual(result["usage"]["total"], 1300)
        self.assertEqual(result["usage"]["cache_read"], 800)
        self.assertEqual(result["usage"]["reasoning"], 200)

    def test_failed_thread_remains_unmeasured(self):
        path = self.log([{"type": "thread.started", "thread_id": "fixture-2"}, {"type": "turn.failed", "error": {"message": "fixture error"}}])
        result = lab.codex_log_usage(path)
        self.assertIsNone(result["usage"])
        self.assertEqual(result["outcome"], "failed")

    def test_cumulative_multi_turn_log_rejected(self):
        event = {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}
        path = self.log([{"type": "thread.started", "thread_id": "fixture-3"}, event, event])
        with self.assertRaises(ValueError):
            lab.codex_log_usage(path)

    def test_reimport_same_thread_rejected(self):
        path = self.log([{"type": "thread.started", "thread_id": "fixture-4"},
                         {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}])
        run = path.parent / "run"
        run.mkdir()
        lab.save(run / "run.json", {})
        args = argparse.Namespace(run=str(run), events=str(path), model="fixture", stage="plan")
        with contextlib.redirect_stdout(io.StringIO()):
            lab.import_codex(args)
        with self.assertRaises(ValueError):
            lab.import_codex(args)

    def test_missing_thread_identity_cannot_claim_usage(self):
        path = self.log([{"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}])
        self.assertIsNone(lab.codex_log_usage(path)["usage"])

    def test_zero_default_usage_is_not_a_zero_token_success(self):
        path = self.log([{"type": "thread.started", "thread_id": "fixture-zero"},
                         {"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}}])
        self.assertIsNone(lab.codex_log_usage(path)["usage"])


if __name__ == "__main__":
    unittest.main()
