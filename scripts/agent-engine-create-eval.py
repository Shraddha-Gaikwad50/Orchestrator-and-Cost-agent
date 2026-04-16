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
        if isinstance(p, dict) and p.get("text"):
            out.append(str(p["text"]))
    return "\n".join(out).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", required=True, help="projects/.../reasoningEngines/ID")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument("--out", default="logs/agent-engine-eval-report.json")
    args = parser.parse_args()

    if not args.project:
        raise SystemExit("Set --project or GOOGLE_CLOUD_PROJECT.")

    prompts = [
        "List all unique services used till now.",
        "What are the 3 most expensive services till date?",
        "What was total spend in march and april combined till now?",
    ]

    vertexai.init(project=args.project, location=args.location)
    engine = agent_engines.get(args.resource)

    rows: list[dict] = []
    for prompt in prompts:
        user_id = f"eval-{uuid.uuid4().hex[:8]}"
        sess = engine.create_session(user_id=user_id)
        session_id = sess.get("id")
        if not session_id:
            raise SystemExit("create_session failed for eval run")
        chunks: list[str] = []
        for ev in engine.stream_query(message=prompt, user_id=user_id, session_id=session_id):
            text = extract_text(ev)
            if text:
                chunks.append(text)
        rows.append(
            {
                "prompt": prompt,
                "user_id": user_id,
                "session_id": session_id,
                "response": "\n".join(chunks).strip(),
            }
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "location": args.location,
        "resource": args.resource,
        "cases": rows,
        "note": (
            "This harness records baseline responses and sessions. "
            "Use these cases in your Colab/SDK evaluation workflow to create console evaluation runs."
        ),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote evaluation baseline report: {out}")
    print("You can now use these cases in your Vertex evaluation notebook/SDK flow.")


if __name__ == "__main__":
    main()
