#!/usr/bin/env python3
from fastapi import FastAPI, Request
from api.v1 import jsonrpc
import inspect

app = FastAPI()
app.include_router(jsonrpc.router, prefix="/api/v1", tags=["jsonrpc"])