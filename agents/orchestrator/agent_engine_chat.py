"""
Forward UI chat to Vertex AI Agent Engine (stream_query) and re-emit A2A-shaped SSE.

Browsers cannot call reasoningEngines:query directly (auth). The FastAPI orchestrator
uses Application Default Credentials and streams results in the format the Next.js UI expects.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import uuid
from typing import Any, AsyncIterator

import asyncpg
import vertexai
import vertexai.agent_engines as agent_engines

from intelligence import sse_pack_a2a
import session_repository

logger = logging.getLogger(__name__)
_COST_PAYLOAD_MARKER = "COST_PAYLOAD_JSON:\n"

_ORCHESTRATOR_RESOURCE = os.environ.get(
    "ORCHESTRATOR_AGENT_ENGINE_RESOURCE", ""
).strip()
_ORCHESTRATOR_QUERY_URL = os.environ.get(
    "ORCHESTRATOR_AGENT_ENGINE_QUERY_URL", ""
).strip()
_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip()

def _resource_from_query_url(url: str) -> str:
    m = re.search(
        r"(projects/[^/]+/locations/[^/]+/reasoningEngines/[^/:]+)",
        url,
        re.I,
    )
    return m.group(1) if m else ""


def _project_from_resource(resource: str) -> str:
    m = re.search(r"projects/([^/]+)/locations/", resource, re.I)
    return m.group(1) if m else ""


def _location_from_resource(resource: str) -> str:
    m = re.search(r"locations/([^/]+)/reasoningEngines/", resource, re.I)
    return m.group(1) if m else ""


def resolved_engine_resource() -> str:
    if _ORCHESTRATOR_RESOURCE:
        return _ORCHESTRATOR_RESOURCE
    if _ORCHESTRATOR_QUERY_URL:
        return _resource_from_query_url(_ORCHESTRATOR_QUERY_URL)
    return ""


def resolved_project() -> str:
    if _PROJECT:
        return _PROJECT
    return _project_from_resource(resolved_engine_resource())


def resolved_location() -> str:
    if _LOCATION:
        return _LOCATION
    return _location_from_resource(resolved_engine_resource()) or "us-central1"


def is_agent_engine_chat_enabled() -> bool:
    # Local dev: UI should hit FastAPI /chat/stream → cost agent on :8001 (BigQuery).
    # Set ORCHESTRATOR_LOCAL_CHAT=1 when ORCHESTRATOR_AGENT_ENGINE_RESOURCE is set but
    # your user/SA lacks reasoningEngines.get/query (otherwise the stream crashes → browser "Network error").
    if os.environ.get("ORCHESTRATOR_LOCAL_CHAT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    return bool(resolved_engine_resource() and resolved_project())


def _extract_text_from_part(p: dict) -> str:
    if p.get("text"):
        return str(p["text"])
    # Suppress tool-call/function-response chatter in UI stream text.
    return ""


def _unwrap_result_text(value: Any) -> str:
    """Best-effort recursive unwrapping for nested {"result": "..."} payloads."""
    if value is None:
        return ""
    if isinstance(value, dict):
        if "response" in value:
            unwrapped_response = _unwrap_result_text(value.get("response"))
            if unwrapped_response:
                return unwrapped_response
        if "result" in value:
            return _unwrap_result_text(value.get("result"))
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    text = str(value).strip()
    if not text:
        return ""
    try:
        loaded = json.loads(text)
    except Exception:
        return text
    return _unwrap_result_text(loaded)


def _extract_first_json_snippet(text: str) -> str:
    """Extract first balanced JSON object/array snippet from text."""
    start = -1
    for i, ch in enumerate(text):
        if ch in "[{":
            start = i
            break
    if start < 0:
        return ""
    stack: list[str] = []
    pair = {"[": "]", "{": "}"}
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in pair:
            stack.append(pair[ch])
            continue
        if ch in ("]", "}"):
            if not stack or stack[-1] != ch:
                return ""
            stack.pop()
            if not stack:
                snippet = text[start : i + 1].strip()
                try:
                    json.loads(snippet)
                    return snippet
                except Exception:
                    return ""
    return ""


def _extract_structured_result_from_event(event: dict) -> str:
    """
    Pull structured JSON rows from function_response payloads and format for
    frontend table parser.
    """
    content = event.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    for p in parts:
        if not isinstance(p, dict):
            continue
        fr = p.get("function_response")
        if fr is None:
            continue
        payload_text = _unwrap_result_text(fr)
        if not payload_text:
            continue
        snippet = _extract_first_json_snippet(payload_text)
        if not snippet:
            continue
        try:
            parsed = json.loads(snippet)
        except Exception:
            continue
        if isinstance(parsed, dict) and parsed.get("response_type"):
            return f"{_COST_PAYLOAD_MARKER}{json.dumps(parsed, ensure_ascii=False)}"
        # Backward compatibility: old specialist payloads may be raw arrays/objects.
        wrapped = {"response_type": "result", "data": parsed}
        return f"{_COST_PAYLOAD_MARKER}{json.dumps(wrapped, ensure_ascii=False)}"
    return ""


def _extract_text_from_vertex_event(event: dict) -> str:
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


def _create_vertex_session(client_session_id: str) -> tuple[str, str]:
    """Sync: create Agent Engine session (runs in thread pool)."""
    resource = resolved_engine_resource()
    vertexai.init(project=resolved_project(), location=resolved_location())
    engine = agent_engines.get(resource)
    user_id = f"ui-{client_session_id}"
    sess = engine.create_session(user_id=user_id)
    sid = sess.get("id") if isinstance(sess, dict) else None
    if not sid:
        raise RuntimeError("Agent Engine create_session returned no session id")
    return user_id, str(sid)


async def _ensure_ui_session(
    pool: asyncpg.Pool,
    tenant_id: str,
    owner_user_id: str,
    client_session_id: str,
) -> tuple[str, str]:
    """Map canonical UI session UUID -> (Agent Engine user_id, engine session id); persisted in Postgres."""
    cid = uuid.UUID(client_session_id)
    async with pool.acquire() as conn:
        existing = await session_repository.get_agent_engine_binding(
            conn, cid, tenant_id, owner_user_id
        )
    if existing:
        return existing

    user_id, engine_sid = await asyncio.to_thread(
        _create_vertex_session, client_session_id
    )

    async with pool.acquire() as conn:
        await session_repository.upsert_agent_engine_binding(
            conn,
            cid,
            tenant_id,
            owner_user_id,
            user_id,
            engine_sid,
        )
    return user_id, engine_sid


async def _iter_stream_query(
    message: str, user_id: str, engine_session_id: str
) -> AsyncIterator[dict]:
    """Run synchronous stream_query in a worker thread; async-iterate events."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[Any] = asyncio.Queue(maxsize=512)
    _DONE = object()

    resource = resolved_engine_resource()

    def worker() -> None:
        try:
            vertexai.init(project=resolved_project(), location=resolved_location())
            engine = agent_engines.get(resource)
            for ev in engine.stream_query(
                message=message,
                user_id=user_id,
                session_id=engine_session_id,
            ):
                asyncio.run_coroutine_threadsafe(q.put(ev), loop).result(timeout=180)
            asyncio.run_coroutine_threadsafe(q.put(_DONE), loop).result(timeout=30)
        except Exception as e:
            logger.exception("Agent Engine stream_query failed")
            asyncio.run_coroutine_threadsafe(q.put(("__error__", e)), loop).result(
                timeout=30
            )

    threading.Thread(target=worker, daemon=True).start()
    while True:
        item = await q.get()
        if item is _DONE:
            return
        if isinstance(item, tuple) and item[0] == "__error__":
            raise item[1]
        if isinstance(item, dict):
            yield item
        else:
            logger.debug("Skipping non-dict Agent Engine event: %s", type(item).__name__)


