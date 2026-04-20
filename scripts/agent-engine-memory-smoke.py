#!/usr/bin/env python3
"""Seed Agent Engine sessions/memories with reusable multi-turn scenarios."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import vertexai
import vertexai.agent_engines as agent_engines


def extract_text(event: dict) -> str:
    content = event.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("text"):
            out.append(str(p["text"]))
    return "\n".join(out).strip()


def _default_scenarios() -> list[dict]:
    return [
        {
            "name": "cost_preference_memory",
            "turns": [
                "Remember this preference: I always prefer cost summaries grouped by project and service.",
                "For future answers, default to top 5 services unless I ask otherwise.",
                "Acknowledge these preferences in one sentence.",
            ],
            "verify_query": "What are my preferences for cost summaries?",
        },
        {
            "name": "schema_and_followup_memory",
            "turns": [
                "Remember this: my default schema focus column is project_name.",
                "Remember this too: when I ask unique values, I usually mean project_name unless I specify a different column.",
                "Confirm you stored my schema defaults.",
            ],
            "verify_query": "What schema defaults did I share?",
        },
    ]


def load_scenarios(path: str | None) -> list[dict]:
    if not path:
        return _default_scenarios()
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise SystemExit("--scenarios must point to a JSON array")
    return data


def _resource_short_name(resource: str) -> str:
    return resource.rstrip("/").split("/")[-1]


def _run_scenario(engine, resource: str, scenario: dict) -> dict:
    turns = scenario.get("turns")
    if not isinstance(turns, list) or not turns:
        raise SystemExit(f"Scenario '{scenario.get('name', 'unnamed')}' has no turns")
    user_id = f"memory-smoke-{uuid.uuid4().hex[:8]}"
    sess = engine.create_session(user_id=user_id)
    session_id = sess.get("id")
    if not session_id:
        raise SystemExit("Agent Engine did not return session id.")

    print(f"\nresource={resource}")
    print(f"scenario={scenario.get('name', 'unnamed')}")
    print(f"user_id={user_id}")
    print(f"session_id={session_id}")

    turn_rows: list[dict] = []
    for i, prompt in enumerate(turns, start=1):
        prompt_text = str(prompt).strip()
        if not prompt_text:
            continue
        chunks: list[str] = []
        for ev in engine.stream_query(message=prompt_text, user_id=user_id, session_id=session_id):
            text = extract_text(ev)
            if text:
                chunks.append(text)
        joined = "\n".join(chunks).strip()
        preview = joined[:500] + ("..." if len(joined) > 500 else "")
        print(f"\n[{i}] prompt: {prompt_text}\n[{i}] response preview:\n{preview}\n")
        turn_rows.append({"turn_index": i, "prompt": prompt_text, "response": joined})

    return {
        "resource": resource,
        "resource_short_name": _resource_short_name(resource),
        "scenario_name": scenario.get("name", "unnamed"),
        "user_id": user_id,
        "session_id": session_id,
        "turns": turn_rows,
    }


async def _trigger_memory_generation(engine, user_id: str, session_id: str) -> dict:
    session_obj = await engine.async_get_session(user_id=user_id, session_id=session_id)
    if not isinstance(session_obj, dict):
        raise RuntimeError("async_get_session returned unexpected payload")
    return await engine.async_add_session_to_memory(session=session_obj)


async def _search_memory(engine, user_id: str, query: str) -> dict:
    return await engine.async_search_memory(user_id=user_id, query=query)


def _extract_memory_count(search_payload: dict) -> int:
    if not isinstance(search_payload, dict):
        return 0
    candidates = search_payload.get("memories")
    if isinstance(candidates, list):
        return len(candidates)
    for key in ("results", "items", "matches"):
        val = search_payload.get(key)
        if isinstance(val, list):
            return len(val)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--resource",
        action="append",
        default=[],
        help="projects/.../reasoningEngines/ID (repeat flag for multiple engines)",
    )
    parser.add_argument("--resources-file", help="JSON file containing an array of engine resources")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument(
        "--scenarios",
        default="scripts/evals/memory_seed_cases.json",
        help="Path to JSON scenario list (default: scripts/evals/memory_seed_cases.json)",
    )
    parser.add_argument(
        "--out",
        default="logs/agent-engine-memory-seed-report.json",
        help="Where to write memory seeding report JSON",
    )
    parser.add_argument(
        "--skip-memory-trigger",
        action="store_true",
        help="Skip explicit add_session_to_memory call",
    )
    parser.add_argument(
        "--verify-memory",
        action="store_true",
        help="Search memory after generation and report count",
    )
    parser.add_argument(
        "--memory-search-wait-seconds",
        type=int,
        default=20,
        help="Max wait for memory search to return results",
    )
    parser.add_argument(
        "--memory-search-interval-seconds",
        type=int,
        default=4,
        help="Polling interval for memory search",
    )
    args = parser.parse_args()

    if not args.project:
        raise SystemExit("Set --project or GOOGLE_CLOUD_PROJECT.")

    resources = list(args.resource)
    if args.resources_file:
        raw = Path(args.resources_file).read_text(encoding="utf-8")
        file_resources = json.loads(raw)
        if not isinstance(file_resources, list):
            raise SystemExit("--resources-file must contain a JSON array")
        resources.extend(str(x).strip() for x in file_resources if str(x).strip())
    if not resources:
        raise SystemExit("Provide at least one --resource (or --resources-file).")

    scenarios = load_scenarios(args.scenarios)
    vertexai.init(project=args.project, location=args.location)
    rows: list[dict] = []
    for resource in resources:
        engine = agent_engines.get(resource)
        for scenario in scenarios:
            row = _run_scenario(engine=engine, resource=resource, scenario=scenario)
            user_id = row["user_id"]
            session_id = row["session_id"]

            trigger_payload: dict | None = None
            trigger_error: str | None = None
            if not args.skip_memory_trigger:
                try:
                    trigger_payload = asyncio.run(
                        _trigger_memory_generation(
                            engine=engine, user_id=user_id, session_id=session_id
                        )
                    )
                except Exception as exc:
                    trigger_error = str(exc)

            verify_query = str(scenario.get("verify_query") or "").strip()
            verify_result: dict | None = None
            verify_count = 0
            verify_passed: bool | None = None
            verify_error: str | None = None
            if args.verify_memory and verify_query:
                deadline = time.time() + max(args.memory_search_wait_seconds, 1)
                while time.time() < deadline:
                    try:
                        verify_result = asyncio.run(
                            _search_memory(engine=engine, user_id=user_id, query=verify_query)
                        )
                        verify_count = _extract_memory_count(verify_result)
                        if verify_count > 0:
                            verify_passed = True
                            break
                    except Exception as exc:
                        verify_error = str(exc)
                        break
                    time.sleep(max(args.memory_search_interval_seconds, 1))
                if verify_passed is None:
                    verify_passed = verify_count > 0

            row["memory_generation_requested"] = not args.skip_memory_trigger
            row["memory_generation_payload"] = trigger_payload
            row["memory_generation_error"] = trigger_error
            row["memory_verify_query"] = verify_query or None
            row["memory_verify_enabled"] = bool(args.verify_memory and verify_query)
            row["memory_verify_result_count"] = verify_count
            row["memory_verify_passed"] = verify_passed
            row["memory_verify_error"] = verify_error
            if verify_result is not None:
                row["memory_verify_result"] = verify_result
            rows.append(row)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "location": args.location,
        "resources": resources,
        "scenario_count": len(scenarios),
        "runs": rows,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Done. Wrote memory seeding report: {out}")
    print("Check Agent Engine Sessions/Traces/Memories tabs for each engine.")


if __name__ == "__main__":
    main()
