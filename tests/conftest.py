"""Shared fixtures: one read-only connection and one in-memory MCP client.

The suite drives the tools through `fastmcp.Client(mcp)` rather than importing
the functions, so registration, JSON serialization and schema validation are
exercised the way a real client exercises them.
"""

import duckdb
import pytest

from rmi_mcp_uth import db as db_module
from rmi_mcp_uth import mcp
from rmi_mcp_uth.config import DB_PATH


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Refuse to run when selected tests need a database that is not built.

    Selecting these tests is a claim that the data is there, so its absence is a
    setup error, not a reason to skip — skipping would report success for a run
    that tested nothing. Stopping at collection reports it once rather than once
    per test.

    trylast: `-m` deselection is itself a pytest_collection_modifyitems hook, so
    running earlier would see every collected test rather than the selected ones.
    """
    if DB_PATH.exists():
        return
    needs_data = [
        item for item in items
        if "data" in item.keywords or "drift" in item.keywords
    ]
    if not needs_data:
        return
    pytest.exit(
        f"{DB_PATH.name} not built, but {len(needs_data)} selected tests read it. "
        f"Build it first (README step 2), or select the database-free tests with "
        f'-m "not data and not drift".',
        returncode=1,
    )


@pytest.fixture(scope="session")
def db():
    """The real database, opened read-only once and shared with the server.

    Assigning `db_module._DB` is what keeps the tools on this connection: they
    call `get_db()`, which returns the cached global. Opened through
    `connect_serving()` rather than by hand, so the suite exercises the same
    connection policy the server runs under — a hand-rolled connection here
    would quietly test something the server never uses.
    """
    if not DB_PATH.exists():
        raise RuntimeError(f"{DB_PATH.name} not built")
    connection = db_module.connect_serving()
    db_module._DB = connection
    yield connection
    db_module._DB = None
    connection.close()


@pytest.fixture(scope="session")
async def client(db):
    """An in-memory MCP client speaking to the registered server."""
    from fastmcp import Client

    async with Client(mcp) as session:
        yield session


@pytest.fixture
def call(client):
    """Call a tool and return its envelope."""

    async def _call(name, **arguments):
        result = await client.call_tool(name, arguments)
        return result.structured_content

    return _call


@pytest.fixture(scope="session")
async def envelopes(client):
    """Every TOOL_CALLS envelope, computed once.

    Layer 1 asserts ten separate properties of each. Recomputing per assertion
    would re-run comparability_cte over the whole table 120 times.
    """
    out = {}
    for name, arguments in TOOL_CALLS:
        result = await client.call_tool(name, arguments)
        out[(name, tuple(sorted(arguments.items())))] = result.structured_content
    return out


# Every tool, with arguments small enough to run fast and wide enough to
# exercise the envelope. Layer 1 parametrizes over this.
TOOL_CALLS = [
    ("list_tables", {}),
    ("preview_table", {"table_name": "emissions_targets", "limit": 5}),
    ("list_utilities", {"state_abbr": "WI"}),
    ("list_utilities", {"name_contains": "Alliant"}),
    ("get_emissions_trend", {"utility_name": "Wisconsin Power & Light", "start_year": 2015}),
    ("get_generation_mix", {"utility_name": "Wisconsin Power & Light", "year": 2023}),
    ("get_generation_mix", {"utility_name": "Alliant", "group_by": "technology"}),
    ("get_climate_alignment", {"utility_name": "Wisconsin Power & Light"}),
    ("rank_climate_alignment", {"year": 2023}),
    ("rank_climate_alignment", {"year": 2023, "group_by": "parent"}),
    ("rank_climate_alignment", {"year": 2023, "scope": "flagged"}),
    ("query_data", {"sql": "SELECT utility_name_irp, year FROM emissions_targets LIMIT 5"}),
]

# Every `kind` the code is allowed to put on a note. README and the list_tables
# docstring both publish this set, so test_docs holds them to it.
NOTE_KINDS = {
    "projection",
    "excluded_by_default",
    "not_comparable",
    "below_min_emissions",
    "entity_scope",
    "truncated",
}
