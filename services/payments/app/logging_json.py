"""JSON log lines: timestamp, level, service, env, trace_id, span_id, message, and order_id
whenever an order is in scope (T083)."""

import json
import logging
import sys
from datetime import UTC, datetime

from opentelemetry import trace

from app.telemetry import current_tags

_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, env: str):
        super().__init__()
        self.service = service
        self.env = env

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        body = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "service": self.service,
            "env": self.env,
            "trace_id": f"{ctx.trace_id:032x}" if ctx.is_valid else "",
            "span_id": f"{ctx.span_id:016x}" if ctx.is_valid else "",
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and key not in body:
                body[key] = value
        for key, value in current_tags().items():
            body.setdefault(key, value)
        if record.exc_info:
            body["exception"] = self.formatException(record.exc_info)
        return json.dumps(body, default=str)


def configure_logging(service: str, env: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, env))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
