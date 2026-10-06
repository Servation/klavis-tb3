#!/usr/bin/env python3
"""Summarize the evidence trials for TRIALS.md: one row per trial with its
reward, exception, duration, token use and the TB3 trial-analysis verdicts.

  devtools/summarize_trials.py [jobs-dir] [analysis-dir] [task-commit]

Reads harbor's result.json per trial and analysis.json written by
devtools/analyze_trials.sh. Prints Markdown tables and writes trimmed,
secret-free copies of both files to results/<job>/.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JOBS = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/tb3-local/jobs")
ANALYSIS = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/tb3-local/analysis")
COMMIT = sys.argv[3] if len(sys.argv) > 3 else "5666ac5"
ICONS = {"pass": "🟢", "fail": "🔴", "not_applicable": "⚪"}
CRITERIA = ["task_specification", "reward_hacking", "difficulty_crux", "near_miss", "refusals", "low_timeout"]


def minutes(result: dict) -> float:
    start = datetime.datetime.fromisoformat(result["started_at"].replace("Z", "+00:00"))
    end = datetime.datetime.fromisoformat(result["finished_at"].replace("Z", "+00:00"))
    return (end - start).total_seconds() / 60


def main() -> None:
    rows = []
    for path in sorted(glob.glob(f"{JOBS}/*-{COMMIT}-*/crash-safe-ledger__*/result.json")):
        job = path.split("/")[-3]
        result = json.load(open(path))
        agent_result = result.get("agent_result") or {}
        exc = result.get("exception_info") or {}
        reward = (result.get("verifier_result") or {}).get("rewards", {}).get("reward")
        analysis_path = f"{ANALYSIS}/analysis-{job}/analysis.json"
        verdicts, summary = {}, ""
        if os.path.exists(analysis_path):
            analysis = json.load(open(analysis_path))["results"][0]
            verdicts = {k: v["outcome"] for k, v in analysis["checks"].items()}
            summary = analysis["summary"]
        rows.append((job, reward, exc.get("exception_type"), minutes(result), agent_result, verdicts, summary))
        out = REPO / "results" / job
        out.mkdir(parents=True, exist_ok=True)
        trimmed = {
            "job": job,
            "task_commit": COMMIT,
            "task_checksum": result.get("task_checksum"),
            "agent": (result.get("agent_info") or {}).get("name"),
            "agent_version": (result.get("agent_info") or {}).get("version"),
            "model": ((result.get("agent_info") or {}).get("model_info") or {}).get("name"),
            "reward": reward,
            "exception_type": exc.get("exception_type"),
            "started_at": result["started_at"],
            "finished_at": result["finished_at"],
            "minutes": round(minutes(result), 1),
            "n_input_tokens": agent_result.get("n_input_tokens"),
            "n_output_tokens": agent_result.get("n_output_tokens"),
            "cost_usd": agent_result.get("cost_usd"),
        }
        (out / "result.json").write_text(json.dumps(trimmed, indent=2) + "\n")
        if verdicts:
            (out / "analysis.json").write_text(json.dumps(json.load(open(analysis_path)), indent=2, ensure_ascii=False) + "\n")
    print("| Trial | Reward | Exception | Minutes | Output tokens | " + " | ".join(CRITERIA) + " |")
    print("|---|---|---|---|---|" + "---|" * len(CRITERIA))
    for job, reward, exc, mins, agent_result, verdicts, _ in rows:
        icon = "⚠️" if exc else ("✅" if reward == 1.0 else "❌")
        cells = [ICONS.get(verdicts.get(c, ""), "-") for c in CRITERIA]
        print(f"| `{job}` | {icon} {reward} | {exc or '-'} | {mins:.0f} | {agent_result.get('n_output_tokens') or '-'} | " + " | ".join(cells) + " |")
    print()
    for job, *_, verdicts, summary in rows:
        print(f"### {job}\n\n{summary}\n")


if __name__ == "__main__":
    main()
