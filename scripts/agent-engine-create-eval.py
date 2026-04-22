#!/usr/bin/env python3
"""Create Agent Engine eval baselines and optional Vertex evaluation runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Same prefix as vertex_agents/cost_metrics_agent/cost_payload_contract.COST_PAYLOAD_PREFIX
COST_PAYLOAD_PREFIX = "COST_PAYLOAD_JSON:\n"

import pandas as pd
import vertexai
import vertexai.agent_engines as agent_engines
from google.genai import types as genai_types
from vertexai import Client, types


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


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def validate_cases(cases: list[dict]) -> None:
    if not cases:
        raise SystemExit("No eval cases found.")
    for i, case in enumerate(cases):
        case_id = str(case.get("id") or f"case-{i+1}")
        turns = case_turns(case)
        if not turns:
            raise SystemExit(f"{case_id}: each case must define non-empty 'prompt' or 'turns'.")
        expected_mode = str(case.get("expected_mode") or "").strip()
        if expected_mode and expected_mode not in {"clarify", "answer", "error"}:
            raise SystemExit(f"{case_id}: expected_mode must be one of clarify|answer|error.")
        priority = str(case.get("priority") or "").strip()
        if priority and priority not in {"P0", "P1", "P2"}:
            raise SystemExit(f"{case_id}: priority must be one of P0|P1|P2.")


def _extract_text_from_part(p: dict) -> str:
    if p.get("text"):
        return str(p["text"])
    for fr_key in ("function_response", "functionResponse", "tool_response", "ToolResponse"):
        fr = p.get(fr_key)
        if isinstance(fr, dict):
            inner = fr.get("response")
            if inner is not None:
                if isinstance(inner, (dict, list)):
                    return json.dumps(inner, ensure_ascii=False)
                s = str(inner)
                if s.strip().startswith("{") and "response_type" in s:
                    return s
                return s
            return json.dumps(fr, ensure_ascii=False)
        if isinstance(fr, str):
            return fr
    return ""


def extract_text(event: dict) -> str:
    content = event.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for p in parts:
        if isinstance(p, dict):
            chunk = _extract_text_from_part(p)
            if chunk:
                out.append(chunk)
    return "\n".join(out).strip()


def _collect_structured_payloads(value: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        rt = value.get("response_type")
        if isinstance(rt, str) and rt.strip():
            out.append(value)
        for nested in value.values():
            _collect_structured_payloads(nested, out)
        return
    if isinstance(value, list):
        for nested in value:
            _collect_structured_payloads(nested, out)


def _structured_from_text(text: str) -> list[dict[str, Any]]:
    stripped = (text or "").strip()
    if not stripped:
        return []
    if stripped.startswith("COST_PAYLOAD_JSON:"):
        if stripped.startswith(COST_PAYLOAD_PREFIX):
            body = stripped[len(COST_PAYLOAD_PREFIX) :].strip()
        else:
            body = stripped.split("\n", 1)[-1] if "\n" in stripped else stripped.replace("COST_PAYLOAD_JSON:", "").strip()
        try:
            parsed = json.loads(body)
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        _collect_structured_payloads(parsed, out)
        return out
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return []
    try:
        parsed = json.loads(stripped)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    _collect_structured_payloads(parsed, out)
    return out


def extract_structured_payloads(event: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    _collect_structured_payloads(event, out)
    content = event.get("content")
    if not isinstance(content, dict):
        return out
    parts = content.get("parts")
    if not isinstance(parts, list):
        return out
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("text"):
            out.extend(_structured_from_text(str(part.get("text"))))
        for key in ("function_response", "functionResponse", "tool_response", "ToolResponse"):
            if key in part:
                _collect_structured_payloads(part[key], out)
    return out


def _prefer_structured(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    for obj in reversed(candidates):
        rt = str(obj.get("response_type") or "").lower()
        if rt in ("clarification", "error"):
            return obj
    return candidates[-1]


def parse_structured_from_response_text(response: str) -> dict[str, Any] | None:
    t = (response or "").strip()
    if t.startswith("COST_PAYLOAD_JSON:") or COST_PAYLOAD_PREFIX in t:
        if t.startswith(COST_PAYLOAD_PREFIX):
            body = t[len(COST_PAYLOAD_PREFIX) :].strip()
        else:
            body = t.split("\n", 1)[-1] if "\n" in t else t.replace("COST_PAYLOAD_JSON:", "").strip()
        try:
            parsed = json.loads(body)
        except Exception:
            return None
        if isinstance(parsed, dict) and str(parsed.get("response_type") or "").strip():
            return parsed
    return None


def _scoring_haystacks(response: str, structured: dict[str, Any] | None) -> list[str]:
    stacks = [response.lower()]
    if not structured:
        p = parse_structured_from_response_text(response)
        if p:
            structured = p
    if structured:
        stacks.append(json.dumps(structured, ensure_ascii=False, sort_keys=True).lower())

        def walk(obj: Any) -> None:
            if isinstance(obj, dict):
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)
            elif isinstance(obj, bool):
                stacks.append("true" if obj else "false")
            elif isinstance(obj, (str, int, float)):
                stacks.append(str(obj).lower())

        walk(structured)
    return stacks


def case_turns(case: dict) -> list[str]:
    turns = case.get("turns")
    if isinstance(turns, list):
        return [str(t).strip() for t in turns if str(t).strip()]
    prompt = str(case.get("prompt") or "").strip()
    return [prompt] if prompt else []


def case_prompt_for_inference(case: dict) -> str:
    turns = case_turns(case)
    if not turns:
        return ""
    if len(turns) == 1:
        return turns[0]
    transcript = ["multi-turn conversation"]
    for t in turns:
        transcript.append(f"USER: {t}")
    return "\n".join(transcript)


def parse_labels(pairs: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"Invalid --label '{pair}'. Use key=value format.")
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise SystemExit(f"Invalid --label '{pair}'. Empty key/value is not allowed.")
        labels[key] = value
    return labels


def infer_mode(response: str, structured: dict[str, Any] | None = None) -> str:
    if structured:
        rt = str(structured.get("response_type") or "").strip().lower()
        if rt == "clarification":
            return "clarify"
        if rt == "error":
            return "error"
        if rt in {"result", "text"}:
            return "answer"
    lower = response.lower()
    if not lower.strip():
        return "error"
    if "clarification_required" in lower:
        return "clarify"
    if "what time window" in lower or "what two scopes should i compare" in lower:
        return "clarify"
    if "response_type" in lower and "clarification" in lower:
        return "clarify"
    if "response_type" in lower and "error" in lower:
        return "error"
    if "error" in lower and ("cannot" in lower or "failed" in lower or "invalid" in lower):
        return "error"
    return "answer"


def infer_response_type(response: str, structured: dict[str, Any] | None = None) -> str:
    if structured:
        rt = str(structured.get("response_type") or "").strip().lower()
        if rt in {"clarification", "error", "result", "text"}:
            return rt
    lower = response.lower()
    if "response_type" in lower and "clarification" in lower:
        return "clarification"
    if "response_type" in lower and "error" in lower:
        return "error"
    if "response_type" in lower and "result" in lower:
        return "result"
    inferred = infer_mode(response, structured=structured)
    if inferred == "clarify":
        return "clarification"
    if inferred == "error":
        return "error"
    return "result"


def score_case(case: dict, response: str, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    if structured is None:
        structured = parse_structured_from_response_text(response)
    expected_mode = str(case.get("expected_mode") or "").strip() or None
    expected_response_type = str(case.get("expected_response_type") or "").strip() or None
    must_contain_any = _as_str_list(case.get("must_contain_any"))
    must_not_contain_any = _as_str_list(case.get("must_not_contain_any"))
    lower = response.lower()
    match_haystacks = _scoring_haystacks(response, structured)

    checks: list[dict[str, Any]] = []

    actual_mode = infer_mode(response, structured=structured)
    checks.append(
        {
            "name": "expected_mode",
            "passed": (expected_mode is None) or (actual_mode == expected_mode),
            "expected": expected_mode,
            "actual": actual_mode,
        }
    )

    actual_response_type = infer_response_type(response, structured=structured)
    checks.append(
        {
            "name": "expected_response_type",
            "passed": (expected_response_type is None) or (actual_response_type == expected_response_type),
            "expected": expected_response_type,
            "actual": actual_response_type,
        }
    )

    if must_contain_any:
        present: list[str] = []
        for token in must_contain_any:
            tl = token.lower()
            for h in match_haystacks:
                if tl in h:
                    present.append(token)
                    break
        checks.append(
            {
                "name": "must_contain_any",
                "passed": bool(present),
                "expected": must_contain_any,
                "actual": present,
            }
        )
    if must_not_contain_any:
        blocked: list[str] = []
        for token in must_not_contain_any:
            tl = token.lower()
            for h in match_haystacks:
                if tl in h:
                    blocked.append(token)
                    break
        checks.append(
            {
                "name": "must_not_contain_any",
                "passed": not blocked,
                "expected": must_not_contain_any,
                "actual": blocked,
            }
        )

    passed = all(bool(c["passed"]) for c in checks)
    return {
        "passed": passed,
        "inferred_mode": actual_mode,
        "inferred_response_type": actual_response_type,
        "used_structured_payload": bool(structured),
        "checks": checks,
    }


def summarize_rows(rows: list[dict]) -> dict[str, Any]:
    total = len(rows)
    passed = sum(1 for r in rows if r.get("assertions", {}).get("passed"))
    failed = total - passed
    pass_rate = (passed / total) if total else 0.0

    by_priority: dict[str, dict[str, int]] = {}
    by_category: dict[str, dict[str, int]] = {}
    by_mode: dict[str, dict[str, int]] = {}
    for row in rows:
        ok = bool(row.get("assertions", {}).get("passed"))
        status_key = "passed" if ok else "failed"
        priority = str(row.get("priority") or "unlabeled")
        category = str(row.get("category") or "uncategorized")
        mode = str(row.get("expected_mode") or "unspecified")
        by_priority.setdefault(priority, {"passed": 0, "failed": 0})[status_key] += 1
        by_category.setdefault(category, {"passed": 0, "failed": 0})[status_key] += 1
        by_mode.setdefault(mode, {"passed": 0, "failed": 0})[status_key] += 1

    failing_case_ids = [str(row.get("id") or "") for row in rows if not row.get("assertions", {}).get("passed")]
    return {
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": failed,
        "pass_rate": round(pass_rate, 4),
        "failing_case_ids": failing_case_ids,
        "by_priority": by_priority,
        "by_category": by_category,
        "by_expected_mode": by_mode,
    }


def _safe_metric(name: str):
    metric = getattr(types.RubricMetric, name, None)
    if metric is None:
        raise SystemExit(f"SDK missing RubricMetric.{name}; upgrade google-cloud-aiplatform[evaluation].")
    return metric


def default_metrics() -> list:
    return [
        _safe_metric("FINAL_RESPONSE_QUALITY"),
        _safe_metric("TOOL_USE_QUALITY"),
        _safe_metric("HALLUCINATION"),
        _safe_metric("SAFETY"),
    ]


def build_eval_dataset(cases: list[dict]) -> pd.DataFrame:
    prompts: list[str] = []
    session_inputs: list = []
    for case in cases:
        prompt = case_prompt_for_inference(case)
        if not prompt:
            continue
        prompts.append(prompt)
        session_inputs.append(types.evals.SessionInput(user_id=f"eval-ds-{uuid.uuid4().hex[:8]}", state={}))
    if not prompts:
        raise SystemExit("No non-empty prompts found in cases file.")
    return pd.DataFrame({"prompt": prompts, "session_inputs": session_inputs})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", required=True, help="projects/.../reasoningEngines/ID")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument("--out", default="logs/agent-engine-eval-report.json")
    parser.add_argument(
        "--cases",
        default="scripts/evals/agent_engine_eval_cases.json",
        help="Path to JSON eval cases",
    )
    parser.add_argument(
        "--publish-to-vertex",
        action="store_true",
        help="Create a Vertex evaluation run (shows under Evaluation tab)",
    )
    parser.add_argument("--gcs-dest", help="gs://... destination for Vertex evaluation artifacts")
    parser.add_argument("--display-name", help="Display name for the Vertex evaluation run")
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Label for eval run as key=value (repeatable)",
    )
    parser.add_argument(
        "--fail-on-assertion",
        action="store_true",
        help="Exit non-zero if any eval case assertion fails.",
    )
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        help="Exit non-zero when pass rate drops below this value (0.0 to 1.0).",
    )
    parser.add_argument(
        "--fail-on-priority",
        action="append",
        default=[],
        help="Exit non-zero if any case with this priority fails (repeatable, e.g. P0).",
    )
    parser.add_argument(
        "--turn-timeout-seconds",
        type=float,
        default=0.0,
        help="Stop reading stream events for each turn after this many seconds (0 = no limit; best-effort).",
    )
    parser.add_argument(
        "--turn-retries",
        type=int,
        default=0,
        help="On empty final text for a turn, retry the turn up to this many extra times (same session).",
    )
    args = parser.parse_args()

    if not args.project:
        raise SystemExit("Set --project or GOOGLE_CLOUD_PROJECT.")
    if args.publish_to_vertex and not args.gcs_dest:
        raise SystemExit("--gcs-dest is required when --publish-to-vertex is set.")
    if args.min_pass_rate is not None and not (0.0 <= args.min_pass_rate <= 1.0):
        raise SystemExit("--min-pass-rate must be between 0.0 and 1.0")
    if args.turn_retries < 0:
        raise SystemExit("--turn-retries must be non-negative")
    if args.turn_timeout_seconds < 0:
        raise SystemExit("--turn-timeout-seconds must be non-negative")

    cases = load_cases(args.cases)
    validate_cases(cases)
    labels = parse_labels(args.label)

    vertexai.init(project=args.project, location=args.location)
    engine = agent_engines.get(args.resource)
    eval_client = Client(
        project=args.project,
        location=args.location,
        http_options=genai_types.HttpOptions(api_version="v1beta1"),
    )

    rows: list[dict] = []
    for idx, case in enumerate(cases, start=1):
        turns = case_turns(case)
        if not turns:
            continue
        case_id = str(case.get("id") or f"case-{idx}")
        user_id = f"eval-{uuid.uuid4().hex[:8]}"
        sess = engine.create_session(user_id=user_id)
        session_id = sess.get("id")
        if not session_id:
            raise SystemExit("create_session failed for eval run")
        turn_rows: list[dict] = []
        final_response = ""
        final_structured: dict[str, Any] | None = None
        for turn in turns:
            turn_structured: dict[str, Any] | None = None
            joined = ""
            attempts = 1 + int(args.turn_retries)
            for attempt in range(attempts):
                chunks: list[str] = []
                collected: list[dict[str, Any]] = []
                stop = [False]

                def _timeout_stop() -> None:
                    stop[0] = True

                tmr: threading.Timer | None = None
                if args.turn_timeout_seconds and args.turn_timeout_seconds > 0:
                    tmr = threading.Timer(args.turn_timeout_seconds, _timeout_stop)
                    tmr.daemon = True
                    tmr.start()
                try:
                    for ev in engine.stream_query(message=turn, user_id=user_id, session_id=session_id):
                        if stop[0]:
                            break
                        text = extract_text(ev)
                        if text:
                            chunks.append(text)
                        payloads = extract_structured_payloads(ev)
                        if payloads:
                            collected.extend(payloads)
                finally:
                    if tmr is not None:
                        tmr.cancel()
                turn_structured = _prefer_structured(collected) or turn_structured
                joined = "\n".join(chunks).strip()
                parsed = parse_structured_from_response_text(joined)
                if parsed is not None:
                    turn_structured = parsed
                if joined.strip() or attempt + 1 >= attempts:
                    break
            final_response = joined or final_response
            if turn_structured is not None:
                final_structured = turn_structured
            turn_rows.append(
                {
                    "prompt": turn,
                    "response": joined,
                    "structured_response": turn_structured,
                }
            )
        rows.append(
            {
                "id": case_id,
                "category": case.get("category"),
                "priority": case.get("priority"),
                "prompt": turns[-1],
                "turns": turn_rows,
                "expected_mode": case.get("expected_mode"),
                "expected_response_type": case.get("expected_response_type"),
                "must_contain_any": case.get("must_contain_any", []),
                "must_not_contain_any": case.get("must_not_contain_any", []),
                "user_id": user_id,
                "session_id": session_id,
                "response": final_response,
                "structured_response": final_structured,
                "assertions": score_case(case, final_response, structured=final_structured),
            }
        )

    summary = summarize_rows(rows)

    eval_run_name: str | None = None
    if args.publish_to_vertex:
        dataset = build_eval_dataset(cases)
        inferred_dataset = eval_client.evals.run_inference(agent=args.resource, src=dataset)
        eval_run = eval_client.evals.create_evaluation_run(
            dataset=inferred_dataset,
            agent=args.resource,
            metrics=default_metrics(),
            dest=args.gcs_dest,
            display_name=args.display_name,
            labels=labels if labels else None,
        )
        eval_run_name = eval_run.name

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "location": args.location,
        "resource": args.resource,
        "vertex_eval_published": bool(args.publish_to_vertex),
        "vertex_eval_run_name": eval_run_name,
        "vertex_eval_display_name": args.display_name,
        "vertex_eval_labels": labels,
        "vertex_eval_gcs_dest": args.gcs_dest,
        "summary": summary,
        "cases": rows,
        "note": "This harness records baseline responses, scores deterministic assertions, and can publish a Vertex evaluation run.",
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote evaluation baseline report: {out}")
    print(
        "Assertion summary: "
        f"{summary['passed_cases']}/{summary['total_cases']} passed "
        f"(pass_rate={summary['pass_rate']:.2%})."
    )
    if eval_run_name:
        print(f"Created Vertex evaluation run: {eval_run_name}")
        print("Check the Agent Engine Evaluation tab after the run is processed.")
    else:
        print("Baseline-only mode. Re-run with --publish-to-vertex and --gcs-dest to populate Evaluation tab.")

    should_fail = False
    if args.fail_on_assertion and summary["failed_cases"] > 0:
        print("--fail-on-assertion triggered: one or more cases failed.")
        should_fail = True
    if args.min_pass_rate is not None and summary["pass_rate"] < args.min_pass_rate:
        print(
            "--min-pass-rate triggered: "
            f"actual={summary['pass_rate']:.4f} required={args.min_pass_rate:.4f}"
        )
        should_fail = True
    if args.fail_on_priority:
        requested = {p.strip().upper() for p in args.fail_on_priority if p.strip()}
        failed_priorities = {
            str(row.get("priority")).upper()
            for row in rows
            if not row.get("assertions", {}).get("passed") and row.get("priority")
        }
        blocked = sorted(requested.intersection(failed_priorities))
        if blocked:
            print(f"--fail-on-priority triggered for: {', '.join(blocked)}")
            should_fail = True

    if should_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
