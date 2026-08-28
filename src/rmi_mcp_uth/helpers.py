"""Shared filters, row budgets, and the response headers the tools attach."""

from .db import get_db
from .names import (
    TARGET_NAME_COLUMNS,
    name_params,
    name_predicate,
    name_suggestions,
)


def basis_filter(basis: str) -> str | None:
    """Map a basis name to a SQL predicate on emissions_targets.owned_delivered.

    emissions_targets holds one row set per accounting boundary; returning both
    without distinguishing them double-counts, so callers must pick.
    """
    return {
        "delivered": "AND owned_delivered = 'delivered'",
        "owned": "AND owned_delivered = 'owned'",
        "all": "",
    }.get(basis.strip().lower())

MAX_LISTED_MATCHES = 25

# One row budget for every tool. A parent name crossed with 20 years of
# per-subsidiary rows reaches ~1,350 rows / ~490 KB — roughly 165k tokens, a
# whole context window for a single call, and no question is better answered by
# that than by an aggregate.
#
# Set above the technology rollup's ceiling (20 years x 11 technology_rmi values
# = 220 rows) so the aggregate a refusal points at can never itself be refused.
# That ceiling grows by ~11 rows for each new year of data.
MAX_RESPONSE_ROWS = 300


def year_window(start_year: int, end_year: int) -> tuple[str, list[int]]:
    """SQL predicate and params for an optional inclusive year range."""
    clauses, params = [], []
    if start_year:
        clauses.append("AND year >= ?")
        params.append(start_year)
    if end_year:
        clauses.append("AND year <= ?")
        params.append(end_year)
    return " ".join(clauses), params


def history_split(df) -> dict:
    """Where history stops and projection starts, for the response header.

    emissions_targets runs to 2035 while measured data stops at 2024, so an
    unbounded request returns a decade of projections. Naming the boundary
    keeps a stated 2035 target from being read as a current emissions figure.
    """
    historical = df.loc[df["emissions_co2_historical"].notna(), "year"]
    projected = df.loc[df["emissions_co2_historical"].isna(), "year"]
    split = {}
    if not historical.empty:
        split["historical_through"] = int(historical.max())
    if not projected.empty:
        split["projected_from"] = int(projected.min())
        split["projection_note"] = (
            "Rows at or after projected_from are stated targets or IRP "
            "projections, not measured emissions. See the `source` column."
        )
    return split


def matched_names_meta(utility_name: str, matched: list[str], rows: int) -> dict:
    """Header entry naming which utilities a fuzzy search actually resolved to.

    A parent name can match dozens of subsidiaries; without this the caller has
    to scan every row to notice that "Xcel Energy" spans three utilities, or
    cannot see it at all once a rollup drops the utility_name column.
    """
    matched = sorted(matched)
    meta = {
        "search": utility_name,
        "matched_utilities": matched[:MAX_LISTED_MATCHES],
        "match_count": len(matched),
        "rows": rows,
    }
    if len(matched) > MAX_LISTED_MATCHES:
        meta["matched_utilities_truncated"] = True
    return {"_meta": meta}


def excluded_note(matched_all: list[str], kept: list[str], reason: str) -> dict | None:
    """Name the utilities a tool's own default filter cut from a name match.

    `matched_utilities` deliberately reports what actually ran, not everything
    the name touched, which hides a whole class of surprise: a parent whose
    subsidiaries mostly deliver rather than generate resolves to just its
    generation arm, and nothing in the response says the rest were dropped.
    Only implicit defaults are reported here — a caller who passes start_year
    already knows the window excludes things.
    """
    dropped = sorted(set(matched_all) - set(kept))
    if not dropped:
        return None
    note = {
        "utilities": dropped[:MAX_LISTED_MATCHES],
        "count": len(dropped),
        "reason": reason,
    }
    if len(dropped) > MAX_LISTED_MATCHES:
        note["truncated"] = True
    return note


