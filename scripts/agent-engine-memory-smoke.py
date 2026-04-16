#!/usr/bin/env python3
"""Seed Agent Engine sessions/memories by running multi-turn chat in one session."""

from __future__ import annotations

import argparse
import os
import uuid

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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", required=True, help="projects/.../reasoningEngines/ID")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    args = parser.parse_args()

    if not args.project:
        raise SystemExit("Set --project or GOOGLE_CLOUD_PROJECT.")

    vertexai.init(project=args.project, location=args.location)
    engine = agent_engines.get(args.resource)
    user_id = f"memory-smoke-{uuid.uuid4().hex[:8]}"
    sess = engine.create_session(user_id=user_id)
    session_id = sess.get("id")
    if not session_id:
        raise SystemExit("Agent Engine did not return session id.")

    prompts = [
        "Hello, remember I care about spend by project and service.",
        "What are my top services this month?",
        "Now focus only on invoice-like cost categories and summarize.",
    ]

    print(f"user_id={user_id}")
    print(f"session_id={session_id}")
    for i, prompt in enumerate(prompts, start=1):
        chunks: list[str] = []
        for ev in engine.stream_query(message=prompt, user_id=user_id, session_id=session_id):
            text = extract_text(ev)
            if text:
                chunks.append(text)
        joined = "\n".join(chunks).strip()
        preview = joined[:500] + ("..." if len(joined) > 500 else "")
        print(f"\n[{i}] prompt: {prompt}\n[{i}] response preview:\n{preview}\n")

    print("Done. Check Agent Engine Sessions/Traces/Memories tabs for this engine.")


if __name__ == "__main__":
    main()
