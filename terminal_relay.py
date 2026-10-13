#!/usr/bin/env python3
"""Sequential native CLI handoff. Waiting workers do not call an LLM."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROLES = ("planner", "implementer", "reviewer")
SCHEMA = {"type": "object", "properties": {"status": {"type": "string", "enum": ["pass", "fail", "blocked"]},
          "failures": {"type": "array", "items": {"type": "string"}}}, "required": ["status", "failures"], "additionalProperties": False}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


@contextlib.contextmanager
def locked(directory):
    # Portable cooperative lock. Never automatically steal an active/stale lock.
    lock = directory / "lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError("coordination lock is busy; retry, or inspect interrupted workers")
    try:
        yield
    finally:
        lock.rmdir()


def initialize(root, config_path, mission):
    root = Path(root).resolve(strict=True)
    config = read(Path(config_path))
    if isinstance(config.get("max_implementation_attempts"), bool) or not isinstance(config.get("max_implementation_attempts"), int) or config["max_implementation_attempts"] < 1:
        raise ValueError("max_implementation_attempts must be a positive integer")
    if isinstance(config.get("packet_byte_limit"), bool) or not isinstance(config.get("packet_byte_limit"), int) or config["packet_byte_limit"] < 256:
        raise ValueError("packet_byte_limit must be an integer >= 256")
    for role in ROLES:
        entry = config["roles"][role]
        if not entry["model_id"] or not isinstance(entry["argv"], list) or not entry["argv"] or not all(isinstance(value, str) for value in entry["argv"]):
            raise ValueError(f"invalid CLI configuration: {role}")
    mission_path = (root / mission).resolve(strict=True)
    mission_path.relative_to(root)
    directory = root / ".coord"
    directory.mkdir(exist_ok=False)
    (directory / "logs").mkdir()
    write(directory / "config.json", config)
    write(directory / "review.schema.json", SCHEMA)
    write(directory / "state.json", {"phase": "planner", "active": None, "implementation_attempts": 0,
          "sequence": 0, "mission": str(mission_path.relative_to(root)), "history": []})
    return directory


def claim(directory, role):
    with locked(directory):
        state = read(directory / "state.json")
        if state["phase"] != role or state["active"] is not None:
            return None
        state["sequence"] += 1
        if role == "implementer":
            state["implementation_attempts"] += 1
        state["active"] = {"role": role, "pid": os.getpid(), "sequence": state["sequence"]}
        write(directory / "state.json", state)
        return state


def prompt(root, state, role):
    shared = (f"Work in {root}. Your role is {role}. This is a sequential handoff; other workers are idle. "
              "Do not start other agents or models. Do not edit the coordination script, config, state, schema or logs. "
              "Do not poll files; the local controller handles waiting. Do not perform commits or remote operations. ")
    if role == "planner":
        return shared + (f"Read {state['mission']} and inspect only necessary project files. Do not edit source code. "
                         "Return a concise implementation packet as your final response: goal, relevant files, allowed changes, interfaces/invariants, acceptance conditions, test commands, unresolved questions. "
                         "Reference existing requirements by path instead of copying them. Do not write a full implementation or a lengthy transcript.")
    if role == "implementer":
        failed = " Read .coord/review.json for the latest failures." if state["implementation_attempts"] > 1 else ""
        return shared + ("Read .coord/plan.md first. Read relevant files and referenced requirements only as needed. "
                         "Implement and run the necessary tests. You are the only source-code writer. Do not read model logs or conversation histories." + failed +
                         " Return a concise final response: changed files, actual test commands and results, unresolved issues. Do not repeat the plan or full code.")
    return shared + ("Read .coord/plan.md and .coord/implementation.md, then inspect changed code and run acceptance tests. "
                     "Do not modify source code; keep test-generated files outside source paths. Do not read model logs or old conversations. "
                     "Return only a JSON object with status ('pass', 'fail', or 'blocked') and failures (specific reproducible problems as strings). "
                     "Base the decision on actual test results. A local pass is a request for independent quality evaluation, not benchmark completion.")


def complete(directory, role, sequence, returncode):
    with locked(directory):
        state = read(directory / "state.json")
        active = state["active"]
        if not active or active["role"] != role or active["sequence"] != sequence:
            raise ValueError("stale worker cannot complete another invocation")
        config = read(directory / "config.json")
        response = directory / {"planner": "plan.md", "implementer": "implementation.md", "reviewer": "review.json"}[role]
        phase, error = "blocked", None
        try:
            if returncode != 0:
                raise ValueError(f"CLI exit {returncode}; inspect logs")
            if not response.is_file() or not response.read_text(encoding="utf-8").strip():
                raise ValueError("missing final response")
            if response.stat().st_size > config["packet_byte_limit"]:
                raise ValueError("handoff packet exceeds byte limit; do not silently truncate requirements")
            if role == "planner":
                phase = "implementer"
            elif role == "implementer":
                phase = "reviewer"
            else:
                report = read(response)
                if set(report) != {"status", "failures"} or report["status"] not in ("pass", "fail", "blocked") or not isinstance(report["failures"], list) or not all(isinstance(item, str) for item in report["failures"]):
                    raise ValueError("invalid reviewer response")
                if report["status"] == "pass":
                    if report["failures"]:
                        raise ValueError("pass cannot include failures")
                    phase = "ready_for_quality_evaluation"
                elif report["status"] == "fail":
                    if not report["failures"]:
                        raise ValueError("fail requires reproducible failures")
                    phase = "implementer" if state["implementation_attempts"] < config["max_implementation_attempts"] else "needs_redesign"
                else:
                    phase = "blocked"
        except (ValueError, OSError, TypeError) as exc:
            error = str(exc)
        state["history"].append({"sequence": sequence, "role": role, "returncode": returncode, "next_phase": phase,
                                 "response_bytes": response.stat().st_size if response.exists() else None, "error": error})
        state["active"], state["phase"] = None, phase
        write(directory / "state.json", state)
        return state


def quality_failure(root, feedback_path):
    directory = Path(root).resolve(strict=True) / ".coord"
    feedback = read(Path(feedback_path))
    if not isinstance(feedback, dict) or set(feedback) != {"status", "failures"} or feedback["status"] != "fail" or not isinstance(feedback["failures"], list) or not feedback["failures"] or not all(isinstance(item, str) and item.strip() for item in feedback["failures"]):
        raise ValueError("independent quality feedback requires status=fail and nonempty failure strings")
    with locked(directory):
        state = read(directory / "state.json")
        if state["phase"] != "ready_for_quality_evaluation" or state["active"] is not None:
            raise ValueError("only an idle submission awaiting independent evaluation can receive feedback")
        config = read(directory / "config.json")
        write(directory / "review.json", feedback)
        state["phase"] = "implementer" if state["implementation_attempts"] < config["max_implementation_attempts"] else "needs_redesign"
        state["history"].append({"kind": "independent_quality_failure", "failures": feedback["failures"], "next_phase": state["phase"]})
        write(directory / "state.json", state)
        return state


def worker(root, role, once=False):
    root = Path(root).resolve(strict=True)
    directory = root / ".coord"
    config = read(directory / "config.json")
    entry = config["roles"][role]
    if entry["model_id"].startswith("REPLACE_"):
        raise ValueError("configure a real CLI model ID before starting workers")
    while True:
        state = read(directory / "state.json")
        if state["phase"] not in ROLES:
            print(f"{role}: {state['phase']}", flush=True)
            return
        try:
            claimed = claim(directory, role)
        except ValueError:
            claimed = None
        if claimed is None:
            if once:
                print(f"{role}: idle; no model call", flush=True)
                return
            time.sleep(0.5)
            continue
        state = claimed
        sequence = state["sequence"]
        response = directory / {"planner": "plan.md", "implementer": "implementation.md", "reviewer": "review.json"}[role]
        values = {"root": str(root), "model": entry["model_id"], "response": str(response), "schema": str(directory / "review.schema.json")}
        result_code = -1
        try:
            argv = [item.format(**values) for item in entry["argv"]]
            if response.exists():
                response.unlink()  # Do not accept a stale result if this invocation fails.
            print(f"{role}: starting invocation {sequence} with {entry['model_id']}", flush=True)
            with (directory / "logs" / f"{sequence:03}-{role}.jsonl").open("wb") as output, (directory / "logs" / f"{sequence:03}-{role}.stderr.log").open("wb") as errors:
                result = subprocess.run(argv, input=prompt(root, state, role).encode(), cwd=root, stdout=output, stderr=errors)
                result_code = result.returncode
        finally:
            completed = complete(directory, role, sequence, result_code)
            print(f"{role}: next phase = {completed['phase']}", flush=True)
        if once:
            return


def main():
    parser = argparse.ArgumentParser(description="Native CLI file handoff; no gateway")
    parser.add_argument("--root", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("init")
    command.add_argument("--config", default="cli.models.json")
    command.add_argument("--mission", default="mission/MISSION.md")
    command = commands.add_parser("worker")
    command.add_argument("--role", choices=ROLES, required=True)
    command.add_argument("--once", action="store_true")
    command = commands.add_parser("quality-feedback", help="Return independent quality failures to the implementation worker")
    command.add_argument("--file", required=True)
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "init":
            print(initialize(args.root, args.config, args.mission))
        elif args.command == "status":
            print(json.dumps(read(Path(args.root) / ".coord" / "state.json"), ensure_ascii=False, indent=2))
        elif args.command == "quality-feedback":
            print(json.dumps(quality_failure(args.root, args.file), ensure_ascii=False, indent=2))
        else:
            worker(args.root, args.role, args.once)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