async def stream_chat_via_agent_engine(
    message: str,
    client_session_id: str,
    pool: asyncpg.Pool,
    tenant_id: str,
    owner_user_id: str,
) -> AsyncIterator[bytes]:
    """
    Stream one user turn through Vertex Agent Engine; output matches frontend SSE parser.
    """
    user_id, engine_sid = await _ensure_ui_session(
        pool, tenant_id, owner_user_id, client_session_id
    )
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    structured_sent = False

    try:
        async for ev in _iter_stream_query(message.strip(), user_id, engine_sid):
            if ev.get("code"):
                err = f"Agent Engine error {ev.get('code')}: {ev.get('message')}"
                payload = json.dumps({"error": True, "detail": err}, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode()
                return
            if not structured_sent:
                structured = _extract_structured_result_from_event(ev)
                if structured:
                    structured_sent = True
                    yield sse_pack_a2a(
                        task_id, "working", structured, completed=False
                    ).encode()
                    continue
            delta = _extract_text_from_vertex_event(ev)
            if delta:
                yield sse_pack_a2a(
                    task_id, "working", delta, completed=False
                ).encode()
        yield sse_pack_a2a(task_id, "completed", "", completed=True).encode()
    except Exception as e:
        logger.exception("agent_engine_chat stream failed")
        payload = json.dumps(
            {"error": True, "detail": f"Agent Engine request failed: {e}"},
            ensure_ascii=False,
        )
        yield f"data: {payload}\n\n".encode()
