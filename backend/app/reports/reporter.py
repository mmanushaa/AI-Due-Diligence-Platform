"""The Reporter agent: the executive summary (step 3.9).

The summary is written by rules, not by a model. That is a deliberate choice for this
section: a model asked to summarise findings will produce prose that reads better and
cites worse, and the requirement here is that *every* claim carries the id of a stored
finding. Rules can guarantee that; a prompt cannot. The writing is still derived only
from the stored rows passed in - findings, the risk assessment and the review decision -
and never from anything the run did not record.

:func:`write_summary` returns claims; :func:`validate_citations` is the separate check
that every cited id exists, so the guarantee is verified rather than assumed.
"""

from __future__ import annotations

from app.reports.document import Claim, DroppedClaim, FindingView

SEVERITY_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
LOUD = {"critical", "high"}


def _by_severity(findings: list[FindingView]) -> list[FindingView]:
    return sorted(
        findings,
        key=lambda f: (SEVERITY_RANK.get(f.severity, 0), f.confidence or 0),
        reverse=True,
    )


def write_summary(findings: list[FindingView]) -> list[Claim]:
    """Turn stored findings into cited sentences.

    A claim is only produced when it has findings to cite. An audit with nothing to
    report gets an explicit sentence elsewhere; inventing a citation for "everything is
    fine" is exactly the failure this step is guarding against.
    """
    claims: list[Claim] = []
    actionable = [f for f in findings if f.status != "rejected_false_positive"]
    loud = _by_severity([f for f in actionable if f.severity in LOUD])
    medium = [f for f in actionable if f.severity == "medium"]
    unresolved = [f for f in findings if f.status == "unresolved"]
    rejected = [f for f in findings if f.status == "rejected_false_positive"]

    if loud:
        claims.append(
            Claim(
                text=(
                    f"{len(loud)} finding(s) need attention before closing: "
                    + ", ".join(f"{f.severity} — {f.title}" for f in loud[:3])
                    + (" …" if len(loud) > 3 else "")
                ),
                finding_ids=tuple(f.id for f in loud),
            )
        )
    if medium:
        claims.append(
            Claim(
                text=f"{len(medium)} medium-severity finding(s) are recorded and should be scheduled.",
                finding_ids=tuple(f.id for f in medium),
            )
        )
    if unresolved:
        claims.append(
            Claim(
                text=(
                    f"{len(unresolved)} finding(s) could not be verified against the code and are "
                    "listed under limitations."
                ),
                finding_ids=tuple(f.id for f in unresolved),
            )
        )
    if rejected:
        claims.append(
            Claim(
                text=f"{len(rejected)} candidate finding(s) were rejected as false positives.",
                finding_ids=tuple(f.id for f in rejected),
            )
        )

    verified = [f for f in actionable if f.status in {"verified", "scanner_confirmed"}]
    if verified and len(verified) != len(actionable):
        claims.append(
            Claim(
                text=(
                    f"{len(verified)} of {len(actionable)} actionable finding(s) carry evidence "
                    "that was checked against the source."
                ),
                finding_ids=tuple(f.id for f in actionable),
            )
        )
    return claims


def validate_citations(
    claims: list[Claim], known_ids: set[str]
) -> tuple[list[Claim], list[DroppedClaim]]:
    """Split claims into those whose ids all exist and those that cite an invention.

    A claim is kept only if **every** id it cites exists: a sentence supported by one
    real finding and one invented one is still a sentence that partly rests on nothing.
    """
    kept: list[Claim] = []
    dropped: list[DroppedClaim] = []
    for claim in claims:
        unknown = tuple(fid for fid in claim.finding_ids if fid not in known_ids)
        if unknown:
            dropped.append(DroppedClaim(text=claim.text, unknown_ids=unknown))
        elif claim.cites:
            kept.append(claim)
    return kept, dropped
