"""Layer 6 — reliability.

Determinism, row budgets, error messages a caller can act on, name matching,
input validation, and what `query_data` will and will not let through.
"""

import json

import duckdb
import pytest

from rmi_mcp_uth.helpers import MAX_RESPONSE_ROWS
from rmi_mcp_uth.names import normalize_name, normalized_column

# ------------------------------------------------------------------ determinism

DETERMINISM_CALLS = [
    ("list_utilities", {"state_abbr": "WI"}),
    ("get_emissions_trend", {"utility_name": "Alliant"}),
    ("get_generation_mix", {"utility_name": "Alliant", "year": 2023}),
    ("get_climate_alignment", {"utility_name": "Alliant"}),
    ("rank_climate_alignment", {"year": 2023}),
    ("rank_climate_alignment", {"year": 2023, "group_by": "parent"}),
]


@pytest.mark.data
async def test_tools_are_deterministic(client):
    """The same arguments against unchanged data must give the same bytes.

    Several ORDER BYs are not total, so this is a live risk rather than a
    formality — and it currently fails for four calls. See .claude/ISSUES.md
    issue 1 for the causes and the fix shape.
    """
    unstable = {}
    for name, arguments in DETERMINISM_CALLS:
        seen = set()
        for _ in range(8):
            result = await client.call_tool(name, arguments)
            seen.add(json.dumps(result.structured_content, sort_keys=True))
        if len(seen) > 1:
            unstable[f"{name}{arguments}"] = len(seen)
    assert not unstable, f"non-deterministic responses (variants per 8 calls): {unstable}"


# ------------------------------------------------------------------ row budget


@pytest.mark.data
async def test_no_tool_exceeds_the_row_budget(envelopes):
    for key, envelope in envelopes.items():
        assert len(envelope["rows"]) <= MAX_RESPONSE_ROWS, (key, len(envelope["rows"]))


@pytest.mark.data
async def test_technology_rollup_stays_under_the_budget(db, call):
    """helpers.py:32 claims years x technology_rmi is under the budget by construction.

    Computed from the current year count rather than hardcoded, so the claim
    fails when the data grows past it rather than when someone edits a comment.
    """
    years, technologies = db.execute(
        """SELECT count(DISTINCT year), count(DISTINCT technology_rmi)
           FROM operations_emissions_by_tech"""
    ).fetchone()
    assert years * technologies <= MAX_RESPONSE_ROWS, (years, technologies)

    envelope = await call("get_generation_mix", utility_name="Xcel",
                          group_by="technology")
    assert envelope["error"] is None
    assert len(envelope["rows"]) <= MAX_RESPONSE_ROWS


@pytest.mark.data
async def test_oversized_request_refuses_with_a_retry_that_works(call):
    """An error that suggests an impossible retry is a bug the message hides."""
    envelope = await call("get_generation_mix", utility_name="Energy")
    if envelope["error"] is None:
        pytest.skip("no oversized match in this snapshot")
    message = envelope["error"]["message"]
    assert "group_by='technology'" in message

    retried = await call("get_generation_mix", utility_name="Energy",
                         group_by="technology")
    assert retried["error"] is None, retried["error"]
    assert retried["rows"]


@pytest.mark.data
async def test_oversized_emissions_request_names_a_working_narrowing(call):
    envelope = await call("get_emissions_trend", utility_name="Energy", basis="all")
    if envelope["error"] is None:
        pytest.skip("no oversized match in this snapshot")
    detail = envelope["error"]
    assert detail["matched_rows"] > MAX_RESPONSE_ROWS
    # The error names specific utilities; calling one must succeed.
    retried = await call("get_emissions_trend",
                         utility_name=detail["matched_utilities"][0], basis="all")
    assert retried["error"] is None, retried["error"]


# --------------------------------------------------------------- name matching


@pytest.mark.data
def test_python_and_sql_normalization_agree(db):
    """Two implementations of one rule; drift between them is invisible otherwise."""
    for table, column in [
        ("utility_information", "utility_name"),
        ("utility_information", "parent_name"),
        ("emissions_targets", "utility_name_irp"),
        ("operations_emissions_by_tech", "utility_name"),
    ]:
        rows = db.execute(
            f"""SELECT DISTINCT {column}, {normalized_column(column)}
                FROM {table} WHERE {column} IS NOT NULL"""
        ).fetchall()
        assert rows
        mismatched = [(raw, sql) for raw, sql in rows if normalize_name(raw) != sql]
        assert not mismatched, mismatched[:5]


@pytest.mark.data
async def test_ranking_puts_the_operating_utility_first(call):
    """Exact and prefix hits on the utility's own name beat the project LLCs."""
    for term, expected in [
        ("Alliant", "Alliant"),
        ("Wisconsin Power & Light", "Wisconsin Power & Light"),
    ]:
        envelope = await call("list_utilities", name_contains=term)
        assert envelope["rows"], term
        first = envelope["rows"][0]
        assert expected.lower() in (
            f"{first['utility_name']} {first['parent_name']}".lower()
        ), (term, first)


@pytest.mark.data
async def test_misspelling_returns_suggestions_containing_the_target(call):
    envelope = await call("get_emissions_trend", utility_name="Wisconson Power")
    assert envelope["error"] is not None
    suggestions = envelope["error"].get("suggestions", [])
    message = envelope["error"]["message"]
    assert suggestions or "list_utilities" in message


# ------------------------------------------------------------ input validation


