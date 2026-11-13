"""Mask secrets in every log line a process emits (step 2.11).

A prompt is not the only place a repository's credential can surface. A model
reply that quotes the code, a validator that echoes a snippet, a scanner error that
carries the matched line - all of them reach a log, and a log is copied into bug
reports. This module installs one filter on the handlers that will actually receive
the record, rather than on a logger, because a filter on a logger only sees records
created by that logger and not those of its children.

It is installed by the entry point, once logging is configured: the API's lifespan
startup and ``benchmark.harness.__main__.main``. Calling it at import time masked
the handlers that happened to exist then - usually none - and every handler created
afterwards wrote unmasked lines, which is the opposite of the guarantee.

``install_log_masking`` is idempotent and safe to call from the API, the worker and
the benchmark harness.
"""

from __future__ import annotations

import logging
from typing import Iterable

from app.scanners.common import redact_text


def mask_secrets(text: str) -> str:
    """The masker itself, so a call site can mask before formatting."""
    return redact_text(text or "")


class SecretMaskFilter(logging.Filter):
    """Rewrite one record so its rendered line carries no recognisable secret."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format must not drop the log
            return True
        masked = mask_secrets(message)
        if masked != message:
            record.msg = masked
            record.args = ()
        if record.exc_info:
            # The traceback is formatted later; leave it to the formatter, which
            # this module also wraps.
            pass
        return True


class MaskingFormatter(logging.Formatter):
    """A formatter that masks the finished line, traceback included."""

    def format(self, record: logging.LogRecord) -> str:
        return mask_secrets(super().format(record))


def _mask_handler(handler: logging.Handler) -> None:
    """Filter one handler and keep its format, date format and style."""
    if not any(isinstance(item, SecretMaskFilter) for item in handler.filters):
        handler.addFilter(SecretMaskFilter())
    if isinstance(handler.formatter, MaskingFormatter):
        return
    existing = handler.formatter
    fmt = getattr(existing, "_fmt", None) or "%(message)s"
    datefmt = getattr(existing, "datefmt", None)
    handler.setFormatter(MaskingFormatter(fmt, datefmt))


def install_log_masking(handlers: Iterable[logging.Handler] | None = None) -> None:
    """Attach the filter to every handler that will emit a line.

    Called with no argument it covers the root logger's handlers, gives the process
    one handler if it has none (a record with no handler goes to
    ``logging.lastResort``, which no filter on the root logger can reach), and masks
    that fallback too. A test can pass its own capture handler instead.
    """
    if handlers is None:
        if not logging.getLogger().handlers:
            # No level: basicConfig would otherwise reset the process's log level.
            logging.basicConfig()
        targets: list[logging.Handler] = list(logging.getLogger().handlers)
        if isinstance(logging.lastResort, logging.Handler):
            targets.append(logging.lastResort)
    else:
        targets = list(handlers)
    for handler in targets:
        _mask_handler(handler)
