"""Structured logging bound to (run_id, check_id) via LoggerAdapter."""

from __future__ import annotations

import logging


def bound_logger(run_id: str, check_id: str | None = None) -> logging.LoggerAdapter[logging.Logger]:
    extra = {"run_id": run_id, **({"check_id": check_id} if check_id else {})}
    return logging.LoggerAdapter(logging.getLogger("digital_twin"), extra)
