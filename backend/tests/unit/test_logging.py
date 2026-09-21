import json
import logging

from app.core.logging import JsonFormatter, request_id_var


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_emits_one_json_object_with_core_fields() -> None:
    out = json.loads(JsonFormatter().format(_record("hi")))
    assert out["message"] == "hi" and out["level"] == "INFO" and out["logger"] == "t"
    assert "request_id" not in out


def test_includes_request_id_and_extra_fields() -> None:
    token = request_id_var.set("req-12345678")
    try:
        out = json.loads(JsonFormatter().format(_record(status=200, method="GET")))
    finally:
        request_id_var.reset(token)
    assert out["request_id"] == "req-12345678"
    assert out["status"] == 200 and out["method"] == "GET"


def test_includes_exception_text() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = _record()
        rec.exc_info = sys.exc_info()
    assert "ValueError: boom" in json.loads(JsonFormatter().format(rec))["exc"]