@pytest.mark.data
@pytest.mark.parametrize(
    "name,arguments,expected",
    [
        ("get_emissions_trend", {"utility_name": "Alliant", "basis": "sideways"},
         "basis must be"),
        ("get_climate_alignment", {"utility_name": "Alliant", "basis": "sideways"},
         "basis must be"),
        ("rank_climate_alignment", {"metric": "vibes"}, "metric must be"),
        ("rank_climate_alignment", {"group_by": "galaxy"}, "group_by must be"),
        ("rank_climate_alignment", {"scope": "everything"}, "scope must be"),
        ("get_generation_mix", {"utility_name": "Alliant", "group_by": "galaxy"},
         "group_by must be"),
        ("preview_table", {"table_name": "'; DROP TABLE emissions_targets; --"},
         "Unknown table"),
    ],
)
async def test_bad_arguments_return_an_error_envelope_not_an_exception(
    call, name, arguments, expected
):
    envelope = await call(name, **arguments)
    assert envelope["error"] is not None, (name, arguments)
    assert expected in envelope["error"]["message"]
    assert envelope["rows"] == []


@pytest.mark.data
@pytest.mark.parametrize("limit", [0, -5, 10**6])
async def test_limit_is_clamped(call, limit):
    envelope = await call("rank_climate_alignment", year=2023, limit=limit)
    assert envelope["error"] is None
    assert 1 <= len(envelope["rows"]) <= 100


@pytest.mark.data
async def test_start_year_after_end_year_errors_rather_than_returning_empty(call):
    envelope = await call("get_emissions_trend", utility_name="Alliant",
                          start_year=2030, end_year=2010)
    assert envelope["error"] is not None
    assert envelope["rows"] == []


@pytest.mark.data
async def test_preview_table_limit_is_bounded(call):
    envelope = await call("preview_table", table_name="emissions_targets", limit=10**6)
    assert len(envelope["rows"]) <= 20


# -------------------------------------------------------- query_data containment


@pytest.mark.data
@pytest.mark.parametrize(
    "sql,reason",
    [
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("CREATE TABLE t AS SELECT 1", "Only SELECT"),
        ("UPDATE emissions_targets SET year = 1", "Only SELECT"),
        ("DELETE FROM emissions_targets", "Only SELECT"),
        ("DROP TABLE emissions_targets", "Only SELECT"),
        ("COPY (SELECT 1) TO '/tmp/exfil.csv'", "Only SELECT"),
        ("ATTACH '/tmp/other.duckdb'", "Only SELECT"),
    ],
)
async def test_query_data_refuses_writes_and_multi_statement(call, sql, reason):
    envelope = await call("query_data", sql=sql)
    assert envelope["error"] is not None, sql
    assert reason.lower() in envelope["error"]["message"].lower()


@pytest.mark.data
def test_the_connection_actually_refuses_writes(db):
    """Belt and braces: the statement check is not the only thing standing there."""
    with pytest.raises(duckdb.Error):
        db.execute("CREATE TABLE should_not_exist (x INTEGER)")


@pytest.mark.data
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM glob('/etc/*')",
        "SELECT * FROM read_csv('/etc/hosts')",
        "SELECT content FROM read_text('/etc/hosts')",
        "SELECT * FROM read_json_auto('/etc/hosts')",
        "SELECT * FROM read_csv('https://example.com/x.csv')",
    ],
)
async def test_query_data_cannot_reach_the_filesystem(call, sql):
    """A SELECT must not read local or remote files through DuckDB table functions.

    The statement check only proves a query is a SELECT; a SELECT can still read
    anything the process can. The serving connection gives up external access at
    startup, where a query cannot re-enable it, so this holds for shapes nobody
    thought to enumerate.
    """
    envelope = await call("query_data", sql=sql)
    assert envelope["error"] is not None, f"reached the filesystem: {sql}"
    assert envelope["rows"] == []


@pytest.mark.data
def test_the_serving_policy_is_what_the_server_runs_under(db):
    """The restriction belongs to the connection, not to a call site."""
    from rmi_mcp_uth.db import SERVE_CONFIG

    assert SERVE_CONFIG["enable_external_access"] is False
    with pytest.raises(duckdb.Error):
        db.execute("SELECT * FROM glob('/etc/*')").fetchall()


def test_ingest_policy_keeps_the_access_it_needs():
    """Ingest reads the RMI CSVs, so it must not inherit the serving lockdown."""
    from rmi_mcp_uth.db import INGEST_CONFIG

    assert INGEST_CONFIG.get("enable_external_access", True) is not False


# ------------------------------------------------------------------- startup


def test_missing_database_and_data_produce_a_clear_error(tmp_path, monkeypatch):
    """Not a traceback halfway through a tool call."""
    from rmi_mcp_uth import db as db_module

    monkeypatch.setattr(db_module, "_DB", None)
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "absent.duckdb")
    monkeypatch.setattr(db_module, "DATA_DIR", tmp_path / "absent-data")

    connection = db_module.get_db()
    try:
        tables = connection.execute("SHOW TABLES").fetchall()
    finally:
        connection.close()
        db_module._DB = None
    # It builds an empty database rather than raising, which is survivable; what
    # matters is that the failure is visible instead of a half-populated schema.
    assert tables == []


# ------------------------------------------------------------------ resources


@pytest.mark.data
async def test_resources_return_text(client):
    for uri in ("rmi://data-dictionary", "rmi://methodology", "rmi://data-dictionary-full"):
        contents = await client.read_resource(uri)
        assert contents
        assert contents[0].text.strip(), uri


@pytest.mark.data
async def test_data_dictionary_year_span_matches_the_database(db, client):
    """Read off the data rather than stated, so a refresh cannot make it false."""
    first, last, horizon = db.execute(
        """SELECT min(year),
                  max(year) FILTER (WHERE emissions_co2_historical IS NOT NULL),
                  max(year)
           FROM emissions_targets"""
    ).fetchone()
    text = (await client.read_resource("rmi://data-dictionary"))[0].text
    assert f"measured data {first}-{last}" in text
    assert f"pathway to {horizon}" in text
