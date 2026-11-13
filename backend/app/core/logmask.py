"""Mask secrets in every log line a process emits (step 2.11).

A prompt is not the only place a repository's credential can surface. A model
reply that quotes the code, a validator that echoes a snippet, a scanner error that
carries the matched line - all of them reach a log, and a log is copied into bug
reports. This module installs one filter on the handlers that will actually receive
the record, rather than on a logger, because a filter on a logger only sees records
created by that logger and not those of its children.

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


def install_log_masking(handlers: Iterable[logging.Handler] | None = None) -> None:
    """Attach the filter to every handler that will emit a line.

    Called with no argument it covers the root logger's handlers; a test can pass
    its own capture handler instead. Handlers added after the call are the caller's
    to wrap, which is why the API installs this once logging is configured.
    """
    targets = list(handlers) if handlers is not None else list(logging.getLogger().handlers)
    for handler in targets:
        if not any(isinstance(item, SecretMaskFilter) for item in handler.filters):
            handler.addFilter(SecretMaskFilter())
        handler.setFormatter(MaskingFormatter(handler.formatter._fmt or "%(message)s"))
