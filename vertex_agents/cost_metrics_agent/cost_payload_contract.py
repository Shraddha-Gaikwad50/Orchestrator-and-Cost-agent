"""Typed response prefix for cost specialist outputs (clarify/error contract).

Keep in sync with `agents/orchestrator/agent_engine_chat._COST_PAYLOAD_MARKER`
and the prefix parsed by `scripts/agent-engine-create-eval.py`.
"""

COST_PAYLOAD_PREFIX = "COST_PAYLOAD_JSON:\n"
