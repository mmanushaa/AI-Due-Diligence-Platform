"""Step 2.6b: the instrumentation columns and the extras table exist as models.

These assertions read the SQLAlchemy metadata only, so they run without a
database and still fail if a column is renamed or the EvalExtras class is
dropped from the module (which is what happened last time: it was listed in
``__all__`` while no class had been written).
"""

from app.db.models import EvalExtras, EvalResult

MATCHED_FINDING_COLUMNS = {
    "matched_finding_id",
    "matched_origin",
    "matched_source_tool",
    "matched_file",
    "matched_line_start",
    "matched_line_end",
    "is_cross_file",
    "miss_reason",
}

EXTRA_COLUMNS = {
    "id",
    "created_at",
    "eval_run_id",
    "finding_id",
    "repo",
    "category",
    "title",
    "file",
    "line_start",
    "line_end",
    "origin",
    "source_tool",
    "rejection_reason",
}


def test_eval_result_carries_the_matched_finding_columns() -> None:
    columns = set(EvalResult.__table__.columns.keys())
    assert MATCHED_FINDING_COLUMNS <= columns


def test_eval_extras_maps_the_table() -> None:
    assert EvalExtras.__tablename__ == "eval_extras"
    columns = set(EvalExtras.__table__.columns.keys())
    assert EXTRA_COLUMNS <= columns


def test_both_tables_hang_off_an_eval_run() -> None:
    for model in (EvalResult, EvalExtras):
        run_fk = next(
            fk for fk in model.__table__.foreign_keys if fk.parent.name == "eval_run_id"
        )
        assert run_fk.column.table.name == "eval_runs"
        assert run_fk.ondelete == "CASCADE"


def test_a_pruned_finding_leaves_the_row_in_place() -> None:
    """The benchmark numbers must outlive the findings they describe."""
    for model, column in ((EvalResult, "matched_finding_id"), (EvalExtras, "finding_id")):
        finding_fk = next(
            fk for fk in model.__table__.foreign_keys if fk.parent.name == column
        )
        assert finding_fk.column.table.name == "findings"
        assert finding_fk.ondelete == "SET NULL"
