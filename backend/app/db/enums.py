"""PostgreSQL enum types from docs/5. Backend Schema.md.

Named enums (severity, category, verification_status, verdict, audit_status,
stage, user_role, review_decision) are the ones the schema tab names. The
inline value lists that the tab specifies without a type name are named here
after their column or role, and every name is recorded in the migration.
"""

import enum

from sqlalchemy import Enum as SAEnum

# --- enums named in the schema tab -----------------------------------------


class UserRole(str, enum.Enum):
    ANALYST = "analyst"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class AuditStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    NEEDS_EVIDENCE = "needs_evidence"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stage(str, enum.Enum):
    INTAKE = "intake"
    INDEX = "index"
    SCANNERS = "scanners"
    AGENTS = "agents"
    VALIDATION = "validation"
    RISK = "risk"
    REVIEW = "review"
    REPORT = "report"


class Severity(str, enum.Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Category(str, enum.Enum):
    SECURITY = "security"
    ARCHITECTURE = "architecture"
    LICENSE = "license"
    DEPENDENCY = "dependency"
    SECRET = "secret"


class VerificationStatus(str, enum.Enum):
    SCANNER_CONFIRMED = "scanner_confirmed"
    AI_SUGGESTED = "ai_suggested"
    VERIFIED = "verified"
    REJECTED_FALSE_POSITIVE = "rejected_false_positive"
    UNRESOLVED = "unresolved"


class Verdict(str, enum.Enum):
    ISSUE = "issue"
    NO_ISSUE = "no_issue"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ReviewDecision(str, enum.Enum):
    APPROVE = "approve"
    OVERRIDE = "override"
    REQUEST_EVIDENCE = "request_evidence"


# --- enums whose values the schema tab lists inline ------------------------


class SourceType(str, enum.Enum):
    ZIP = "zip"
    GIT_URL = "git_url"


class SymbolKind(str, enum.Enum):
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    ROUTE = "route"
    IMPORT = "import"
    CONFIG = "config"


class ScannerTool(str, enum.Enum):
    GITLEAKS = "gitleaks"
    SEMGREP = "semgrep"
    OSV = "osv"
    SCANCODE = "scancode"
    AST = "ast"


class ScannerRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class FindingOrigin(str, enum.Enum):
    SCANNER = "scanner"
    AGENT = "agent"


class EvidenceKind(str, enum.Enum):
    PRIMARY = "primary"
    SUPPORTING = "supporting"
    RETRIEVED_CONTEXT = "retrieved_context"


class AssessmentStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    FINAL = "final"


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    OVERRIDDEN = "overridden"
    NEEDS_EVIDENCE = "needs_evidence"


class AgentName(str, enum.Enum):
    SECURITY = "security"
    ARCHITECTURE = "architecture"
    COMPLIANCE = "compliance"
    REPORTER = "reporter"


class EvalConfig(str, enum.Enum):
    SCANNERS_ONLY = "scanners_only"
    SINGLE_AGENT = "single_agent"
    MULTI_AGENT = "multi_agent"
    MULTI_AGENT_VALIDATED = "multi_agent_validated"


def pg_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Native Postgres enum that stores the string values, not member names."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )
