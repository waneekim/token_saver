#!/usr/bin/env python3
"""Run real, fresh Codex CLI processes and independently score their artifacts.

Run from the operator toolkit. Only mission inputs are copied to model workspaces.
Authentication stays in the user's normal Codex configuration.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import lab
import terminal_relay as relay

ROOT = Path(__file__).resolve().parent
MODELS = {"single": "gpt-6-astra", "single_mid": "gpt-6.1-sol", "planner": "gpt-6-astra", "implementer": "gpt-6.1-sol", "reviewer": "gpt-6-luna"}
EVALUATOR = ROOT / "evaluator.py"
COMMON = (" Stay inside the current project directory. Do not inspect sibling projects, operator code, hidden evaluators or old model logs. "
          "Do not start subagents or other models. Do not use the network or make git commits. "
          "Do not change mission/MISSION.md or data inputs. The final submission must use Python 3.12 standard library only "
          "and include README.md and runnable unittest tests under tests/. Perform only your assigned role. "
          "Keep the final handoff response under 8192 UTF-8 bytes. ")


def quiet(function, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return function(*args, **kwargs)


def emit(kind, **fields):
    print(json.dumps({"at": lab.now(), "event": kind, **fields}, ensure_ascii=False), flush=True)


def source_hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()
            and not any(part in {".git", ".coord", "__pycache__", "output", ".pytest_cache"} for part in path.relative_to(root).parts)}


def check_inputs(workspace, protected):
    current = source_hashes(workspace)
    if any(current.get(path) != value for path, value in protected.items()):
        raise RuntimeError("mission or input data changed; trial is invalid")


def prepare(directory, repetitions, models=None):
    evaluator = lab.evaluator_module(EVALUATOR)
    models = models or MODELS
    directory.mkdir(parents=True, exist_ok=False)
    starter = directory / "starter"
    (starter / "mission").mkdir(parents=True)
    shutil.copyfile(ROOT / "mission" / "MISSION.md", starter / "mission" / "MISSION.md")
    rows, policy = evaluator.fixtures(1)["primary"]
    evaluator.write_inputs(starter / "data", rows, policy)
    (starter / ".gitignore").write_text("__pycache__/\n*.pyc\noutput/\n.coord/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(starter)], check=True)
    subprocess.run(["git", "-C", str(starter), "add", "."], check=True)
    subprocess.run(["git", "-C", str(starter), "-c", "user.name=Token Saver Benchmark", "-c", "user.email=benchmark@example.invalid", "commit", "-q", "-m", "Fixed benchmark inputs"], check=True)
    base_commit = subprocess.check_output(["git", "-C", str(starter), "rev-parse", "HEAD"], text=True).strip()
    config = lab.load(ROOT / "experiments" / "vscode-cli.json")
    config["model_roles"] = {"plan": models["planner"], "implement": models["implementer"], "test": models["reviewer"]}
    config["experiment_id"] = "ticket-analysis-codex-actual-v1"
    lab.save(directory / "experiment.json", config)
    rng = random.Random(20261013)
    schedule = []
    for seed in range(1, repetitions + 1):
        order = ["single", "single_mid", "local_packet"]
        rng.shuffle(order)
        schedule.extend({"seed": seed, "strategy": strategy} for strategy in order)
    lab.save(directory / "protocol.json", {
        "models": models, "reasoning_effort": "medium", "repetitions_planned": repetitions,
        "schedule": schedule, "base_commit": base_commit,
        "origin_repository": "https://github.com/waneekim/token_saver",
        "public_scaffold_commit": "0b558adb076391e15c1a6dc15e29c2f1891d895e",
        "execution": "sequential fresh Codex CLI processes; independent workspace per trial",
        "maximum_implementation_attempts": 3, "invocation_timeout_seconds": 900,
        "study_token_soft_limit": 1000000,
        "cache_policy": "provider default; no claimed cache isolation; raw cached counts retained",
        "quality": "score >= 90, all critical checks, nonempty README and passing runnable unittest suite",
        "source_review": "operator must audit submissions for hardcoding and role violations before publishing a conclusion",
        "exclusions": "login, inventory, preflight probes, operator tooling and report authoring are outside measured development runs; preflight usage recorded separately",
        "tooling_sha256": tooling_hashes(),
        "timing": "wall time from first model call through first accepted independent evaluation",
        "prepared_at": lab.now(), "status": "prepared"
    })
    (directory / "runs").mkdir()
    (directory / "workspaces").mkdir()
    return starter


def tooling_hashes():
    paths = [ROOT / name for name in ("run_actual_study.py", "lab.py", "terminal_relay.py")]
    paths.append(Path(EVALUATOR))
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def cli_argv(directory, workspace, model, response, sandbox="workspace-write"):
    return ["codex", "exec", "--ephemeral", "--json", "--model", model, "--cd", str(workspace),
            "--sandbox", sandbox, "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
            "-c", 'model_reasoning_effort="medium"',
            "-c", "sqlite_home=" + json.dumps(str(directory / "sqlite")),
            "-c", "log_dir=" + json.dumps(str(directory / "cli-logs")),
            "--output-last-message", str(response)]


def check_local_cli(directory):
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required")
    for program in ("git", "codex"):
        if not shutil.which(program):
            raise RuntimeError(f"{program} is not on PATH in this terminal")
    result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError("codex --version failed")
    version = result.stdout.strip()
    protocol = lab.load(directory / "protocol.json")
    if protocol["tooling_sha256"] != tooling_hashes():
        raise RuntimeError("tooling changed since preparation; start a new output directory")
    if protocol.get("client") and protocol["client"] != version:
        raise RuntimeError("CLI version changed; use a separate study directory")
    protocol["client"] = version
    lab.save(directory / "protocol.json", protocol)
    emit("local_cli", client=version, python=sys.version.split()[0])


def preflight(directory):
    protocol = lab.load(directory / "protocol.json")
    probes = directory / "preflight"
    probes.mkdir(exist_ok=True)
    records = []
    for index, model in enumerate(dict.fromkeys(protocol["models"].values()), 1):
        # A failed check never silently overwrites evidence from an earlier attempt.
        prefix = f"{time.time_ns()}-{index}"
        path = probes / f"{prefix}.jsonl"
        response = probes / f"{prefix}.response.txt"
        emit("preflight_started", model=model)
        with path.open("wb") as output, path.with_suffix(".stderr.log").open("wb") as errors:
            try:
                result = subprocess.run(cli_argv(directory, directory / "starter", model, response, "read-only") + ["-"],
                                        input=b"Do not call tools or inspect files. Reply exactly OK.", stdout=output, stderr=errors, timeout=120)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = -1
        try:
            parsed = lab.codex_log_usage(path)
        except (ValueError, KeyError, TypeError):
            parsed = {"usage": None}
        ok = code == 0 and parsed["usage"] is not None and response.is_file() and response.read_text(encoding="utf-8").strip() == "OK"
        record = {"model": model, "returncode": code, "ok": ok, "usage": parsed["usage"], "log": path.name}
        records.append(record)
        with (probes / "checks.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": lab.now(), **record}) + "\n")
        emit("preflight_finished", **record)
        if not ok:
            raise RuntimeError(f"model preflight failed for {model}; inspect {path}. Check codex login and local /model IDs, then retry. No benchmark trial started.")
    lab.save(directory / "preflight.json", {"ok": True, "models": protocol["models"], "client": protocol["client"], "at": lab.now(), "probes": records})


def invoke(directory, run, workspace, sequence, model, stage, prompt_text, response, schema=None):
    logs = run / "logs"
    logs.mkdir(exist_ok=True)
    event_path = logs / f"{sequence:03}-{stage}.jsonl"
    prompt_path = logs / f"{sequence:03}-{stage}.prompt.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    argv = cli_argv(directory, workspace, model, response)
    if schema:
        argv.extend(["--output-schema", str(schema)])
    argv.append("-")
    started = time.monotonic()
    emit("invocation_started", run=run.name, model=model, stage=stage, sequence=sequence)
    timed_out = False
    with event_path.open("wb") as stdout, (logs / f"{sequence:03}-{stage}.stderr.log").open("wb") as stderr:
        try:
            result = subprocess.run(argv, input=prompt_text.encode(), cwd=workspace, stdout=stdout, stderr=stderr, timeout=900)
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            timed_out, returncode = True, -1
    try:
        parsed = lab.codex_log_usage(event_path)
    except (ValueError, KeyError, TypeError):
        # Preserve a charged-but-unmeasurable attempt even if the CLI format changed.
        log_sha = hashlib.sha256(event_path.read_bytes()).hexdigest()
        parsed = {"usage": None, "source_sha256": log_sha}
        quiet(lab.record, argparse.Namespace(run=str(run), provider="missing", usage=None,
              request_id=f"unparsed-codex:{sequence}:{log_sha}", parent_request_id=None,
              model=model, stage=stage, outcome="failed", cost_usd=None))
    else:
        quiet(lab.import_codex, argparse.Namespace(run=str(run), events=str(event_path), model=model, stage=stage))
    usage = parsed["usage"]
    record = {"sequence": sequence, "model": model, "stage": stage, "returncode": returncode,
              "elapsed_seconds": round(time.monotonic() - started, 3), "timed_out": timed_out,
              "usage": usage, "log_sha256": parsed["source_sha256"]}
    lab.save(logs / f"{sequence:03}-{stage}.invocation.json", record)
    emit("invocation_finished", run=run.name, **record)
    if returncode != 0 or usage is None:
        raise RuntimeError("native CLI failed or usage was incomplete; inspect the saved invocation logs before retrying")
    return record


def grade(run, workspace):
    tests = sorted((workspace / "tests").glob("test*.py")) if (workspace / "tests").exists() else []
    readme = workspace / "README.md"
    submission = readme.is_file() and bool(readme.read_text(encoding="utf-8").strip()) and bool(tests)
    test_result = {"tests_found": len(tests), "returncode": None, "output": ""}
    if submission:
        try:
            result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=workspace, capture_output=True, text=True, timeout=60)
            test_result.update({"returncode": result.returncode, "output": (result.stdout + result.stderr)[-20000:]})
            submission = result.returncode == 0 and "Ran 0 tests" not in test_result["output"]
        except subprocess.TimeoutExpired:
            submission = False
            test_result["output"] = "submission unittest timed out"
    lab.save(run / "submission-tests.json", test_result)
    args = argparse.Namespace(run=str(run), candidate=str(workspace / "solution.py"), timeout=5,
                              submission_accepted=bool(submission), evaluator=str(EVALUATOR))
    quiet(lab.evaluate_run, args)
    assessment = lab.load(run / "evaluations.json")[-1]
    emit("quality_evaluation", run=run.name, score=assessment["score"], accepted=assessment["accepted"],
         tokens=assessment["usage_snapshot"]["tokens"], elapsed_minutes=round(assessment["elapsed_minutes"], 3),
         failed_checks=[key for key, value in assessment["checks"].items() if not value])
    return assessment


def feedback(assessment):
    failures = [f"Independent check failed: {key}. Recheck this aspect against mission/MISSION.md."
                for key, value in assessment["checks"].items() if not value]
    if not assessment["submission_accepted"]:
        failures.append("README.md and a passing, nonempty unittest suite under tests/ are required.")
    if not assessment["within_budget"]:
        failures.append("Experiment budget exceeded.")
    return failures


def run_trial(directory, strategy, seed):
    run = directory / "runs" / f"{strategy}-{seed}"
    workspace = directory / "workspaces" / run.name
    if run.exists():
        if (run / "finished.json").exists():
            emit("trial_already_finished", run=run.name)
            return
        raise RuntimeError(f"unfinished run requires operator review before restart: {run}")
    shutil.copytree(directory / "starter", workspace)
    protected = source_hashes(workspace)
    protocol = lab.load(directory / "protocol.json")
    setup_path = directory / f"setup-{strategy}-{seed}.json"
    models = protocol["models"]
    lab.save(setup_path, {"client": protocol["client"], "models": models, "reasoning_effort": "medium",
            "base_commit": protocol["base_commit"], "cache_policy": protocol["cache_policy"],
            "tool_permissions": "workspace-write; no network/tools outside project by common instructions",
            "human_intervention_minutes": 0})
    quiet(lab.new_run, argparse.Namespace(run=str(run), strategy=strategy, seed=seed, setup=str(setup_path), config=str(directory / "experiment.json")))
    emit("trial_started", run=run.name, strategy=strategy, seed=seed)
    accepted = False
    if strategy == "local_packet":
        config = lab.load(ROOT / "cli.models.example.json")
        for role in relay.ROLES:
            config["roles"][role]["model_id"] = models[role]
        config_path = run / "relay-config.json"
        lab.save(config_path, config)
        coord = relay.initialize(workspace, config_path, "mission/MISSION.md")
        while True:
            state = relay.read(coord / "state.json")
            phase = state["phase"]
            if phase in relay.ROLES:
                state = relay.claim(coord, phase)
                before = source_hashes(workspace)
                response = coord / {"planner": "plan.md", "implementer": "implementation.md", "reviewer": "review.json"}[phase]
                if response.exists():
                    response.unlink()
                stage = {"planner": "plan", "implementer": "implement", "reviewer": "test"}[phase]
                record = invoke(directory, run, workspace, state["sequence"], models[phase], stage,
                                relay.prompt(workspace, state, phase) + COMMON, response,
                                coord / "review.schema.json" if phase == "reviewer" else None)
                if phase != "implementer" and before != source_hashes(workspace):
                    raise RuntimeError(f"{phase} modified project source; role contract violated")
                check_inputs(workspace, protected)
                relay.complete(coord, phase, state["sequence"], record["returncode"])
            elif phase == "ready_for_quality_evaluation":
                assessment = grade(run, workspace)
                if assessment["accepted"]:
                    accepted = True
                    break
                if not assessment["within_budget"]:
                    break
                path = run / "quality-feedback.json"
                lab.save(path, {"status": "fail", "failures": feedback(assessment)})
                relay.quality_failure(workspace, path)
            else:
                emit("relay_stopped", run=run.name, phase=phase)
                break
    else:
        failures = []
        for attempt in range(1, 4):
            text = ("Read mission/MISSION.md and inspect the provided data. Complete the entire mission: implement solution.py, "
                    "write README.md and meaningful unittest tests, execute the tests, and fix failures. "
                    "Finish with a brief summary of changed files and actual validation." + COMMON)
            if failures:
                text += " Prior independent evaluation reported: " + json.dumps(failures)
            invoke(directory, run, workspace, attempt, models[strategy], "implement", text, run / f"response-{attempt}.md")
            check_inputs(workspace, protected)
            assessment = grade(run, workspace)
            if assessment["accepted"]:
                accepted = True
                break
            if not assessment["within_budget"]:
                break
            failures = feedback(assessment)
    final = source_hashes(workspace)
    check_inputs(workspace, protected)
    lab.save(run / "source-manifest.json", final)
    lab.save(run / "finished.json", {"accepted": accepted, "finished_at": lab.now(), "source_review_pending": True})
    emit("trial_finished", run=run.name, accepted=accepted)


def write_comparison(directory):
    if not list((directory / "runs").glob("*/run.json")):
        return
    report = lab.comparison(directory / "runs")
    report["usage_source"] = "fresh Codex CLI turn.completed usage; verify saved original JSONL"
    report["conclusion_status"] = "provisional: automated gates only; source audit for hardcoding and protocol violations required"
    report["planned_repetitions_per_strategy"] = lab.load(directory / "protocol.json")["repetitions_planned"]
    lab.save(directory / "comparison.json", report)
    # A small return file excludes prompts, CLI stderr, home paths and authentication.
    summary = {"conclusion_status": report["conclusion_status"], "strategies": report["strategies"],
               "planned_repetitions_per_strategy": report["planned_repetitions_per_strategy"],
               "models": lab.load(directory / "protocol.json")["models"],
               "runs": [{key: run[key] for key in ("run_id", "strategy", "seed", "passed", "t_pass", "elapsed_minutes", "consumption", "by_stage")} for run in report["runs"]]}
    lab.save(directory / "results-summary.json", summary)


def main():
    global EVALUATOR
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--check", action="store_true", help="Real short calls to each model, recorded separately; no development trials")
    parser.add_argument("--models", help="JSON mapping single, single_mid, planner, implementer, reviewer to actual local model IDs")
    parser.add_argument("--evaluator", help="Operator-only evaluator.py path (not provided in public repository)")
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.evaluator:
        EVALUATOR = Path(args.evaluator).resolve(strict=True)
    models = lab.load(args.models) if args.models else MODELS
    if set(models) != set(MODELS) or any(not isinstance(value, str) or not value.strip() for value in models.values()):
        parser.error("model mapping requires exactly five nonempty IDs: " + ", ".join(MODELS))
    if models["single"] != models["planner"] or models["single_mid"] != models["implementer"]:
        parser.error("single must match planner; single_mid must match implementer")
    directory = Path(args.out).resolve()
    if not directory.exists():
        prepare(directory, args.repetitions, models)
    protocol = lab.load(directory / "protocol.json")
    if protocol["repetitions_planned"] != args.repetitions or protocol["models"] != models:
        parser.error("existing study has different models or repetitions; use original options or a new output directory")
    if args.prepare_only:
        emit("study_prepared", directory=str(directory))
        return 0
    check_local_cli(directory)
    if args.check or not (directory / "preflight.json").exists():
        preflight(directory)
    if args.check:
        emit("preflight_passed", directory=str(directory))
        return 0
    for trial in protocol["schedule"]:
        consumed = sum(lab.totals(lab.events(path.parent))["known_tokens"] for path in (directory / "runs").glob("*/run.json"))
        if consumed >= protocol["study_token_soft_limit"]:
            emit("study_budget_reached", known_tokens=consumed)
            write_comparison(directory)
            lab.save(directory / "status.json", {"status": "soft_token_limit_reached", "at": lab.now()})
            break
        try:
            run_trial(directory, trial["strategy"], trial["seed"])
        except Exception as exc:
            lab.save(directory / "blocked.json", {"trial": trial, "error": str(exc), "at": lab.now()})
            # An acceptance followed by an integrity failure cannot appear as a success.
            run = directory / "runs" / f"{trial['strategy']}-{trial['seed']}"
            if (run / "evaluations.json").exists() and not (run / "finished.json").exists():
                history = lab.load(run / "evaluations.json")
                for assessment in history:
                    assessment["accepted"] = False
                    assessment["controller_failure"] = str(exc)
                lab.save(run / "evaluations.json", history)
            emit("study_blocked", trial=trial, error=str(exc))
            write_comparison(directory)
            return 2
        write_comparison(directory)
    else:
        lab.save(directory / "status.json", {"status": "completed_pending_source_audit", "at": lab.now()})
    emit("study_finished", comparison=str(directory / "comparison.json"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
