#!/usr/bin/env python3
from __future__ import annotations

import os
import logging
from typing import Any, Dict, List, Optional
import httpx
from pydantic import BaseModel, Field
from base64 import b64encode, b64decode

logger = logging.getLogger("jira-mcp.services")

# config from env
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_TOKEN = os.getenv("JIRA_TOKEN", "")
JIRA_USER = os.getenv("JIRA_USER", "")
ALLOW_RAW_JQL = os.getenv("ALLOW_RAW_JQL", "false").lower() == "true"

if not JIRA_BASE_URL:
    raise RuntimeError("JIRA_BASE_URL must be set")
if not JIRA_TOKEN:
    raise RuntimeError("JIRA_TOKEN must be set")

# httpx defaults
TIMEOUT = httpx.Timeout(10.0, read=10.0)
RETRY_STATUSES = {429, 500, 502, 503, 504}


# helpers
async def _sleep(s: float) -> None:
    import asyncio
    await asyncio.sleep(s)


async def jira_request(method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Optional[Any] = None) -> Any:
    url = f"{JIRA_BASE_URL}{path}"
    headers: Dict[str, str] = {}
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
                    await _sleep(backoff); backoff *= 2; continue
                logger.exception("Network error calling Jira %s %s: %s", method, url, exc)
                raise
            if resp.status_code in RETRY_STATUSES and attempt <= 3:
                ra = resp.headers.get("Retry-After"); delay = float(ra) if ra else backoff
                await _sleep(delay); backoff = min(backoff * 2, 5.0); continue
            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                logger.debug("Jira returned %s: %s", resp.status_code, detail)
                # raise an exception; callers will map to JSON-RPC error
                raise RuntimeError(detail)
            try:
                return resp.json()
            except Exception:
                return resp.text


# Pydantic models
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

# small helpers for JQL and redaction
def ensure_project_allowed(project: Optional[str]) -> None:
    return

def build_jql(filters: Optional[Filters]) -> str:
    if not filters:
        return "order by updated desc"
    parts: List[str] = []
    if filters.project:
        ensure_project_allowed(filters.project); parts.append(f"project = {filters.project}")
    if filters.status:
        parts.append(f'status = "{filters.status}"')
    if filters.assignee:
        parts.append(f'assignee = "{filters.assignee}"')
    if filters.labels:
        labels = ",".join([f'"{l}"' for l in filters.labels]); parts.append(f"labels in ({labels})")
    if filters.created_since:
        v = filters.created_since
        if v.endswith("d") and v[:-1].isdigit():
            parts.append(f"created >= -{int(v[:-1])}d")
        else:
            parts.append(f'created >= "{v}"')
    if filters.text:
        safe = filters.text.replace('"', '\\"'); parts.append(f'text ~ "{safe}"')
    return " AND ".join(parts) if parts else "order by updated desc"


def redact(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    import re
    return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<redacted>", text)


# read-only implementations
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
        issues_out.append({
            "key": it.get("key"),
            "summary": fields.get("summary"),
            "status": (fields.get("status") or {}).get("name"),
            "assignee": (fields.get("assignee") or {}).get("displayName") or (fields.get("assignee") or {}).get("accountId"),
            "priority": (fields.get("priority") or {}).get("name"),
            "updated_at": fields.get("updated"),
        })
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
    comments = [{"author": (c.get("author") or {}).get("displayName"), "created": c.get("created"), "body": redact(c.get("body"))} for c in comments_block]
    links_out: List[Dict[str, str]] = []
    for link in fields.get("issuelinks", []) or []:
        if "outwardIssue" in link:
            links_out.append({"type": (link.get("type") or {}).get("name", "relates to"), "key": link["outwardIssue"]["key"]})
        if "inwardIssue" in link:
            links_out.append({"type": (link.get("type") or {}).get("name", "relates to"), "key": link["inwardIssue"]["key"]})
    obj = {
        "key": it.get("key"),
        "summary": fields.get("summary"),
        "description": redact(fields.get("description")),
        "status": (fields.get("status") or {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName") or (fields.get("assignee") or {}).get("accountId"),
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
    data = env.data if isinstance(env, Envelope) else env
    summary = {"key": data.get("key"), "title": data.get("summary"), "status": data.get("status"), "assignee": data.get("assignee"), "last_update": data.get("updated_at"), "url": data.get("url")}
    return Envelope(data=summary)


async def list_tools_internal(_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"tools": [{"name": "search_issues", "description": "Search Jira issues by filters or JQL.", "input_schema": SearchIssuesInput.schema()}, {"name": "get_issue", "description": "Fetch a single issue by key.", "input_schema": GetIssueInput.schema()}, {"name": "summarize_issue", "description": "Return a terse summary for humans.", "input_schema": SummarizeIssueInput.schema()}]}


# ---------------------------------------------------------------------------
# Method and model maps for JSON-RPC dispatcher
# ---------------------------------------------------------------------------
METHOD_MAP: Dict[str, Any] = {
    "initialize": lambda params=None: {"protocol": "mcp-jira-jsonrpc", "version": "1.0"},
    "tools.list": list_tools_internal,
    "tools.search_issues": search_issues_internal,
    "search_issues": search_issues_internal,
    "tools.get_issue": get_issue_internal,
    "get_issue": get_issue_internal,
    "tools.summarize_issue": summarize_issue_internal,
    "summarize_issue": summarize_issue_internal,
}

# Map method names to Pydantic input models where applicable
MODEL_MAP: Dict[str, Any] = {
    "tools.search_issues": SearchIssuesInput,
    "search_issues": SearchIssuesInput,
    "tools.get_issue": GetIssueInput,
    "get_issue": GetIssueInput,
    "tools.summarize_issue": SummarizeIssueInput,
    "summarize_issue": SummarizeIssueInput,
}