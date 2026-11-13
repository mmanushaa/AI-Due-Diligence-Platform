"""The one place repository text becomes part of a prompt (step 2.11).

Every prompt is assembled here, for the same reason the tools return blocks rather
than strings: a repository is written by someone else, and some of it is written to
be read by the reviewer rather than by a person. Three rules hold for every prompt
this module builds.

    Repository text is data        It goes inside ``<<<DATA ...>>>`` /
                                   ``<<<END_DATA>>>`` blocks that name the file and
                                   the line range, and never into the instruction
                                   prose. A finding is repository text too: it is a
                                   scanner's or an agent's words about the code, so
                                   it is rendered as a block rather than pasted into
                                   a task description.

    The delimiter cannot be closed A file that contains ``<<<END_DATA>>>``, or a
                                   near miss like ``<<<END_DATA`` or ``<<< DATA``,
                                   has it replaced with ``[delimiter removed]``
                                   before it is rendered, so repository text cannot
                                   end the block it arrived in and start speaking as
                                   the system.

    Secrets do not travel          Every prompt is masked whole, not just the file
                                   bodies: a credential that reaches a model reaches
                                   a log line and a trace too.
"""

from __future__ import annotations

import re
from typing import Iterable

from app.agents.tools import DataBlock, FindingView, render_blocks
from app.scanners.common import redact_text

#: Anything shaped like the delimiters, including the near misses a model might
#: still read as one: ``<<<DATA``, ``<<< DATA >>>``, ``<<<END_DATA``, ``<<<<END_DATA>>>>``.
DELIMITER = re.compile(r"<{2,}\s*(?:END[_\s-]*)?DATA(?:\s*>){0,4}", re.IGNORECASE)
NEUTRALISED = "[delimiter removed]"


def mask_secrets(text: str) -> str:
    """Remove known token shapes and a caller's known secret from free text."""
    return redact_text(text or "")


def neutralise_delimiters(text: str) -> str:
    """Defang anything in repository text that could pose as a block boundary."""
    return DELIMITER.sub(NEUTRALISED, text or "")


def data_block_for_text(
    file_path: str, body: str, *, label: str, line_start: int = 1
) -> str:
    """A block for text that is not code: findings, facts, a quoted reply."""
    lines = (body or "").count("\n") + 1
    return DataBlock(
        file_path=file_path,
        line_start=line_start,
        line_end=line_start + lines - 1,
        label=label,
        body=neutralise_delimiters(mask_secrets(body)),
    ).render()


def findings_block(findings: Iterable[FindingView]) -> str:
    """Scanner or agent findings as one data block."""
    listed = "\n".join(view.render() for view in findings)
    if not listed:
        return ""
    return data_block_for_text("findings", listed, label="known findings")


def facts_block(facts: str) -> str:
    """Deterministic facts about the repository (the module graph) as one block."""
    return data_block_for_text("module-graph", facts, label="facts") if facts else ""


def build_prompt(
    *,
    instructions: str,
    blocks: tuple[DataBlock, ...] = (),
    findings: Iterable[FindingView] = (),
    facts: str | None = None,
    closing: str | None = None,
) -> str:
    """The instructions, then the repository, then the contract - in that order.

    ``instructions`` must not contain repository text: that is the caller's side of
    the contract this module exists to enforce. Everything repository-derived goes
    through ``blocks``, ``findings`` or ``facts`` and therefore arrives in a block.
    """
    parts: list[str] = [mask_secrets(instructions)]
    if facts:
        parts.append(facts_block(facts))
    listed = findings_block(findings)
    if listed:
        parts.append(listed)
    if blocks:
        parts.append(render_blocks(tuple(blocks)))
    if closing:
        parts.append(mask_secrets(closing))
    return mask_secrets("\n\n".join(part for part in parts if part))
