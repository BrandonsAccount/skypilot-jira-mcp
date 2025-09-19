# logging_setup.py
"""
WHAT:
  A tiny logging layer with:
    - Production mode: JSON logs to stdout (machine-friendly).
    - Dev mode: pretty, colored, emoji logs to stdout (human-friendly).
    - Simple helpers: header/info/success/waiting/warn/error/debug.
    - Context binding: bind(request_id="...") etc., appended as structured fields.
    - Env-driven level toggling (DEBUG/LOG_LEVEL).

WHY:
  - 12-factor apps prefer stdout; infra (Docker/K8s/systemd) handles collection.
  - JSON in prod fits ELK/Datadog/Grafana/Loki; pretty logs help local dev.
  - The helpers give a clean, opinionated surface (icons!) without another dep.
"""
from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict

# ───────────────────────────────
# Environment & level resolution
# ───────────────────────────────

def _env_is_production() -> bool:
    """
    WHAT: Decide which formatter to use.
    WHY: Keep prod logs machine-friendly, dev logs human-friendly.
    """
    env = (os.getenv("APP_ENV") or os.getenv("ENV") or "development").lower()
    return env in {"prod", "production"}

def _env_level() -> str:
    """
    WHAT: Resolve effective log level.
    WHY: DEBUG=1 is the quick override; otherwise LOG_LEVEL wins; default INFO.
    """
    if os.getenv("DEBUG", "").lower() in {"1", "true", "yes", "on"}:
        return "DEBUG"
    return (os.getenv("LOG_LEVEL") or "INFO").upper()

# ───────────────────────────────
# Formatters
# ───────────────────────────────

