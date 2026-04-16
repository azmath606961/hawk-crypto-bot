"""General utility helpers — retry, time, formatting."""
from __future__ import annotations

import functools
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def ts_ms() -> int:
    return int(time.time() * 1000)


def fmt_price(price: float, decimals: int = 2) -> str:
    return f"{price:,.{decimals}f}"


def fmt_pct(value: float, decimals: int = 2) -> str:
    return f"{value:+.{decimals}f}%"


def round_step(value: float, step: float) -> float:
    """Round value down to nearest step (exchange lot-size compliance)."""
    if step <= 0:
        return value
    return float(int(value / step) * step)


def retry(
    max_attempts: int = 3,
    delay_seconds: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """Decorator: retry on specified exceptions with exponential back-off."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            wait = delay_seconds
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == max_attempts:
                        log.error(
                            "retry(%s) exhausted after %d attempts: %s",
                            func.__name__,
                            max_attempts,
                            exc,
                        )
                        raise
                    log.warning(
                        "retry(%s) attempt %d/%d failed: %s — retrying in %.1fs",
                        func.__name__,
                        attempt,
                        max_attempts,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                    wait *= backoff

        return wrapper  # type: ignore[return-value]

    return decorator


def safe_divide(numerator: float, denominator: float, fallback: float = 0.0) -> float:
    return numerator / denominator if denominator != 0 else fallback
