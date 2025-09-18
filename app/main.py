#!/usr/bin/env python3
"""
Jira MCP Server (JSON-RPC only) - main.py

- Exposes POST /jsonrpc that accepts JSON-RPC 2.0 requests.
- Read-only tools: search_issues, get_issue, summarize_issue.
- Minimal /healthz endpoint for readiness checks.

Configuration via environment:
- JIRA_BASE_URL (required): e.g. https://your-domain.atlassian.net
- JIRA_TOKEN  (required): OAuth2 access token OR API token
- JIRA_USER   (optional): when set, JIRA_TOKEN is treated as API token and Basic auth is used (email:token)
- ALLOW_RAW_JQL (optional): "true"/"false" default false
"""
from __future__ import annotations

import os
import logging
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from base64 import b64encode, b64decode

# -------- logging --------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jira-mcp-jsonrpc")

# -------- config --------
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_TOKEN = os.getenv("JIRA_TOKEN", "")
JIRA_USER = os.getenv("JIRA_USER", "")
ALLOW_RAW_JQL = os.getenv("ALLOW_RAW_JQL", "false").lower() == "true"

if not JIRA_BASE_URL:
    raise RuntimeError("JIRA_BASE_URL must be set")
if not JIRA_TOKEN:
    raise RuntimeError("JIRA_TOKEN must be set")

# -------- http client defaults --------
TIMEOUT = httpx.Timeout(10.0, read=10.0)
RETRY_STATUSES = {429, 500, 502, 503, 504}


# -------- helpers --------
async def _sleep(s: float) -> None:
    import asyncio

    await asyncio.sleep(s)


async def jira_request(
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
) -> Any:
    """
    Simple wrapper for calling Jira REST API.
    Why: centralizes auth, retries, and error normalization.
    """
    url = f"{JIRA_BASE_URL}{path}"
    headers = {}

    if JIRA_USER:
        cred = f"{JIRA_USER}:{JIRA_TOKEN}".encode("utf-8")
        headers["Authorization"] = f"Basic {b64encode(cred).decode('ascii')}"
    else:
        headers["Authorization"] = f"Bearer {JIRA_TOKEN}"

    headers.setdefault("Accept", "application/json")
    headers.setdefault("Content-Type", "application/json")

    attempt = 0
    backoff = 0.4

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        while True:
            attempt += 1
            try:
                resp = await client.request(method, url, headers=headers, params=params, json=json_body)
            except httpx.RequestError as exc:
                if attempt <= 3:
                    await _sleep(backoff)
                    backoff *= 2
                    continue
                logger.exception("Network error calling Jira %s %s: %s", method, url, exc)
                raise HTTPException(status_code=502, detail=f"Network error: {exc}")

            if resp.status_code in RETRY_STATUSES and attempt <= 3:
                ra = resp.headers.get("Retry-After")
                delay = float(ra) if ra else backoff
                await _sleep(delay)
                backoff = min(backoff * 2, 5.0)
                continue

            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                logger.debug("Jira returned %s: %s", resp.status_code, detail)
                raise HTTPException(status_code=resp.status_code, detail=detail)

            try:
                return resp.json()
            except Exception:
                return resp.text


def _pydantic_to_json(obj: Any) -> Any:
    """Why: support Pydantic v1/v2 without coupling; convert to plain JSONable."""
    if isinstance(obj, BaseModel):
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        return obj.dict()
    return obj


# -------- pydantic models --------
class Filters(BaseModel):
    project: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    labels: Optional[List[str]] = None
    created_since: Optional[str] = None
    text: Optional[str] = None


class SearchIssuesInput(BaseModel):
    jql: Optional[str] = None
    filters: Optional[Filters] = None
    limit: int = Field(25, ge=1, le=100)
    cursor: Optional[str] = None


class GetIssueInput(BaseModel):
    key: str


class SummarizeIssueInput(BaseModel):
    key: str


class Envelope(BaseModel):
    data: Any
    next_cursor: Optional[str] = None
    total_estimate: Optional[int] = None
    warnings: Optional[List[str]] = None


# -------- small helpers for JQL / redaction --------
def ensure_project_allowed(project: Optional[str]) -> None:
    # Stub: hook for allowlist enforcement.
    return