class JSONFormatter(logging.Formatter):
    """
    WHAT: Render each log record as compact JSON.
    WHY: Ideal for production ingestion by log processors.
    """
    _skip = {
        "name","msg","args","levelname","levelno","pathname","filename","module",
        "exc_info","exc_text","stack_info","lineno","funcName","created","msecs",
        "relativeCreated","thread","threadName","processName","process"
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Merge in context/extra fields (from LoggerAdapter or extra=)
        for k, v in record.__dict__.items():
            if k in self._skip or k in payload:
                continue
            try:
                json.dumps(v)  # ensure serializable
                payload[k] = v
            except Exception:
                payload[k] = repr(v)

        if record.exc_info:
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc_type"] = exc_type
            payload["exc_message"] = str(record.exc_info[1]) if record.exc_info[1] else None

        return json.dumps(payload, separators=(",", ":"))

class DevFormatter(logging.Formatter):
    """
    WHAT: Pretty, emoji, colorized one-line logs for local dev.
    WHY: Fast visual scanning while keeping context visible.
    """
    # minimal ANSI (no external deps)
    COLORS = {
        "RESET":  "\x1b[0m",
        "BOLD":   "\x1b[1m",
        "WHITE":  "\x1b[37m",
        "YELLOW": "\x1b[33m",
        "RED":    "\x1b[31m",
        "GREEN":  "\x1b[32m",
        "CYAN":   "\x1b[36m",
        "MAGENTA":"\x1b[35m",
    }

    ICONS = {
        "HEADER":  ("🧠", "MAGENTA", True),
        "INFO":    ("🛈",  "WHITE",   False),
        "SUCCESS": ("✔",   "GREEN",   False),
        "WAITING": ("⏳",  "CYAN",    False),
        "WARNING": ("⚠️",  "YELLOW",  False),
        "ERROR":   ("❌",  "RED",     True),
        "DEBUG":   ("🐞",  "WHITE",   False),
    }

    def format(self, record: logging.LogRecord) -> str:
        c = self.COLORS
        # pick icon & color based on either explicit 'icon' or level
        icon = getattr(record, "icon", None)
        if not icon:
            # derive from level if not given
            if record.levelno >= logging.ERROR:
                icon, color, bold = self.ICONS["ERROR"]
            elif record.levelno >= logging.WARNING:
                icon, color, bold = self.ICONS["WARNING"]
            elif record.levelno == logging.DEBUG:
                icon, color, bold = self.ICONS["DEBUG"]
            else:
                icon, color, bold = self.ICONS["INFO"]
        else:
            # if icon provided, try to pick color from 'icon_level' hint
            icon_level = (getattr(record, "icon_level", "") or "").upper()
            color = self.ICONS.get(icon_level, self.ICONS["INFO"])[1]
            bold = self.ICONS.get(icon_level, self.ICONS["INFO"])[2]

        color_seq = c[color]
        maybe_bold = c["BOLD"] if bold else ""
        reset = c["RESET"]

        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        base = f"{color_seq}{maybe_bold}{icon}{reset} {record.getMessage()}"

        # render a compact context suffix for any extra fields that aren't standard
        skip = JSONFormatter._skip | {"icon", "icon_level"}
        extras = {k: v for k, v in record.__dict__.items() if k not in skip}
        if extras:
            try:
                ctx = json.dumps(extras, separators=(",", ":"), ensure_ascii=False)
            except Exception:
                ctx = str(extras)
            base = f"{base} {c['WHITE']}{ctx}{reset}"

        # prepend time + level tag for quick scanning
        return f"{c['WHITE']}{ts}{reset} [{record.levelname}] {base}"

# ───────────────────────────────
# Configuration
# ───────────────────────────────

_configured = False

def configure_logging(level: str | None = None) -> None:
    """
    WHAT: Idempotently configure root logger to stdout with prod/dev formatting.
    WHY: Single place to set handlers/formatters; safe to call repeatedly.
    """
    global _configured
    if _configured:
        return

    lvl = level or _env_level()
    root = logging.getLogger()
    root.setLevel(lvl)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(lvl)
    handler.setFormatter(JSONFormatter() if _env_is_production() else DevFormatter())

    root.handlers.clear()          # avoid duplicates on reloads
    root.addHandler(handler)
    logging.captureWarnings(True)  # route warnings.warn → logging
    _configured = True

def get_logger(name: str | None = None) -> logging.Logger:
    """
    WHAT: Return a module logger, ensuring config is applied.
    WHY: Centralized, consistent logging setup across the codebase.
    """
    configure_logging()
    return logging.getLogger(name or "app")

def bind(logger: logging.Logger, **extra) -> logging.LoggerAdapter:
    """
    WHAT: Attach structured context (request_id, user_id, etc.) to all logs.
    WHY: No need to pass the same fields to every call; stays in JSON/pretty output.
    """
    return logging.LoggerAdapter(logger, extra)

# ───────────────────────────────
# Convenience API (icons + levels)
# ───────────────────────────────

# Map friendly methods to logging levels + icon hints.
# WHY: mirrors your existing ergonomics without an extra dependency.
_ICON_LEVEL_HINT = {
    "header":  ("INFO",   "🧠", "HEADER"),
    "info":    ("INFO",   "🛈",  "INFO"),
    "success": ("INFO",   "✔",   "SUCCESS"),
    "waiting": ("INFO",   "⏳",  "WAITING"),
    "warn":    ("WARNING","⚠️",  "WARNING"),
    "error":   ("ERROR",  "❌",  "ERROR"),
    "debug":   ("DEBUG",  "🐞",  "DEBUG"),
}

class FancyLogger:
    """
    WHAT: Thin wrapper around Logger/LoggerAdapter offering icon’d helpers.
    WHY: Keeps call sites tiny & expressive while preserving structured fields.
    """
    def __init__(self, logger: logging.Logger | logging.LoggerAdapter):
        self._log = logger

    def _emit(self, level_key: str, msg: str, **extra):
        level_name, icon, hint = _ICON_LEVEL_HINT[level_key]
        level_no = getattr(logging, level_name)
        # icon & hint become fields for both Dev and JSON formatters
        extra = {**extra, "icon": icon, "icon_level": hint}
        if isinstance(self._log, logging.LoggerAdapter):
            self._log.log(level_no, msg, extra=extra)
        else:
            self._log.log(level_no, msg, extra=extra)

    # public API
    def header(self, msg: str, icon: str | None = None, **extra):
        icon_override = icon or _ICON_LEVEL_HINT["header"][1]
        self._emit("header", msg, icon=icon_override, **extra)

    def info(self, msg: str, **extra):    self._emit("info", msg, **extra)
    def success(self, msg: str, **extra): self._emit("success", msg, **extra)
    def waiting(self, msg: str, **extra): self._emit("waiting", msg, **extra)
    def warn(self, msg: str, **extra):    self._emit("warn", msg, **extra)
    def error(self, msg: str, **extra):   self._emit("error", msg, **extra)
    def debug(self, msg: str, **extra):   self._emit("debug", msg, **extra)

def fancy_logger(name: str | None = None, **context) -> FancyLogger:
    """
    WHAT: Create a FancyLogger with optional bound context.
    WHY: One-liner to get a ready-to-use, contextual, icon’d logger.
    """
    base = get_logger(name)
    return FancyLogger(bind(base, **context) if context else base)
