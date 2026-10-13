#!/usr/bin/env python3
"""Offline experiment ledger. No model calls, credentials, or third-party packages."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import shutil
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def evaluator_module(path=None):
    path = Path(path or ROOT / "evaluator.py").resolve()
    if not path.is_file():
        raise ValueError("operator evaluator is required; provide --evaluator /private/path/evaluator.py")
    spec = importlib.util.spec_from_file_location("tokenlab_private_evaluator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def normalize_usage(provider, raw):
    if provider == "missing":
        return None
    usage = raw.get("usage", raw)
    if not isinstance(usage, dict):
        raise ValueError("usage must be an object")
    cache_read = cache_write = reasoning = None
    if provider == "openai":
        if "input_tokens" in usage:
            input_total, output_total = usage["input_tokens"], usage["output_tokens"]
            input_details = usage.get("input_tokens_details") or {}
            output_details = usage.get("output_tokens_details") or {}
        else:
            input_total, output_total = usage["prompt_tokens"], usage["completion_tokens"]
            input_details = usage.get("prompt_tokens_details") or {}
            output_details = usage.get("completion_tokens_details") or {}
        cache_read = input_details.get("cached_tokens")
        reasoning = output_details.get("reasoning_tokens")
        if "total_tokens" in usage and integer(usage["total_tokens"], "total_tokens") != input_total + output_total:
            raise ValueError("inconsistent total_tokens")
    elif provider == "anthropic":
        cache_read = integer(usage.get("cache_read_input_tokens", 0), "cache_read")
        cache_write = integer(usage.get("cache_creation_input_tokens", 0), "cache_write")
        input_total = integer(usage["input_tokens"], "input_tokens") + cache_read + cache_write
        output_total = usage["output_tokens"]
    elif provider == "canonical":
        input_total, output_total = usage["input_total"], usage["output_total"]
        cache_read, cache_write, reasoning = (usage.get(key) for key in ("cache_read", "cache_write", "reasoning"))
    else:
        raise ValueError("unknown provider")
    input_total, output_total = integer(input_total, "input_total"), integer(output_total, "output_total")
    for name, value in (("cache_read", cache_read), ("cache_write", cache_write), ("reasoning", reasoning)):
        if value is not None:
            integer(value, name)
    if (cache_read or 0) + (cache_write or 0) > input_total or (reasoning or 0) > output_total:
        raise ValueError("token subset exceeds total")
    return {"input_total": input_total, "output_total": output_total, "total": input_total + output_total,
            "cache_read": cache_read, "cache_write": cache_write, "reasoning": reasoning}


def events(directory):
    path = Path(directory) / "usage.jsonl"
    result = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    ids = [event["request_id"] for event in result]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate request IDs in ledger")
    return result


def totals(records):
    missing = sum(record["usage"] is None for record in records)
    known_tokens = sum(record["usage"]["total"] for record in records if record["usage"] is not None)
    all_costs = bool(records) and all(record.get("cost_usd") is not None for record in records)
    def component(key):
        return sum(record["usage"][key] for record in records) if records and all(record["usage"] is not None and record["usage"].get(key) is not None for record in records) else None
    return {"calls": len(records), "missing_usage_calls": missing, "known_tokens": known_tokens,
            "tokens": known_tokens if records and not missing else None,
            "input_tokens": component("input_total"), "output_tokens": component("output_total"),
            "cache_read_tokens": component("cache_read"), "reasoning_tokens": component("reasoning"),
            "cost_usd": sum(record["cost_usd"] for record in records) if all_costs else None}


def codex_log_usage(path):
    raw_bytes = Path(path).read_bytes()
    entries = [json.loads(line) for line in raw_bytes.decode("utf-8").splitlines() if line.strip()]
    if not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("Codex JSONL events must be objects")
    started = [entry for entry in entries if entry.get("type") == "thread.started"]
    completed = [entry for entry in entries if entry.get("type") == "turn.completed"]
    failed = [entry for entry in entries if entry.get("type") == "turn.failed"]
    if len(started) > 1 or len(completed) > 1:
        raise ValueError("only one fresh codex exec thread per log is supported; cumulative/resumed logs cannot be summed")
    log_sha = hashlib.sha256(raw_bytes).hexdigest()
    thread_id = started[0].get("thread_id") if started else None
    if thread_id is not None and (not isinstance(thread_id, str) or not thread_id):
        raise ValueError("invalid thread_id")
    request_id = "codex-thread:" + thread_id if thread_id else "codex-log:" + log_sha
    usage = None
    outcome = "failed"
    if len(completed) == 1 and not failed and thread_id:
        raw = completed[0]["usage"]
        usage = normalize_usage("canonical", {"input_total": raw["input_tokens"], "output_total": raw["output_tokens"],
                 "cache_read": raw.get("cached_input_tokens"), "cache_write": raw.get("cache_write_input_tokens"),
                 "reasoning": raw.get("reasoning_output_tokens")})
        outcome = "success"
        if usage["total"] == 0:
            # Some versions can emit default zero usage when counters are unavailable.
            usage = None
    return {"request_id": request_id, "usage": usage, "outcome": outcome, "source_sha256": log_sha}


def import_codex(args):
    target = Path(args.run)
    load(target / "run.json")
    item = codex_log_usage(args.events)
    existing = events(target)
    if any(entry["request_id"] == item["request_id"] for entry in existing):
        raise ValueError("Codex thread already imported; resume totals must not be counted again")
    item.update({"recorded_at": now(), "parent_request_id": None, "provider": "codex-cli",
                 "model": args.model, "stage": args.stage, "cost_usd": None,
                 "measurement_unit": "fresh CLI thread (may contain multiple model requests)", "source_path": str(Path(args.events).resolve())})
    with (target / "usage.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps(totals(existing + [item])))


def prepare(args):
    evaluator = evaluator_module()
    target = Path(args.out)
    target.mkdir(parents=True, exist_ok=False)
    rows, policy = evaluator.fixtures(args.seed)["primary"]
    evaluator.write_inputs(target, rows, policy)
    shutil.copyfile(ROOT / "mission" / "MISSION.md", target / "MISSION.md")
    print(json.dumps({"starter": str(target.resolve()), "seed": args.seed}, ensure_ascii=False))


def new_run(args):
    config = load(args.config)
    setup = load(args.setup)
    if args.strategy not in config["strategies"]:
        raise ValueError("strategy not in experiment")
    for key in ("client", "models", "base_commit", "cache_policy", "tool_permissions", "reasoning_effort"):
        if not setup.get(key):
            raise ValueError(f"setup.{key} required")
    if not isinstance(setup["models"], dict):
        raise ValueError("setup.models must be an object")
    cohort = {"config": config, "base_commit": setup["base_commit"], "client": setup["client"],
              "cache_policy": setup["cache_policy"], "tool_permissions": setup["tool_permissions"], "reasoning_effort": setup["reasoning_effort"]}
    target = Path(args.run)
    target.mkdir(parents=True, exist_ok=False)
    save(target / "run.json", {"run_id": target.name, "strategy": args.strategy, "seed": args.seed,
                              "started_at": now(), "setup": setup, "config": config, "cohort": digest(cohort)})
    save(target / "evaluations.json", [])
    (target / "usage.jsonl").touch()
    print(json.dumps({"run": str(target.resolve()), "status": "started"}))


def record(args):
    target = Path(args.run)
    load(target / "run.json")
    existing = events(target)
    if any(item["request_id"] == args.request_id for item in existing):
        raise ValueError("duplicate request_id; record only the final usage once")
    if args.provider != "missing" and not args.usage:
        raise ValueError("--usage required unless provider=missing")
    if args.provider == "missing" and args.usage:
        raise ValueError("missing provider cannot have --usage")
    if args.cost_usd is not None and (not math.isfinite(args.cost_usd) or args.cost_usd < 0):
        raise ValueError("cost must be finite and nonnegative")
    usage = normalize_usage(args.provider, load(args.usage) if args.usage else {})
    item = {"request_id": args.request_id, "parent_request_id": args.parent_request_id,
            "recorded_at": now(), "provider": args.provider, "model": args.model, "stage": args.stage,
            "outcome": args.outcome, "usage": usage, "cost_usd": args.cost_usd}
    with (target / "usage.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps(totals(existing + [item])))


def evaluate_run(args):
    target = Path(args.run)
    run = load(target / "run.json")
    history = load(target / "evaluations.json")
    if any(item["accepted"] for item in history):
        raise ValueError("run has already passed; start a new run for another trial")
    if len(history) >= run["config"]["max_evaluations"]:
        raise ValueError("evaluation budget exhausted")
    records = events(target)
    snapshot = totals(records)
    # The evaluator seed is separate from the public starter seed. Keep this module private.
    evaluator = evaluator_module(getattr(args, "evaluator", None))
    result = evaluator.evaluate(args.candidate, run["seed"] + 1000003, args.timeout)
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(run["started_at"])).total_seconds() / 60
    config = run["config"]
    within_budget = (snapshot["known_tokens"] <= config["max_tokens"] and snapshot["calls"] <= config["max_calls"] and elapsed <= config["max_wall_minutes"])
    quality_passed = result["score"] >= config["quality_threshold"] and result["critical_passed"] and args.submission_accepted
    result.update({"evaluated_at": now(), "usage_snapshot": snapshot, "usage_sha256": digest(records),
                   "elapsed_minutes": elapsed, "within_budget": within_budget, "submission_accepted": args.submission_accepted,
                   "quality_passed": quality_passed, "accepted": quality_passed and within_budget})
    history.append(result)
    save(target / "evaluations.json", history)
    # This feedback intentionally excludes expected outputs and hidden input data.
    print(json.dumps({key: result[key] for key in ("score", "checks", "critical_passed", "quality_passed", "within_budget", "accepted", "usage_snapshot")}, ensure_ascii=False, indent=2))


def summarize_run(directory):
    run = load(directory / "run.json")
    records = events(directory)
    history = load(directory / "evaluations.json")
    first = next((item for item in history if item["accepted"]), None)
    if first:
        prefix = records[:first["usage_snapshot"]["calls"]]
        if digest(prefix) != first["usage_sha256"] or totals(prefix) != first["usage_snapshot"]:
            raise ValueError(f"usage ledger changed after evaluation: {directory}")
        consumption = first["usage_snapshot"]
    else:
        consumption = totals(records)
    by_stage = {}
    selected_records = records[:consumption["calls"]]
    for event in selected_records:
        by_stage.setdefault(event["stage"], []).append(event)
    return {"run_id": run["run_id"], "strategy": run["strategy"], "seed": run["seed"], "cohort": run["cohort"],
            "passed": first is not None, "t_pass": consumption["tokens"] if first else None,
            "elapsed_minutes": first["elapsed_minutes"] if first else None, "consumption": consumption,
            "all_recorded_usage": totals(records), "by_stage": {key: totals(value) for key, value in by_stage.items()},
            "human_intervention_minutes": run["setup"].get("human_intervention_minutes", 0)}


def median(values):
    return statistics.median(values) if values else None


def comparison(directory):
    runs = [summarize_run(path.parent) for path in sorted(Path(directory).glob("*/run.json"))]
    if not runs:
        raise ValueError("no runs found")
    # Do not silently pool different commits, clients, policies, or experimental settings.
    if len({item["cohort"] for item in runs}) != 1:
        raise ValueError("incompatible cohorts; compare identical commits, clients and policies separately")
    paired = {}
    for item in runs:
        key = (item["strategy"], item["seed"])
        if key in paired:
            raise ValueError("duplicate strategy/seed; use distinct repeat seeds")
        paired[key] = item
    summaries = {}
    for strategy in sorted({item["strategy"] for item in runs}):
        group = [item for item in runs if item["strategy"] == strategy]
        passed = [item for item in group if item["passed"]]
        measured = [item for item in passed if item["t_pass"] is not None]
        complete = all(item["consumption"]["tokens"] is not None for item in group)
        savings = []
        for item in group:
            base = paired.get(("single", item["seed"]))
            if strategy != "single" and item["t_pass"] is not None and base and base["t_pass"]:
                savings.append(1 - item["t_pass"] / base["t_pass"])
        summaries[strategy] = {
            "runs": len(group), "passed": len(passed), "pass_rate": len(passed) / len(group),
            "measured_passes": len(measured), "median_t_pass": median([item["t_pass"] for item in measured]),
            "median_completion_minutes": median([item["elapsed_minutes"] for item in passed]),
            "median_cost_usd_to_pass": median([item["consumption"]["cost_usd"] for item in passed if item["consumption"]["cost_usd"] is not None]),
            "paired_passes": len(savings), "median_paired_token_saving": median(savings),
            "spent_tokens_per_success_including_failures": sum(item["consumption"]["tokens"] for item in group) / len(passed) if complete and passed else None,
            "usage_complete_runs": sum(item["consumption"]["tokens"] is not None for item in group),
            "total_known_tokens": sum(item["consumption"]["known_tokens"] for item in group),
        }
    return {"usage_source": "operator-imported; verify against original gateway logs", "strategies": summaries, "runs": runs,
            "interpretation": "탐색 비교입니다. 성공률·계측 누락·짝지은 반복 수를 함께 확인하세요. 전역 최소 토큰 방식이라는 증거는 아닙니다."}


def compare(args):
    report = comparison(args.runs)
    if args.out:
        save(args.out, report)
    print(json.dumps(report["strategies"], ensure_ascii=False, indent=2))


def parser():
    result = argparse.ArgumentParser(description="Fixed-quality coding token benchmark")
    commands = result.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--out", required=True)
    command.add_argument("--seed", type=int, default=1)
    command.set_defaults(func=prepare)
    command = commands.add_parser("new-run")
    command.add_argument("--run", required=True)
    command.add_argument("--strategy", required=True, help="Strategy key from --config")
    command.add_argument("--seed", type=int, required=True)
    command.add_argument("--setup", required=True)
    command.add_argument("--config", default=str(ROOT / "experiment.json"))
    command.set_defaults(func=new_run)
    command = commands.add_parser("record")
    command.add_argument("--run", required=True)
    command.add_argument("--provider", choices=["openai", "anthropic", "canonical", "missing"], required=True)
    command.add_argument("--usage")
    command.add_argument("--request-id", required=True)
    command.add_argument("--parent-request-id")
    command.add_argument("--model", required=True)
    command.add_argument("--stage", choices=["plan", "implement", "test", "review", "route", "summarize", "escalate", "publish"], required=True)
    command.add_argument("--outcome", choices=["success", "failed", "cancelled"], required=True)
    command.add_argument("--cost-usd", type=float)
    command.set_defaults(func=record)
    command = commands.add_parser("import-codex", help="Import one fresh codex exec --json log; do not use resume")
    command.add_argument("--run", required=True)
    command.add_argument("--events", required=True)
    command.add_argument("--model", required=True)
    command.add_argument("--stage", choices=["plan", "implement", "test", "review", "route", "summarize", "escalate", "publish"], required=True)
    command.set_defaults(func=import_codex)
    command = commands.add_parser("evaluate")
    command.add_argument("--run", required=True)
    command.add_argument("--candidate", required=True)
    command.add_argument("--evaluator", help="Private operator evaluator module; not distributed to development agents")
    command.add_argument("--timeout", type=float, default=5)
    command.add_argument("--submission-accepted", action="store_true")
    command.set_defaults(func=evaluate_run)
    command = commands.add_parser("compare")
    command.add_argument("--runs", required=True)
    command.add_argument("--out")
    command.set_defaults(func=compare)
    return result


def main():
    args = parser().parse_args()
    try:
        args.func(args)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
