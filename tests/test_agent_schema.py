"""Step 2.6h: the crew reply reader, tested on the shapes a model actually sends.

A crew stage used to refuse anything that was not the bare object, and real
replies arrive fenced or after a sentence of prose. These cases pin the line the
reader draws: wrapping is forgiven, ambiguity and schema violations are not.
"""

from __future__ import annotations

import json

import pytest

from app.agents.schema import InvalidOutput, parse_crew_report

PLAIN = '{"findings": []}'


def test_plain_json_is_read():
    assert parse_crew_report(PLAIN).findings == []


def test_fenced_json_is_read():
    assert parse_crew_report('```json\n{"findings": []}\n```').findings == []


def test_prose_plus_json_is_read():
    text = 'Here is the contract:\n{"findings": []}\nThat is everything I found.'

    assert parse_crew_report(text).findings == []


def test_two_objects_are_refused():
    """Two answers cannot be scored, so neither is guessed at."""
    with pytest.raises(InvalidOutput) as excinfo:
        parse_crew_report('{"findings": []}\n\n{"findings": []}')

    assert "more than one" in str(excinfo.value)


def test_malformed_json_is_refused():
    with pytest.raises(InvalidOutput):
        parse_crew_report('{"findings": [}')

    with pytest.raises(InvalidOutput):
        parse_crew_report('{"findings": []')  # never closes


def test_a_reply_with_no_object_is_refused():
    with pytest.raises(InvalidOutput):
        parse_crew_report("I could not decide.")


def test_an_empty_reply_is_refused():
    with pytest.raises(InvalidOutput):
        parse_crew_report("")


def test_a_schema_violation_is_still_refused():
    """The schema is unchanged: a category outside the closed set is a refusal."""
    with pytest.raises(InvalidOutput):
        parse_crew_report('{"findings": [{"category": "performance"}]}')


def test_braces_inside_a_string_do_not_end_the_object():
    """A template literal in the reasoning must not cut the object short."""
    payload = {
        "findings": [
            {
                "category": "security",
                "title": "SQL built by template interpolation",
                "file": "src/db/index.js",
                "line_start": 17,
                "line_end": 17,
                "severity": "high",
                "confidence": 0.9,
                "reasoning": "The query is built from a `SELECT ... {'id': x}` template.",
                "recommendation": "Use bound parameters.",
            }
        ]
    }

    report = parse_crew_report("```json\n" + json.dumps(payload) + "\n```")

    assert report.findings[0].line_start == 17
