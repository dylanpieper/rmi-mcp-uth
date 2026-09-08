"""Layer 1 — the envelope contract, parametrized over every tool.

One shape for every call. A caller building a CSV from any tool's payload
depends on all of this, and none of it is checked at runtime except by a bare
`assert` in `respond()` that vanishes under `python -O`.
"""

import json

import pytest

from rmi_mcp_uth.response import NON_ADDITIVE

from .conftest import NOTE_KINDS, TOOL_CALLS

pytestmark = pytest.mark.data

ENVELOPE_KEYS = {"rows", "units", "meta", "notes", "error"}


@pytest.fixture(params=TOOL_CALLS, ids=lambda c: f"{c[0]}-{'-'.join(map(str, c[1].values()))}")
def envelope(request, envelopes):
    name, arguments = request.param
    return envelopes[(name, tuple(sorted(arguments.items())))]


async def test_keys_are_exactly_the_envelope(envelope):
    assert set(envelope) == ENVELOPE_KEYS


async def test_error_and_rows_are_mutually_exclusive(envelope):
    assert (envelope["error"] is not None) == (envelope["rows"] == [])


async def test_every_row_carries_the_same_keys(envelope):
    rows = envelope["rows"]
    if not rows:
        pytest.skip("no rows")
    first = set(rows[0])
    assert all(set(row) == first for row in rows)


async def test_units_keys_are_row_columns(envelope):
    rows = envelope["rows"]
    if not rows:
        pytest.skip("no rows")
    assert set(envelope["units"]) <= set(rows[0])


async def test_non_additive_is_a_subset_of_the_columns(envelope):
    rows = envelope["rows"]
    if not rows:
        pytest.skip("no rows")
    declared = set(envelope["meta"].get("non_additive", []))
    assert declared <= set(rows[0])
    # Nothing may be declared non-additive that the module does not know about;
    # a typo here would silently stop warning about a real rate column.
    assert declared <= {c.lower() for c in NON_ADDITIVE} | set(NON_ADDITIVE)


async def test_grain_columns_exist_in_the_rows(envelope):
    """`respond()` guards this with a bare assert, which -O removes."""
    rows = envelope["rows"]
    grain = envelope["meta"].get("grain")
    if not rows or not grain:
        pytest.skip("no rows or no grain")
    assert set(grain) <= set(rows[0])


async def test_grain_is_actually_unique(envelope):
    """The one envelope claim a caller cannot verify, and the likeliest to rot."""
    rows = envelope["rows"]
    grain = envelope["meta"].get("grain")
    if not rows or not grain:
        pytest.skip("no rows or no grain")
    keys = [tuple(json.dumps(row[c], sort_keys=True) for c in grain) for row in rows]
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, f"grain {grain} repeats: {sorted(duplicates)[:3]}"


async def test_response_serializes(envelope):
    """Guards jsonable() against numpy scalars, LIST columns, NaN and NaT."""
    json.dumps(envelope)


async def test_note_kinds_are_known(envelope):
    kinds = {note["kind"] for note in envelope["notes"]}
    assert kinds <= NOTE_KINDS


async def test_error_envelope_is_empty_everywhere_else(call):
    """When `error` is set, nothing else carries data."""
    envelope = await call("preview_table", table_name="does_not_exist")
    assert envelope["error"]["message"]
    assert envelope["rows"] == []
    assert envelope["units"] == {}
    assert envelope["meta"] == {}
    assert envelope["notes"] == []


async def test_grain_is_null_where_the_caller_wrote_the_shape(call):
    """query_data and preview_table promise no key, and must say so rather than guess."""
    for envelope in (
        await call("query_data", sql="SELECT 1 AS a, 1 AS b"),
        await call("preview_table", table_name="emissions_targets"),
    ):
        assert envelope["meta"]["grain"] is None
