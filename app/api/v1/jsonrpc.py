#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Dict, Optional, Union
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError, BaseModel
import inspect
from services.jira import METHOD_MAP, MODEL_MAP

router = APIRouter()
logger = logging.getLogger("jira-mcp.jsonrpc")


# JSON-RPC error codes (kept local)
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_PROVIDER_ERROR = -32000

def jsonrpc_error(code: int, message: str, request_id: Optional[Union[str, int]] = None, data: Any = None):
    """Wrap an error in a JSON-RPC 2.0 response envelope."""
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}

def pydantic_to_json(obj: Any) -> Any:
    """
    Convert Pydantic models (v1 or v2) to plain JSON-serializable structures.
    Leaves non-Pydantic objects untouched.
    """
    try:
        if isinstance(obj, BaseModel):
            # pydantic v2
            if hasattr(obj, "model_dump"):
                return obj.model_dump()
            # pydantic v1
            return obj.dict()
    except Exception:
        # Fall through to default return
        pass
    return obj


def asyncio_callable(fn: Any) -> bool:
    """
    Return True if fn is awaitable/async function.
    Accepts coroutine functions and callable objects returning awaitables.
    """
    try:
        if inspect.iscoroutinefunction(fn):
            return True
        # For callable objects, check their __call__ implementation
        if hasattr(fn, "__call__") and inspect.iscoroutinefunction(getattr(fn, "__call__")):
            return True
        return False
    except Exception:
        return False

@router.post("/jsonrpc")
async def jsonrpc(request: Request) -> JSONResponse:
    """
    Minimal JSON-RPC 2.0 dispatcher.
    - Always returns HTTP 200 with a JSON-RPC result or error envelope.
    - Validates params with Pydantic models from MODEL_MAP when present.
    - Dispatches to functions in METHOD_MAP (which may be sync or async).
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(jsonrpc_error(JSONRPC_PARSE_ERROR, "Parse error", None), status_code=200)

    req_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0" or "method" not in payload:
        return JSONResponse(jsonrpc_error(JSONRPC_INVALID_REQUEST, "Invalid Request", req_id), status_code=200)

    # normalize method name: accept "tools/list" or "tools.list"
    method = payload["method"].replace("/", ".")
    params = payload.get("params", {}) or {}

    handler = METHOD_MAP.get(method)
    if not handler:
        return JSONResponse(jsonrpc_error(JSONRPC_METHOD_NOT_FOUND, "Method not found", req_id), status_code=200)

    try:
        # Validate params with model if present
        if method in MODEL_MAP:
            model_cls = MODEL_MAP[method]
            if isinstance(params, list):
                # positional: take first
                model_in = model_cls.parse_obj(params[0] if params else {})
            else:
                model_in = model_cls.parse_obj(params or {})
            result_obj = await handler(model_in) if asyncio_callable(handler) else handler(model_in)
        else:
            # no model; pass params as-is
            result_obj = await handler(params) if asyncio_callable(handler) else handler(params)
    except ValidationError as ve:
        return JSONResponse(jsonrpc_error(JSONRPC_INVALID_PARAMS, "Invalid params", req_id, ve.errors()), status_code=200)
    except Exception as e:
        # Distinguish provider (HTTPException) vs internal where possible in services;
        # services should raise HTTPException for upstream Jira errors; treat generically here.
        logger.exception("Handler raised exception for method %s", method)
        return JSONResponse(jsonrpc_error(JSONRPC_INTERNAL_ERROR, "Internal error", req_id, {"type": type(e).__name__, "message": str(e)}), status_code=200)

    # Normalize Pydantic models to plain JSON
    try:
        result = pydantic_to_json(result_obj)
    except Exception:
        result = result_obj

    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result}, status_code=200)