def build_jql(filters: Optional[Filters]) -> str:
    if not filters:
        return "order by updated desc"
    parts: List[str] = []
    if filters.project:
        ensure_project_allowed(filters.project)
        parts.append(f"project = {filters.project}")
    if filters.status:
        parts.append(f'status = "{filters.status}"')
    if filters.assignee:
        parts.append(f'assignee = "{filters.assignee}"')
    if filters.labels:
        labels = ",".join([f'"{l}"' for l in filters.labels])
        parts.append(f"labels in ({labels})")
    if filters.created_since:
        v = filters.created_since
        if v.endswith("d") and v[:-1].isdigit():
            parts.append(f"created >= -{int(v[:-1])}d")
        else:
            parts.append(f'created >= "{v}"')
    if filters.text:
        safe = filters.text.replace('"', '\\"')
        parts.append(f'text ~ "{safe}"')
    if not parts:
        return "order by updated desc"
    return " AND ".join(parts)


def redact(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    import re

    return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<redacted>", text)


# -------- internal tool implementations (read-only) --------
async def search_issues_internal(inp: SearchIssuesInput) -> Envelope:
    jql = inp.jql if (inp.jql and ALLOW_RAW_JQL) else build_jql(inp.filters)
    start_at = 0
    if inp.cursor:
        try:
            start_at = int(b64decode(inp.cursor).decode("utf-8"))
        except Exception:
            start_at = 0

    params = {"jql": jql, "maxResults": inp.limit, "startAt": start_at}
    data = await jira_request("GET", "/rest/api/3/search", params=params)

    issues_out: List[Dict[str, Any]] = []
    for it in data.get("issues", []):
        fields = it.get("fields", {}) or {}
        issues_out.append(
            {
                "key": it.get("key"),
                "summary": fields.get("summary"),
                "status": (fields.get("status") or {}).get("name"),
                "assignee": (fields.get("assignee") or {}).get("displayName")
                            or (fields.get("assignee") or {}).get("accountId"),
                "priority": (fields.get("priority") or {}).get("name"),
                "updated_at": fields.get("updated"),
            }
        )

    total = data.get("total")
    maxr = data.get("maxResults", len(issues_out))
    next_cursor = None
    if (start_at + maxr) < (total or 0):
        next_cursor = b64encode(str(start_at + maxr).encode("utf-8")).decode("utf-8")

    return Envelope(data=issues_out, next_cursor=next_cursor, total_estimate=total)


async def get_issue_internal(inp: GetIssueInput) -> Envelope:
    it = await jira_request("GET", f"/rest/api/3/issue/{inp.key}")
    fields = it.get("fields", {}) or {}

    comments_block = (fields.get("comment") or {}).get("comments", []) if fields.get("comment") else []
    comments = [
        {
            "author": (c.get("author") or {}).get("displayName"),
            "created": c.get("created"),
            "body": redact(c.get("body")),
        }
        for c in comments_block
    ]

    links_out: List[Dict[str, str]] = []
    for link in fields.get("issuelinks", []) or []:
        if "outwardIssue" in link:
            links_out.append(
                {"type": (link.get("type") or {}).get("name", "relates to"), "key": link["outwardIssue"]["key"]}
            )
        if "inwardIssue" in link:
            links_out.append(
                {"type": (link.get("type") or {}).get("name", "relates to"), "key": link["inwardIssue"]["key"]}
            )

    obj = {
        "key": it.get("key"),
        "summary": fields.get("summary"),
        "description": redact(fields.get("description")),
        "status": (fields.get("status") or {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName")
                    or (fields.get("assignee") or {}).get("accountId"),
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "labels": fields.get("labels"),
        "fix_versions": [v.get("name") for v in (fields.get("fixVersions") or [])],
        "components": [c.get("name") for c in (fields.get("components") or [])],
        "links": links_out or None,
        "comments": comments or None,
        "updated_at": fields.get("updated"),
        "url": f"{JIRA_BASE_URL}/browse/{it.get('key')}",
    }
    return Envelope(data=obj)


async def summarize_issue_internal(inp: SummarizeIssueInput) -> Envelope:
    env = await get_issue_internal(GetIssueInput(key=inp.key))
    data = env.data if isinstance(env, Envelope) else env  # keeps Envelope shape consistent
    summary = {
        "key": data.get("key"),
        "title": data.get("summary"),
        "status": data.get("status"),
        "assignee": data.get("assignee"),
        "last_update": data.get("updated_at"),  # why: the detailed shape uses `updated_at`
        "url": data.get("url"),
    }
    return Envelope(data=summary)


async def list_tools_internal(_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "tools": [
            {
                "name": "search_issues",
                "description": "Search Jira issues by filters or JQL.",
                "input_schema": SearchIssuesInput.schema(),
            },
            {
                "name": "get_issue",
                "description": "Fetch a single issue by key.",
                "input_schema": GetIssueInput.schema(),
            },
            {
                "name": "summarize_issue",
                "description": "Return a terse summary for humans.",
                "input_schema": SummarizeIssueInput.schema(),
            },
        ]
    }


# -------- JSON-RPC wiring --------
app = FastAPI(title="Jira MCP Server (JSON-RPC)", version="0.1.0")

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_PROVIDER_ERROR = -32000

METHOD_MAP = {
    "initialize": lambda params: {"protocol": "mcp-jira-jsonrpc", "version": "1.0"},
    "tools.list": list_tools_internal,
    "tools.search_issues": search_issues_internal,
    "search_issues": search_issues_internal,
    "tools.get_issue": get_issue_internal,
    "get_issue": get_issue_internal,
    "tools.summarize_issue": summarize_issue_internal,
    "summarize_issue": summarize_issue_internal,
}

MODEL_MAP = {
    "tools.search_issues": SearchIssuesInput,
    "search_issues": SearchIssuesInput,
    "tools.get_issue": GetIssueInput,
    "get_issue": GetIssueInput,
    "tools.summarize_issue": SummarizeIssueInput,  # fix: was GetIssueInput
    "summarize_issue": SummarizeIssueInput,        # fix: was GetIssueInput
}


@app.post("/jsonrpc")
async def jsonrpc(request: Request):
    # Parse JSON body
    try:
        payload = await request.json()
    except Exception:
        # JSON-RPC spec: transport stays 200; error object conveys failure.
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": JSONRPC_PARSE_ERROR, "message": "Parse error"}},
            status_code=200,
        )

    id_ = payload.get("id")
    if payload.get("jsonrpc") != "2.0" or "method" not in payload:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": id_, "error": {"code": JSONRPC_INVALID_REQUEST, "message": "Invalid Request"}},
            status_code=200,
        )

    # Normalize method name to support both "tools/list" and "tools.list"
    method = payload["method"].replace("/", ".")
    params = payload.get("params", {}) or {}

    handler = METHOD_MAP.get(method)
    if not handler:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": id_, "error": {"code": JSONRPC_METHOD_NOT_FOUND, "message": "Method not found"}},
            status_code=200,
        )

    # Validate params if model is known
    try:
        if method in MODEL_MAP:
            model_cls = MODEL_MAP[method]
            if isinstance(params, list):
                obj = model_cls.parse_obj(params[0] if params else {})
            else:
                obj = model_cls.parse_obj(params or {})
            # call handler (may be async)
            if asyncio_callable(handler):
                result_obj = await handler(obj)
            else:
                result_obj = handler(obj)
        else:
            # handler may accept dict or None
            if asyncio_callable(handler):
                result_obj = await handler(params)
            else:
                result_obj = handler(params)
    except ValidationError as ve:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": id_,
                "error": {"code": JSONRPC_INVALID_PARAMS, "message": "Invalid params", "data": ve.errors()},
            },
            status_code=200,
        )
    except HTTPException as he:
        detail = getattr(he, "detail", str(he))
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": id_,
                "error": {"code": JSONRPC_PROVIDER_ERROR, "message": "Provider error", "data": detail},
            },
            status_code=200,
        )
    except Exception as e:
        logger.exception("JSON-RPC handler unexpected error: %s", e)
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": id_,
                "error": {
                    "code": JSONRPC_INTERNAL_ERROR,
                    "message": "Internal error",
                    "data": {"type": type(e).__name__, "message": str(e)},
                },
            },
            status_code=200,
        )

    # Normalize return value
    try:
        result = _pydantic_to_json(result_obj)
    except Exception:
        result = result_obj

    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result}, status_code=200)


def asyncio_callable(fn):
    """Why: handlers can be sync or async; detect both cases."""
    import inspect

    return inspect.iscoroutinefunction(fn) or inspect.isawaitable(fn)


# -------- health --------
@app.get("/healthz")
async def healthz():
    return {"ok": True}
