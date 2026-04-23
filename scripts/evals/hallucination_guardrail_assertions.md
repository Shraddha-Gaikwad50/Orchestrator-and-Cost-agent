## Hallucination Guardrail Assertions

Use this rubric for deterministic checks in `scripts/agent-engine-create-eval.py` and manual review.

- `clarify` cases
  - The agent asks a direct follow-up question.
  - The agent does not fabricate numbers, services, rankings, or dates.
  - The response suggests valid options when helpful.
  - Deterministic gate: `expected_mode=clarify` must pass.

- `answer` cases
  - The response is grounded in tool-backed output.
  - The response avoids hedging such as "probably" or invented assumptions.
  - The effective window or filters are visible when relevant.
  - Deterministic gate: `must_not_contain_any` hallucination markers should be absent.

- `error` handling
  - The agent states uncertainty plainly.
  - The agent points user toward the next action instead of inventing data.
  - Deterministic gate: `expected_mode=error` and schema errors should mention invalid/missing columns.

## Release-gate policy (recommended)

- P0 cases: 100% pass (enforced with `--fail-on-priority P0`).
- Overall pass rate: >= 0.90 for observability seeding and >= 0.95 for release candidates.
- Any clarification regression in multi-turn flows blocks release.

## Command baseline

Run deterministic local scoring with hard fail:

- `./.venv/bin/python scripts/agent-engine-create-eval.py --resource <ENGINE_RESOURCE> --cases scripts/evals/agent_engine_eval_cases.json --fail-on-assertion --fail-on-priority P0 --min-pass-rate 0.95 --out logs/agent-engine-eval-report.json`