def basis_excluded_note(
    db, utility_name: str, basis: str, year_filter: str, year_params: list, matched: list
) -> dict | None:
    """Report utilities the default basis dropped from an emissions name match.

    A quarter of the utilities with targets carry only one accounting basis, so
    a parent can resolve to some subsidiaries on 'delivered' and others only on
    'owned'. empty_targets_reason already catches the case where that empties
    the result; this catches the partial one, where the survivors look complete.
    """
    basis = basis.strip().lower()
    if basis == "all":
        return None
    return excluded_note(
        distinct_names(
            db,
            "emissions_targets",
            "utility_name_irp",
            f"WHERE ({name_predicate(TARGET_NAME_COLUMNS)}) {year_filter}",
            [*name_params(TARGET_NAME_COLUMNS, utility_name), *year_params],
        ),
        matched,
        f"Matched the name but has no {basis!r} rows in this year range — RMI "
        f"publishes only the other accounting basis for it. Pass basis='all' "
        f"to include it.",
    )


def distinct_names(db, table: str, column: str, where_sql: str, params: list) -> list:
    """Distinct non-null values of a name column under a WHERE clause."""
    rows = db.execute(
        f"SELECT DISTINCT {column} FROM {table} {where_sql}", params
    ).fetchall()
    return [row[0] for row in rows if row[0] is not None]


def empty_targets_reason(
    utility_name: str, basis: str, start_year: int, end_year: int
) -> dict:
    """Explain an empty emissions_targets result without blaming the name.

    A quarter of the utilities with targets carry only one accounting basis
    (27 owned-only, 20 delivered-only), so the default basis="delivered" empties
    the result for a name that matched perfectly well. Reporting that as "no
    match" sends the caller off to fix a name that was never wrong.
    """
    db = get_db()
    rows = db.execute(
        f"""
        SELECT DISTINCT owned_delivered, min(year) OVER (), max(year) OVER ()
        FROM emissions_targets
        WHERE ({name_predicate(TARGET_NAME_COLUMNS)})
        """,
        name_params(TARGET_NAME_COLUMNS, utility_name),
    ).fetchall()

    if not rows:
        return no_name_match(utility_name, "emissions_targets", TARGET_NAME_COLUMNS)

    available = sorted({row[0] for row in rows})
    if basis.strip().lower() != "all" and basis.strip().lower() not in available:
        return {
            "error": (
                f"'{utility_name}' matched, but has no '{basis}' rows — RMI "
                f"publishes only {' and '.join(repr(b) for b in available)} for "
                f"it. Retry with basis={available[0]!r}."
            ),
            "available_bases": available,
        }

    low, high = rows[0][1], rows[0][2]
    return {
        "error": (
            f"'{utility_name}' matched, but no rows fall in the requested year "
            f"range. Data runs {low}-{high}."
        ),
        "available_years": [low, high],
    }


def oversized_targets(utility_name: str, df, basis: str) -> dict | None:
    """Refuse an emissions payload past the row budget, naming how to narrow it.

    Rows are ordered by year, so trimming to the budget would drop the most
    recent years — the ones the caller almost certainly wanted. Refusing with
    the real counts is more useful than a quietly truncated trend.
    """
    if len(df) <= MAX_RESPONSE_ROWS:
        return None

    utilities = sorted(df["utility_name"].dropna().unique().tolist())
    bases = sorted(df["owned_delivered"].dropna().unique().tolist())
    low, high = int(df["year"].min()), int(df["year"].max())

    options = []
    if len(utilities) > 1:
        shown = ", ".join(repr(u) for u in utilities[:3])
        options.append(
            f"name one utility ({shown}{', ...' if len(utilities) > 3 else ''})"
        )
    options.append(
        f"narrow the years (data runs {low}-{high}; end_year=2024 is history only)"
    )
    if len(bases) > 1:
        options.append(f"pick a single basis ({' or '.join(repr(b) for b in bases)})")

    return {
        "error": (
            f"'{utility_name}' matched {len(df):,} rows across {len(utilities)} "
            f"utilities and {low}-{high}, over the {MAX_RESPONSE_ROWS}-row budget. "
            f"Narrow it: " + "; ".join(options) + "."
        ),
        "matched_rows": int(len(df)),
        "matched_utilities": utilities[:MAX_LISTED_MATCHES],
        "utilities": len(utilities),
        "years": [low, high],
        "available_bases": bases,
    }


def no_name_match(utility_name: str, table: str, columns: list[str]) -> dict:
    """Error payload for a name that matched nothing, with close names attached."""
    suggestions = name_suggestions(table, columns, utility_name)
    message = f"No match for '{utility_name}' in {table}."
    if suggestions:
        message += " Did you mean: " + ", ".join(repr(s) for s in suggestions) + "?"
    else:
        message += " Use list_utilities() to browse."
    return {"error": message}
