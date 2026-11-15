"""make the audit trail append-only

Revision ID: 0006_append_only
Revises: 0005_validator_agent
Create Date: 2026-11-14

Step 3.5. Two tables record what happened - ``audit_log`` and ``review_decisions`` -
and an audit trail that can be edited proves nothing. This migration makes them
append-only twice over:

    triggers      BEFORE UPDATE OR DELETE on both tables raise, so the rule holds
                  for the owner of the table, for a superuser and for the ORM;
    privileges    the application role is granted SELECT and INSERT and has UPDATE,
                  DELETE and TRUNCATE revoked, so a compromised application cannot
                  even attempt it.

The application role is ``due_diligence_app``. It is created without a password: how
it logs in (password, IAM, certificate) is a deployment decision, and putting a
credential in a migration is not.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_append_only"
down_revision: str | None = "0005_validator_agent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "due_diligence_app"
IMMUTABLE_TABLES = ("audit_log", "review_decisions")

TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_trail_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'append-only table: % cannot be updated or deleted (attempted %)',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(TRIGGER_FUNCTION)
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION audit_trail_is_append_only()"
        )
        # A row trigger cannot fire on a statement that touches no row, so an empty
        # ``DELETE FROM audit_log`` would succeed silently. The statement-level
        # trigger refuses the statement itself (and covers TRUNCATE, which bypasses
        # row triggers entirely).
        op.execute(
            f"CREATE TRIGGER {table}_append_only_statement "
            f"BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION audit_trail_is_append_only()"
        )

    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN;
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    for table in IMMUTABLE_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM {APP_ROLE}")


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only_statement ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS audit_trail_is_append_only()")
