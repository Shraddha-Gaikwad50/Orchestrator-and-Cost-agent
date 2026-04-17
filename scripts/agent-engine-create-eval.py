#!/usr/bin/env python3
"""Prepare and run a lightweight Agent Engine evaluation harness.

Note: Vertex console currently indicates evaluation creation is primarily via SDK/Colab.
This script runs a deterministic prompt suite against an engine and writes a JSON report
that can be used as a baseline and attached to eval workflows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import vertexai
import vertexai.agent_engines as agent_engines


PHASE0_ENV_KEYS = [
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "COST_DATA_SOURCE",
    "BQ_BILLING_PROJECT",
    "BQ_BILLING_DATASET",
    "BQ_BILLING_TABLE",
    "BILLING_BQ_SCHEMA_MODE",
    "BILLING_AGENT_LLM_SQL",
    "BILLING_CONTEXT_ROUTER_ENABLED",
    "BILLING_LLM_PROVIDER",
    "BILLING_LLM_MODEL",
    "BILLING_LLM_GOOGLE_AI_MODEL",
    "BILLING_LLM_MAX_BYTES_BILLED",
    "BILLING_LLM_MAX_LOOKBACK_DAYS",
    "BILLING_LLM_ALLOW_EXPLICIT_CALENDAR_WINDOW",
    "BILLING_LLM_ALLOW_LONG_RANGE",
    "BILLING_DEFAULT_TILL_NOW_SCOPE",
    "BILLING_FULL_HISTORY_START_DATE",
    "ENABLE_VERTEX_ROUTING",
]


def load_cases(cases_path: str | None) -> list[dict]:
    if not cases_path:
        return [
            {"prompt": "List all unique services used till now.", "expected_mode": "answer"},
            {"prompt": "What are the 3 most expensive services till date?", "expected_mode": "answer"},
            {"prompt": "What was total spend in march and april combined till now?", "expected_mode": "answer"},
        ]
    raw = Path(cases_path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise SystemExit("--cases must point to a JSON array")
    return data


def load_profile(profile_path: str | None) -> dict | None:
    if not profile_path:
        return None
    p = Path(profile_path)
    if not p.exists():
        raise SystemExit(f"--profile not found: {profile_path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("--profile must point to a JSON object")
    return data


def resolve_arg_or_profile(
    cli_value: str | None, profile: dict | None, *path: str
) -> str:
    if cli_value:
        return cli_value
    node = profile or {}
    for seg in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(seg)
    if isinstance(node, str):
        return node
    return ""


def env_snapshot() -> dict[str, str]:
    return {k: os.environ.get(k, "") for k in PHASE0_ENV_KEYS}


def git_commit_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
        )
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def enforce_profile_env(profile: dict | None, snap: dict[str, str]) -> None:
    if not profile:
        return
    frozen = profile.get("frozen_env")
    if not isinstance(frozen, dict):
        return
    mismatches: list[str] = []
    for k, expected in frozen.items():
        if expected is None:
            continue
        if snap.get(k, "") != str(expected):
            mismatches.append(
                f"{k}: expected={expected!r} actual={snap.get(k, '')!r}"
            )
    if mismatches:
        msg = "\n".join(mismatches)
        raise SystemExit(
            "Profile environment mismatch. Fix env vars or update profile.\n"
            + msg
        )


def extract_text(event: dict) -> str:
    content = event.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for p in parts:
        if isinstance(p, dict) and p.get("text"):
            out.append(str(p["text"]))
    return "\n".join(out).strip()


def _normalize_text(s: str) -> str:
    return " ".join((s or "").lower().split())


def _contains_any(haystack: str, needles: list[str]) -> bool:
    hs = _normalize_text(haystack)
    return any(_normalize_text(n) in hs for n in needles if str(n).strip())


def infer_mode(response_text: str) -> str:
    t = _normalize_text(response_text)
    if any(k in t for k in ("error", "failed", "unavailable", "does not exist")):
        return "error"
    if any(
        k in t
        for k in (
            "clarify",
            "which time window",
            "last 7 days",
            "this month",
            "full history",
            "please specify",
        )
    ):
        return "clarify"
    return "answer"


def infer_route(response_text: str) -> str:
    t = _normalize_text(response_text)
    if any(k in t for k in ("source=bigquery", "source=postgres", "llm-sql")):
        return "delegate_to_cost_specialist"
    return "direct_reply"


def severity_weight(sev: str | None) -> int:
    s = (sev or "").strip().lower()
    if s == "critical":
        return 5
    if s == "high":
        return 3
    if s == "medium":
        return 2
    return 1


def score_case(case: dict, response_text: str) -> dict:
    failures: list[str] = []
    must_contain = [str(x) for x in case.get("must_contain_any", []) if str(x).strip()]
    must_not = [str(x) for x in case.get("must_not_contain_any", []) if str(x).strip()]

    if must_contain and not _contains_any(response_text, must_contain):
        failures.append(f"must_contain_any failed: expected one of {must_contain}")
    for term in must_not:
        if _contains_any(response_text, [term]):
            failures.append(f"must_not_contain_any failed: found forbidden term '{term}'")

    expected_mode = str(case.get("expected_mode") or "").strip().lower()
    if expected_mode:
        actual_mode = infer_mode(response_text)
        if actual_mode != expected_mode:
            failures.append(
                f"expected_mode mismatch: expected={expected_mode} actual={actual_mode}"
            )

    expected_route = str(case.get("expected_route") or "").strip().lower()
    if expected_route:
        actual_route = infer_route(response_text)
        if actual_route != expected_route:
            failures.append(
                f"expected_route mismatch: expected={expected_route} actual={actual_route}"
            )

    passed = not failures
    sev = str(case.get("severity") or "low")
    weight = severity_weight(sev)
    return {
        "passed": passed,
        "severity": sev,
        "weight": weight,
        "weighted_score": weight if passed else 0,
        "checks_failed": failures,
        "actual_mode": infer_mode(response_text),
        "actual_route": infer_route(response_text),
    }


def evaluate_case(engine: object, case: dict) -> dict | None:
    turns = case.get("turns")
    prompt = str(case.get("prompt") or "").strip()
    if isinstance(turns, list) and turns:
        prepared_turns = [
            {"role": str(t.get("role", "user")), "text": str(t.get("text", "")).strip()}
            for t in turns
            if str(t.get("text", "")).strip()
        ]
        if not prepared_turns:
            return None
    elif prompt:
        prepared_turns = [{"role": "user", "text": prompt}]
    else:
        return None

    user_id = f"eval-{uuid.uuid4().hex[:8]}"
    sess = engine.create_session(user_id=user_id)
    session_id = sess.get("id")
    if not session_id:
        raise SystemExit("create_session failed for eval run")

    per_turn: list[dict] = []
    final_response = ""
    for t in prepared_turns:
        if t["role"] != "user":
            # Agent Engine stream_query is user-turn oriented; skip non-user prompt rows.
            continue
        chunks: list[str] = []
        for ev in engine.stream_query(
            message=t["text"], user_id=user_id, session_id=session_id
        ):
            text = extract_text(ev)
            if text:
                chunks.append(text)
        response = "\n".join(chunks).strip()
        per_turn.append({"user_text": t["text"], "response": response})
        final_response = response or final_response

    scored = score_case(case, final_response)
    return {
        "case_id": case.get("case_id"),
        "category": case.get("category"),
        "severity": case.get("severity", "low"),
        "expected_mode": case.get("expected_mode"),
        "expected_route": case.get("expected_route"),
        "must_contain_any": case.get("must_contain_any", []),
        "must_not_contain_any": case.get("must_not_contain_any", []),
        "user_id": user_id,
        "session_id": session_id,
        "turns": prepared_turns,
        "responses_by_turn": per_turn,
        "response": final_response,
        "scoring": scored,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", default="", help="projects/.../reasoningEngines/ID")
    parser.add_argument("--project", default="", help="GCP project id")
    parser.add_argument("--location", default="", help="GCP location")
    parser.add_argument("--out", default="logs/agent-engine-eval-report.json")
    parser.add_argument(
        "--cases",
        default="scripts/evals/hallucination_guardrail_cases.json",
        help="Path to JSON eval cases",
    )
    parser.add_argument(
        "--profile",
        default="scripts/evals/phase0_eval_profile.json",
        help="Phase-0 run profile JSON. Set empty to disable profile loading.",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Optional run id; generated automatically when omitted.",
    )
    parser.add_argument(
        "--no-score",
        action="store_true",
        help="Disable phase-2 scoring and only record raw responses.",
    )
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=None,
        help="Fail (exit 2) if summary.pass_rate is below this threshold (0..1).",
    )
    parser.add_argument(
        "--min-weighted-pass-rate",
        type=float,
        default=None,
        help="Fail (exit 2) if summary.weighted_pass_rate is below this threshold (0..1).",
    )
    parser.add_argument(
        "--max-critical-failures",
        type=int,
        default=None,
        help="Fail (exit 2) if number of failed critical cases exceeds this value.",
    )
    parser.add_argument(
        "--thresholds-file",
        default="",
        help="Optional JSON file with threshold overrides (min_pass_rate, min_weighted_pass_rate, max_critical_failures).",
    )
    args = parser.parse_args()

    profile = load_profile(args.profile.strip() or None)
    resource = resolve_arg_or_profile(args.resource.strip(), profile, "targets", "resource")
    project = resolve_arg_or_profile(
        args.project.strip() or os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        profile,
        "project",
    )
    location = resolve_arg_or_profile(
        args.location.strip() or os.environ.get("GOOGLE_CLOUD_LOCATION", ""),
        profile,
        "location",
    )
    if not resource:
        raise SystemExit("Set --resource or provide profile.targets.resource.")
    if not project:
        raise SystemExit("Set --project/GOOGLE_CLOUD_PROJECT or provide profile.project.")
    if not location:
        location = "us-central1"

    cases = load_cases(args.cases)
    snap = env_snapshot()
    enforce_profile_env(profile, snap)
    run_id = args.run_id.strip() or f"eval-{uuid.uuid4().hex[:12]}"

    vertexai.init(project=project, location=location)
    engine = agent_engines.get(resource)

    rows: list[dict] = []
    for case in cases:
        rec = evaluate_case(engine, case)
        if not rec:
            continue
        if args.no_score:
            rec["scoring"] = None
        rows.append(rec)

    summary = {
        "total_cases": len(rows),
        "scored_cases": 0,
        "passed_cases": 0,
        "failed_cases": 0,
        "weighted_total": 0,
        "weighted_passed": 0,
        "pass_rate": 0.0,
        "weighted_pass_rate": 0.0,
        "critical_failures": 0,
    }
    for r in rows:
        sc = r.get("scoring")
        if not isinstance(sc, dict):
            continue
        summary["scored_cases"] += 1
        summary["weighted_total"] += int(sc.get("weight", 0))
        summary["weighted_passed"] += int(sc.get("weighted_score", 0))
        if sc.get("passed"):
            summary["passed_cases"] += 1
        else:
            summary["failed_cases"] += 1
            if str(r.get("severity", "")).lower() == "critical":
                summary["critical_failures"] += 1
    if summary["scored_cases"] > 0:
        summary["pass_rate"] = round(
            summary["passed_cases"] / summary["scored_cases"], 4
        )
    if summary["weighted_total"] > 0:
        summary["weighted_pass_rate"] = round(
            summary["weighted_passed"] / summary["weighted_total"], 4
        )

    thresholds: dict[str, float | int] = {}
    if args.thresholds_file.strip():
        tf = Path(args.thresholds_file.strip())
        if not tf.exists():
            raise SystemExit(f"--thresholds-file not found: {tf}")
        td = json.loads(tf.read_text(encoding="utf-8"))
        if not isinstance(td, dict):
            raise SystemExit("--thresholds-file must be a JSON object")
        thresholds = td
    min_pass_rate = (
        args.min_pass_rate
        if args.min_pass_rate is not None
        else thresholds.get("min_pass_rate")
    )
    min_weighted = (
        args.min_weighted_pass_rate
        if args.min_weighted_pass_rate is not None
        else thresholds.get("min_weighted_pass_rate")
    )
    max_critical = (
        args.max_critical_failures
        if args.max_critical_failures is not None
        else thresholds.get("max_critical_failures")
    )
    gate_failures: list[str] = []
    if min_pass_rate is not None and summary["pass_rate"] < float(min_pass_rate):
        gate_failures.append(
            f"pass_rate {summary['pass_rate']:.4f} < min_pass_rate {float(min_pass_rate):.4f}"
        )
    if min_weighted is not None and summary["weighted_pass_rate"] < float(min_weighted):
        gate_failures.append(
            "weighted_pass_rate "
            f"{summary['weighted_pass_rate']:.4f} < min_weighted_pass_rate {float(min_weighted):.4f}"
        )
    if max_critical is not None and summary["critical_failures"] > int(max_critical):
        gate_failures.append(
            f"critical_failures {summary['critical_failures']} > max_critical_failures {int(max_critical)}"
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": project,
        "location": location,
        "resource": resource,
        "git_commit_sha": git_commit_sha(),
        "profile_name": (profile or {}).get("profile_name", ""),
        "session_policy": (profile or {}).get("session_policy", {}),
        "data_window_policy": (profile or {}).get("data_window_policy", {}),
        "env_snapshot": snap,
        "phase": "phase2",
        "scoring_enabled": not args.no_score,
        "gating": {
            "min_pass_rate": min_pass_rate,
            "min_weighted_pass_rate": min_weighted,
            "max_critical_failures": max_critical,
            "passed": len(gate_failures) == 0,
            "fail_reasons": gate_failures,
        },
        "summary": summary,
        "cases": rows,
        "note": (
            "Phase-2 evaluator: captures prompts/responses/sessions and applies lightweight rule-based scoring "
            "(contains/not-contains, expected_mode, expected_route) with severity-weighted summary."
        ),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote evaluation report: {out}")
    print(
        "Summary: "
        f"pass_rate={summary['pass_rate']:.4f}, "
        f"weighted_pass_rate={summary['weighted_pass_rate']:.4f}, "
        f"critical_failures={summary['critical_failures']}"
    )
    if gate_failures:
        print("Gating FAILED:")
        for reason in gate_failures:
            print(f" - {reason}")
        sys.exit(2)
    print("Gating PASSED.")


if __name__ == "__main__":
    main()
