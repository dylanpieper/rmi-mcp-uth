"""The MCP tools."""

import duckdb

from .comparability import CAVEATS, caveat_notes, comparability_cte
from .config import mcp
from .db import get_db
from .helpers import (
    MAX_LISTED_MATCHES,
    MAX_RESPONSE_ROWS,
    basis_excluded_note,
    basis_filter,
    distinct_names,
    empty_targets_reason,
    excluded_note,
    history_split,
    matched_names_meta,
    no_name_match,
    oversized_targets,
    year_window,
)
from .names import (
    OPERATIONS_NAME_COLUMNS,
    TARGET_NAME_COLUMNS,
    name_params,
    name_predicate,
    name_rank,
    name_rank_params,
    name_suggestions,
)




@mcp.tool
def list_tables() -> list[dict]:
    """List all tables with their column names and types.

    Call this first to understand what data is available before querying.
    """
    db = get_db()
    tables = [row[0] for row in db.execute("SHOW TABLES").fetchall()]
    result = []
    for table in tables:
        cols = db.execute(f"DESCRIBE {table}").fetchall()
        row_count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        result.append(
            {
                "table": table,
                "columns": [{"name": c[0], "type": c[1]} for c in cols],
                "row_count": row_count,
            }
        )
    return result


@mcp.tool
def preview_table(table_name: str, limit: int = 5) -> list[dict]:
    """Show the first few rows of a table to understand its structure.

    Use this after list_tables to see what the actual data looks like.
    `limit` caps the rows returned (1-20).
    """
    db = get_db()
    known = {row[0] for row in db.execute("SHOW TABLES").fetchall()}
    if table_name not in known:
        return [{"error": f"Unknown table '{table_name}'. Use list_tables()."}]
    df = db.execute(
        f'SELECT * FROM "{table_name}" LIMIT ?', [max(1, min(limit, 20))]
    ).fetchdf()
    return df.to_dict(orient="records")


@mcp.tool
def list_utilities(
    state_abbr: str = "", name_contains: str = "", limit: int = 100
) -> list[dict]:
    """List utilities, optionally filtered by two-letter state code (e.g. 'CO', 'TX').

    Returns utility and parent names, EIA/FERC1 IDs, RMI utility type, and — when
    a state is given — the capacity that utility owns in that state (MW), largest
    first. `name_contains` additionally filters on utility or parent name
    (case-insensitive substring).
    Use this to find the right utility name before pulling emissions or generation data.
    """
    db = get_db()
    limit = max(1, min(limit, 500))

    name_columns = ["u.utility_name", "u.parent_name"]
    rank_select, rank_order, rank_params, name_where, name_params = "", "", [], "", []
    # Without a state there is no size signal to sort by, and a parent name ties
    # dozens of rows at the same rank. Filing a FERC Form 1 separates operating
    # utilities from the project LLCs that share their name.
    if name_contains:
        rank_select = f", {name_rank(*name_columns)} AS match_rank"
        rank_order = "match_rank, "
        rank_params = name_rank_params(name_contains)
        name_where = f"AND ({name_predicate(name_columns)})"
        name_params = name_params(name_columns, name_contains)

    if state_abbr:
        sql = f"""
            SELECT
                u.utility_name,
                u.parent_name,
                u.utility_id_eia,
                u.utility_id_ferc1,
                u.utility_type_rmi,
                m.state_abbr,
                max(m.year) AS latest_year,
                max_by(m.capacity_owned_in_state, m.year) AS capacity_owned_in_state_mw
                {rank_select}
            FROM utility_information u
            JOIN utility_state_map m USING (utility_id_eia)
            WHERE UPPER(m.state_abbr) = UPPER(?)
            {name_where}
            GROUP BY ALL
            ORDER BY {rank_order} capacity_owned_in_state_mw DESC NULLS LAST, u.utility_name
            LIMIT ?
        """
        params = [*rank_params, state_abbr, *name_params, limit]
    else:
        sql = f"""
            SELECT DISTINCT
                u.utility_name,
                u.parent_name,
                u.utility_id_eia,
                u.utility_id_ferc1,
                u.utility_type_rmi
                {rank_select}
            FROM utility_information u
            WHERE TRUE
            {name_where}
            ORDER BY {rank_order} u.utility_id_ferc1 IS NULL, u.utility_name
            LIMIT ?
        """
        params = [*rank_params, *name_params, limit]

    df = db.execute(sql, params).fetchdf()

    if df.empty:
        suggestions = name_suggestions(
            "utility_information", ["utility_name", "parent_name"], name_contains
        )
        note = "No utilities found. Try a different state code or name."
        if suggestions:
            note = (
                "No utilities found. Did you mean: "
                + ", ".join(repr(s) for s in suggestions)
                + "?"
            )
        return [{"note": note}]

    # match_rank exists only to order the result; it is not data the caller wants.
    df = df.drop(columns=["match_rank"], errors="ignore")
    out = df.to_dict(orient="records")
    if len(out) == limit:
        out.append(
            {"note": f"Truncated to {limit} rows; raise `limit` or narrow the filter."}
        )
    return out


@mcp.tool
def get_emissions_trend(
    utility_name: str,
    basis: str = "delivered",
    start_year: int = 0,
    end_year: int = 0,
) -> list[dict]:
    """Get yearly CO2 emissions and 1.5°C pathway comparison for a utility.

    Matches the utility's own name or its parent's, so "Xcel Energy" returns
    all three Xcel subsidiaries. Corporate suffixes are interchangeable
    ("Southern Company" finds "Southern Co.").

    Returns historical CO2 (MMT), emissions implied by the utility's stated
    targets, its IRP projection, and RMI's 1.5°C benchmark — plus generation
    (TWh) and emissions intensity (kg CO2/MWh).

    `basis` selects the accounting boundary and MUST be one of:
      "delivered" (default) — emissions behind the power the utility sells
      "owned"               — emissions from generation the utility owns
      "all"                 — both, distinguished by the owned_delivered column
    The two bases are separate row sets; never sum across them.
    The search is case-insensitive and supports partial names.

    Covers 2005-2035: measured emissions through 2024, stated targets and IRP
    projections after that. The `source` column labels every row, and the
    response header reports where the boundary falls. Narrow with start_year /
    end_year (both inclusive, 0 means unbounded) — pass end_year=2024 for
    history only.
    """
    db = get_db()
    basis_row = basis_filter(basis)
    if basis_row is None:
        return [{"error": "basis must be one of: delivered, owned, all"}]

    year_filter, year_params = year_window(start_year, end_year)

    df = db.execute(
        f"""
        SELECT
            utility_name_irp AS utility_name,
            parent_name,
            owned_delivered,
            year,
            emissions_co2_historical,
            emissions_co2_target,
            emissions_co2_irp,
            CASE
                WHEN emissions_co2_historical IS NOT NULL THEN 'historical'
                WHEN emissions_co2_target IS NOT NULL THEN 'stated target'
                WHEN emissions_co2_irp IS NOT NULL THEN 'IRP projection'
            END AS source,
            emissions_co2_1point5c,
            net_generation_mwh / 1e6 AS net_generation_twh,
            net_generation_mwh_1point5c / 1e6 AS net_generation_twh_1point5c,
            emissions_co2_historical * 1e9
                / nullif(net_generation_mwh, 0) AS co2_intensity_kg_mwh,
            emissions_co2_1point5c * 1e9
                / nullif(net_generation_mwh_1point5c, 0)
                AS co2_intensity_1point5c_kg_mwh
        FROM emissions_targets
        WHERE ({name_predicate(TARGET_NAME_COLUMNS)})
        {basis_row}
        {year_filter}
        ORDER BY owned_delivered, year
    """,
        [*name_params(TARGET_NAME_COLUMNS, utility_name), *year_params],
    ).fetchdf()

    if df.empty:
        return [empty_targets_reason(utility_name, basis, start_year, end_year)]

    oversized = oversized_targets(utility_name, df, basis)
    if oversized:
        return [oversized]

    matched = df["utility_name"].dropna().unique().tolist()
    meta = matched_names_meta(utility_name, matched, len(df))
    meta["_meta"].update(history_split(df))
    excluded = basis_excluded_note(
        db, utility_name, basis, year_filter, year_params, matched
    )
    if excluded:
        meta["_meta"]["excluded_by_default"] = excluded
    return [meta, *df.to_dict(orient="records")]


@mcp.tool
def get_generation_mix(
    utility_name: str,
    year: int = 0,
    include_purchases: bool = False,
    group_by: str = "utility",
) -> list[dict]:
    """Get electricity generation breakdown by technology for a utility.

    Matches the utility's own name or its parent's. group_by="utility" (the
    default) returns one row per subsidiary and technology; group_by="technology"
    sums across everything the name matched, which is what a parent-level
    comparison wants — "Duke Energy" collapses from 41 rows to 8.

    The rollup sums over the matched set rather than grouping by parent_name,
    which matters: RMI splits jointly owned utilities across parents (a 19.9%
    stake in Duke Energy Indiana is filed under parent "Singapore"), so a
    parent_name rollup would report Duke roughly 20% light.

    Shows capacity (GW), net generation (TWh), capacity factor, and CO2
    emissions (MMT) by RMI technology group (Coal, Gas, Wind, Solar, Nuclear,
    Hydro, Storage, ...).

    By default this covers only generation the utility OWNS. The underlying
    table also carries non-owned energy_source rows — wholesale power
    purchases, net exchanges, wheeled power, energy efficiency, demand
    response, and negative transmission losses. Those are supply, not
    generation, and summing them together with owned output overstates the
    fleet and mixes in negative rows. Set include_purchases=True to get them
    as separate rows, split by the energy_source and owned_energy_source
    columns.

    The search is case-insensitive and supports partial names.
    Optionally filter to a single year; defaults to all years (2005-2024).
    """
    db = get_db()

    group_by = group_by.strip().lower()
    if group_by not in ("utility", "technology"):
        return [{"error": "group_by must be one of: utility, technology"}]

    params: list = name_params(OPERATIONS_NAME_COLUMNS, utility_name)
    owned_filter = "" if include_purchases else "AND owned_energy_source"
    year_filter = ""
    if year:
        year_filter = "AND year = ?"
        params.append(year)

    where_sql = f"""
        WHERE ({name_predicate(OPERATIONS_NAME_COLUMNS)})
        {owned_filter}
        {year_filter}
    """
    # Same clause minus the implicit owned filter, for reporting who it cut.
    # owned_filter binds no parameters, so both share `params` unchanged.
    name_year_where = f"""
        WHERE ({name_predicate(OPERATIONS_NAME_COLUMNS)})
        {year_filter}
    """

    # Size follows the rows that survive the filters, not the arguments that
    # were passed, so measure it rather than inferring it: a year pins one
    # dimension but says nothing about the others, and 'Energy' in 2023 is a
    # single year across 1,006 utilities. Refuse rather than truncate — rows
    # are ordered by year, so a cut would silently drop the most recent data,
    # the part the caller almost certainly wanted.
    #
    # The technology rollup needs no check: it is bounded by years x
    # technology_rmi, under the budget by construction (see MAX_RESPONSE_ROWS),
    # and refusing the aggregate a refusal points at would be a dead end.
    if group_by == "utility":
        matched_rows = db.execute(
            f"""
            SELECT count(*) FROM (
                SELECT 1 FROM operations_emissions_by_tech
                {where_sql}
                GROUP BY utility_name, parent_name, year, technology_rmi,
                         energy_source, owned_energy_source
            )
            """,
            params,
        ).fetchone()[0]
        if matched_rows > MAX_RESPONSE_ROWS:
            low, high, utilities = db.execute(
                f"SELECT min(year), max(year), count(DISTINCT utility_name) "
                f"FROM operations_emissions_by_tech {where_sql}",
                params,
            ).fetchone()

            # Only offer narrowing that is still available: telling a caller who
            # already passed year=2023 to pass a year is a dead end.
            options = []
            if not year:
                options.append(f"pass year=YYYY for a single year (data runs {low}-{high})")
            options.append(
                "pass group_by='technology' to sum the subsidiaries into one fleet"
            )
            if utilities > 1:
                options.append(
                    f"name the utility more specifically ({utilities:,} matched "
                    f"'{utility_name}' on their own or their parent's name)"
                )

            span = f"{low}" if year else f"{low}-{high}"
            return [
                {
                    "error": (
                        f"'{utility_name}' matches {matched_rows:,} rows "
                        f"({utilities:,} utilities x {span} x technology), over the "
                        f"{MAX_RESPONSE_ROWS}-row budget. Narrow it: "
                        + "; ".join(options) + "."
                    ),
                    "matched_rows": matched_rows,
                    "utilities": utilities,
                    "years": [low, high],
                }
            ]

    if group_by == "technology":
        # year stays a grouping key so year=0 rolls up per year rather than
        # smearing two decades into one row.
        dimensions = """
            year,
            technology_rmi,
            count(DISTINCT utility_name) AS utilities,
        """
        order_by = "year, net_generation_twh DESC"
    else:
        dimensions = """
            utility_name,
            parent_name,
            year,
            technology_rmi,
            energy_source,
            owned_energy_source,
        """
        order_by = "year, owned_energy_source DESC, net_generation_twh DESC"

    df = db.execute(
        f"""
        SELECT
            {dimensions}
            SUM(capacity) AS capacity_gw,
            SUM(net_generation) AS net_generation_twh,
            SUM(potential_generation) AS potential_generation_twh,
            SUM(net_generation)
                / nullif(SUM(potential_generation), 0) AS capacity_factor,
            SUM(emissions_co2) AS emissions_co2_mmt
        FROM operations_emissions_by_tech
        {where_sql}
        GROUP BY ALL
        ORDER BY {order_by}
    """,
        params,
    ).fetchdf()

    if df.empty:
        return [
            no_name_match(
                utility_name, "operations_emissions_by_tech", OPERATIONS_NAME_COLUMNS
            )
        ]

    if "utility_name" in df.columns:
        matched = df["utility_name"].dropna().unique().tolist()
    else:
        # The technology rollup drops utility_name, so ask for it separately
        # under the same filters rather than reporting a wider match than ran.
        matched = distinct_names(
            db, "operations_emissions_by_tech", "utility_name", where_sql, params
        )

    meta = matched_names_meta(utility_name, matched, len(df))
    if not include_purchases:
        # A name can resolve to subsidiaries that own no generation at all —
        # every Exelon delivery utility, say. They are correctly absent from
        # the numbers, but a caller comparing two parents needs to be told,
        # or a partial fleet reads as the whole one.
        excluded = excluded_note(
            distinct_names(
                db,
                "operations_emissions_by_tech",
                "utility_name",
                name_year_where,
                params,
            ),
            matched,
            "Matched the name but owns no generation in this period; its "
            "supply is purchased, wheeled, or exchanged. Pass "
            "include_purchases=True to include those rows.",
        )
        if excluded:
            meta["_meta"]["excluded_by_default"] = excluded
    return [meta, *df.to_dict(orient="records")]


@mcp.tool
def get_climate_alignment(
    utility_name: str,
    basis: str = "delivered",
    start_year: int = 0,
    end_year: int = 0,
) -> list[dict]:
    """Compare a utility's emissions to RMI's 1.5°C pathway, year by year.

    Matches the utility's own name or its parent's.

    Adds a gap column (CO2 minus the 1.5°C benchmark: positive = above the
    pathway) and a status label. Historical years use reported CO2; future
    years fall back to the utility's stated target, then its IRP projection.

    `basis` is "delivered" (default), "owned", or "all" — see get_emissions_trend.
    The search is case-insensitive and supports partial names.

    Covers 2005-2035; the `source` column marks each row historical, stated
    target, or IRP projection. Narrow with start_year / end_year (inclusive,
    0 means unbounded) — pass end_year=2024 for measured years only.
    """
    db = get_db()
    basis_row = basis_filter(basis)
    if basis_row is None:
        return [{"error": "basis must be one of: delivered, owned, all"}]

    year_filter, year_params = year_window(start_year, end_year)

    df = db.execute(
        f"""
        WITH {comparability_cte()},
        matched AS (
            SELECT * FROM emissions_targets
            WHERE ({name_predicate(TARGET_NAME_COLUMNS)})
            {basis_row}
            {year_filter}
        ),
        t AS (
            SELECT
                m.utility_name_irp AS utility_name,
                m.parent_name,
                m.owned_delivered,
                m.year,
                m.emissions_co2_historical,
                coalesce(
                    m.emissions_co2_historical,
                    m.emissions_co2_target,
                    m.emissions_co2_irp
                ) AS emissions_co2,
                CASE
                    WHEN m.emissions_co2_historical IS NOT NULL THEN 'historical'
                    WHEN m.emissions_co2_target IS NOT NULL THEN 'stated target'
                    WHEN m.emissions_co2_irp IS NOT NULL THEN 'IRP projection'
                END AS source,
                m.emissions_co2_1point5c,
                m.net_generation_mwh / 1e6 AS net_generation_twh,
                m.net_generation_mwh,
                m.net_generation_mwh_1point5c,
                coalesce(c.comparability_flags, []) AS comparability_flags
            FROM matched m
            LEFT JOIN comparability c
                   ON c.utility_name_irp = m.utility_name_irp
                  AND c.owned_delivered = m.owned_delivered
                  AND c.year = m.year
        )
        SELECT
            * EXCLUDE (net_generation_mwh, net_generation_mwh_1point5c),
            emissions_co2 - emissions_co2_1point5c AS gap_vs_1point5c,
            -- Intensity is what the pathway is built from: RMI ramps each
            -- utility down from its own 2005 kg/MWh. Two utilities can carry
            -- the same MMT gap off benchmarks an order of magnitude apart.
            CASE WHEN net_generation_mwh > 0
                 THEN emissions_co2 * 1e9 / net_generation_mwh
                 END AS co2_intensity_kg_mwh,
            CASE WHEN net_generation_mwh_1point5c > 0
                 THEN emissions_co2_1point5c * 1e9 / net_generation_mwh_1point5c
                 END AS co2_intensity_1point5c_kg_mwh,
            CASE WHEN emissions_co2_1point5c > 0
                  AND NOT list_contains(comparability_flags, 'low_pathway_intensity')
                 THEN 100 * (emissions_co2 / emissions_co2_1point5c - 1)
                 END AS pct_over_1point5c,
            CASE
                WHEN emissions_co2 IS NULL OR emissions_co2_1point5c IS NULL THEN NULL
                WHEN emissions_co2 > emissions_co2_1point5c THEN 'above pathway'
                ELSE 'at or below pathway'
            END AS status
        FROM t
        ORDER BY owned_delivered, year
    """,
        [*name_params(TARGET_NAME_COLUMNS, utility_name), *year_params],
    ).fetchdf()

    if df.empty:
        return [empty_targets_reason(utility_name, basis, start_year, end_year)]

    oversized = oversized_targets(utility_name, df, basis)
    if oversized:
        return [oversized]

    matched = df["utility_name"].dropna().unique().tolist()
    meta = matched_names_meta(utility_name, matched, len(df))
    meta["_meta"].update(history_split(df))
    excluded = basis_excluded_note(
        db, utility_name, basis, year_filter, year_params, matched
    )
    if excluded:
        meta["_meta"]["excluded_by_default"] = excluded

    # DuckDB LIST comes back as a numpy array, which does not serialize to
    # JSON — the MCP response fails validation whenever a flag is present.
    df["comparability_flags"] = df["comparability_flags"].apply(list)

    # Surfaced here as well as in rank_climate_alignment, so asking about one
    # utility raises the same caveat a ranking would have applied to it.
    caveats = caveat_notes([f for flags in df["comparability_flags"] for f in flags])
    if caveats:
        meta["_meta"]["not_comparable"] = {
            "reason": "The 1.5C comparison for this utility carries caveats.",
            "caveats": caveats,
        }
    else:
        df = df.drop(columns=["comparability_flags"])
    return [meta, *df.to_dict(orient="records")]


_RANK_METRICS = {
    "gap_mmt": "Absolute MMT above the 1.5C benchmark. The default, and the "
    "only metric that stays comparable across utility types.",
    "pct_over": "Percent above the benchmark. Null wherever the benchmark is "
    "too small to divide by — see the low_pathway_intensity flag.",
    "intensity_gap_kg_mwh": "kg CO2/MWh above the benchmark. Size-neutral, so "
    "it ranks a small dirty utility alongside a large one.",
}

# low_pathway_intensity is missing here on purpose. Every utility's benchmark
# approaches zero by 2035, so treating a small benchmark as disqualifying would
# empty out every projected year. It suppresses pct_over and nothing else.
_EXCLUDING_FLAGS = frozenset(CAVEATS) - {"low_pathway_intensity"}


def _clean(value):
    """NaN out of pandas, None into JSON."""
    if isinstance(value, float) and value != value:
        return None
    return value


@mcp.tool
def rank_climate_alignment(
    year: int = 0,
    basis: str = "delivered",
    metric: str = "gap_mmt",
    group_by: str = "utility",
    utility_type: str = "",
    min_emissions_mmt: float = 0.0,
    include_flagged: bool = False,
    ascending: bool = False,
    limit: int = 20,
) -> list[dict]:
    """Rank utilities or parents by how far they sit from RMI's 1.5C pathway.

    Returns absolute gap, percent over, and both intensities together — one
    metric alone misleads. `metric` picks the sort key: 'gap_mmt' (default),
    'pct_over', or 'intensity_gap_kg_mwh'. `ascending` flips it to rank the
    best-aligned first.

    `year` defaults to the latest year with measured data; later years fall back
    to stated targets, then IRP projections, and the `source` column says which.
    `basis` is 'delivered' or 'owned' — not 'all', which would double-count.
    `group_by` is 'utility' or 'parent'.

    Utilities whose pathway comparison is not meaningful are excluded by
    default and named in the response header with the reason; under
    group_by='parent' they are dropped before the parent is totalled, so one
    wires-only subsidiary cannot disqualify its whole parent. Pass
    include_flagged=True to rank them anyway; every row carries
    `comparability_flags` either way. Narrow further with `utility_type`
    (matches utility_type_rmi, e.g. 'Vertically Integrated') and
    `min_emissions_mmt`.
    """
    db = get_db()

    basis = basis.strip().lower()
    if basis not in ("delivered", "owned"):
        return [
            {
                "error": "basis must be 'delivered' or 'owned'. 'all' would "
                "double-count, since emissions_targets carries one row set per "
                "accounting boundary."
            }
        ]
    if metric not in _RANK_METRICS:
        return [
            {
                "error": f"metric must be one of: {', '.join(_RANK_METRICS)}.",
                "metrics": _RANK_METRICS,
            }
        ]
    group_by = group_by.strip().lower()
    if group_by not in ("utility", "parent"):
        return [{"error": "group_by must be 'utility' or 'parent'."}]

    limit = max(1, min(limit, 100))
    group_column = "utility_name_irp" if group_by == "utility" else "parent_name"

    if not year:
        year = db.execute(
            """
            SELECT max(year) FROM emissions_targets
            WHERE owned_delivered = ? AND emissions_co2_historical IS NOT NULL
            """,
            [basis],
        ).fetchone()[0]

    type_filter, type_params = "", []
    if utility_type:
        type_filter = "AND lower(e.utility_type_rmi) LIKE ?"
        type_params = [f"%{utility_type.strip().lower()}%"]

    # Grain is one row per (group, utility): flags belong to a utility, so the
    # filter has to run before the roll-up to a parent, not after.
    df = db.execute(
        f"""
        WITH {comparability_cte()}
        SELECT
            e.{group_column} AS name,
            e.utility_name_irp,
            any_value(e.utility_type_rmi) AS utility_type_rmi,
            sum(coalesce(
                e.emissions_co2_historical,
                e.emissions_co2_target,
                e.emissions_co2_irp
            )) AS emissions_co2,
            CASE
                WHEN count(e.emissions_co2_historical) > 0 THEN 'historical'
                WHEN count(e.emissions_co2_target) > 0 THEN 'stated target'
                ELSE 'IRP projection'
            END AS source,
            sum(e.emissions_co2_1point5c) AS emissions_co2_1point5c,
            sum(e.net_generation_mwh) AS net_generation_mwh,
            sum(e.net_generation_mwh_1point5c) AS net_generation_mwh_1point5c,
            coalesce(any_value(c.comparability_flags), []) AS comparability_flags
        FROM emissions_targets e
        LEFT JOIN comparability c
               ON c.utility_name_irp = e.utility_name_irp
              AND c.owned_delivered = e.owned_delivered
              AND c.year = e.year
        WHERE e.owned_delivered = ? AND e.year = ?
        {type_filter}
        GROUP BY e.{group_column}, e.utility_name_irp
        HAVING sum(coalesce(
                   e.emissions_co2_historical,
                   e.emissions_co2_target,
                   e.emissions_co2_irp
               )) IS NOT NULL
           AND sum(e.emissions_co2_1point5c) IS NOT NULL
        """,
        [basis, year, *type_params],
    ).fetchdf()

    if df.empty:
        low, high = db.execute(
            "SELECT min(year), max(year) FROM emissions_targets WHERE owned_delivered = ?",
            [basis],
        ).fetchone()
        return [
            {
                "error": (
                    f"No {basis} rows for {year}"
                    + (f" matching utility_type {utility_type!r}" if utility_type else "")
                    + f". Data runs {low}-{high}."
                )
            }
        ]

    df["comparability_flags"] = df["comparability_flags"].apply(list)
    df["blocking"] = df["comparability_flags"].apply(
        lambda flags: sorted(set(flags) & _EXCLUDING_FLAGS)
    )
    candidates = df["name"].nunique()
    flagged = df[df["blocking"].apply(bool)]
    if not include_flagged:
        df = df[~df["blocking"].apply(bool)]

    if df.empty:
        return [
            {
                "error": (
                    f"Every {basis} utility in {year} carries a comparability "
                    f"flag. Pass include_flagged=True to rank them anyway."
                ),
                "flags": sorted({f for flags in flagged["blocking"] for f in flags}),
            }
        ]

    grouped = df.groupby("name", as_index=False).agg(
        source=("source", lambda s: s.iloc[0] if s.nunique() == 1 else "mixed"),
        utility_types=("utility_type_rmi", lambda s: sorted(set(s.dropna()))),
        utilities=("utility_name_irp", lambda s: sorted(set(s))),
        emissions_co2=("emissions_co2", "sum"),
        emissions_co2_1point5c=("emissions_co2_1point5c", "sum"),
        net_generation_mwh=("net_generation_mwh", "sum"),
        net_generation_mwh_1point5c=("net_generation_mwh_1point5c", "sum"),
        comparability_flags=(
            "comparability_flags",
            lambda s: sorted({f for flags in s for f in flags}),
        ),
    )

    below_floor = 0
    if min_emissions_mmt:
        below_floor = int((grouped["emissions_co2"].abs() < min_emissions_mmt).sum())
        grouped = grouped[grouped["emissions_co2"].abs() >= min_emissions_mmt]

    gap = grouped["emissions_co2"] - grouped["emissions_co2_1point5c"]
    mwh = grouped["net_generation_mwh"].where(grouped["net_generation_mwh"] > 0)
    grouped["gap_mmt"] = gap
    # Dividing by a benchmark that has already decayed toward zero manufactures
    # a headline number out of a rounding-scale denominator. Withhold it rather
    # than let it be quoted.
    grouped["pct_over"] = (
        100 * (grouped["emissions_co2"] / grouped["emissions_co2_1point5c"] - 1)
    ).where(
        (grouped["emissions_co2_1point5c"] > 0)
        & ~grouped["comparability_flags"].apply(
            lambda flags: "low_pathway_intensity" in flags
        )
    )
    mwh_1point5c = grouped["net_generation_mwh_1point5c"].where(
        grouped["net_generation_mwh_1point5c"] > 0
    )
    grouped["co2_intensity_kg_mwh"] = grouped["emissions_co2"] * 1e9 / mwh
    grouped["co2_intensity_1point5c_kg_mwh"] = (
        grouped["emissions_co2_1point5c"] * 1e9 / mwh_1point5c
    )
    grouped["intensity_gap_kg_mwh"] = (
        grouped["co2_intensity_kg_mwh"] - grouped["co2_intensity_1point5c_kg_mwh"]
    )

    ranked = grouped.sort_values(
        metric, ascending=ascending, na_position="last"
    ).head(limit)
    ranked = ranked.rename(columns={"name": group_column})

    meta = {
        "year": int(year),
        "basis": basis,
        "metric": metric,
        "metric_note": _RANK_METRICS[metric],
        "group_by": group_by,
        "order": "ascending" if ascending else "descending",
        "candidates": int(candidates),
        "ranked": int(len(ranked)),
    }
    if (ranked["source"] != "historical").any():
        meta["source_note"] = (
            "Rows sourced from a stated target or IRP projection are plans, not "
            "measured emissions. See the `source` column."
        )
    if len(flagged):
        note = {
            "count": int(flagged["utility_name_irp"].nunique()),
            "reason": (
                "Excluded before the ranking; the 1.5C comparison is not "
                "meaningful for these. Pass include_flagged=True to rank them."
                if not include_flagged
                else "Ranked, but the 1.5C comparison is not meaningful for these."
            ),
            "utilities": sorted(flagged["utility_name_irp"].unique())[
                :MAX_LISTED_MATCHES
            ],
            "caveats": caveat_notes(
                [f for flags in flagged["blocking"] for f in flags]
            ),
        }
        if flagged["utility_name_irp"].nunique() > MAX_LISTED_MATCHES:
            note["truncated"] = True
        if group_by == "parent" and not include_flagged:
            note["parent_totals_note"] = (
                "Parent totals cover only the subsidiaries that survived this "
                "filter, so they understate the parent's full book. The "
                "`utilities` column names what each total is built from."
            )
        meta["not_comparable"] = note
    if below_floor:
        meta["below_min_emissions"] = {
            "count": below_floor,
            "reason": f"Under min_emissions_mmt={min_emissions_mmt}.",
        }

    return [
        {"_meta": meta},
        *(
            {k: _clean(v) for k, v in row.items()}
            for row in ranked.to_dict(orient="records")
        ),
    ]


@mcp.tool
def query_data(sql: str) -> list[dict]:
    """Run a read-only SQL query against the database. DuckDB SQL syntax.

    Call list_tables() first to see available tables and columns.
    One SELECT statement per call (a leading WITH ... CTE is fine).

    There is no single join key across all tables:
      utility_id_eia   — utility_information, utility_state_map,
                         operations_emissions_by_tech / _by_fuel, reliability
      utility_id_ferc1 — utility_information, customers_sales, revenue_by_tech,
                         assets_earnings_investments, debt_equity_returns,
                         net_plant_balance
      respondent_id    — expenditure_bills_burden, housing_units_income only
      emissions_targets has NO id column; join it on utility_name_irp / parent_name.
    utility_name and parent_name appear in most tables directly.

    Two traps worth knowing:
      - emissions_targets holds separate 'owned' and 'delivered' row sets —
      filter owned_delivered or you double-count.
      - operations_emissions_by_tech mixes owned generation with purchased
      power, exchanges, EE/DR, and negative transmission losses —
      filter `owned_energy_source` for generation only.

    To rank utilities against the 1.5C pathway, call rank_climate_alignment
    rather than writing the ORDER BY here. Each utility's benchmark is anchored
    to its own 2005 intensity, so a raw percent-over ranking puts utilities that
    own no generation at the top; that tool flags and excludes them.

    Example:
        SELECT utility_name_irp, year, emissions_co2_historical, emissions_co2_1point5c
        FROM emissions_targets
        WHERE year >= 2015 AND owned_delivered = 'delivered'
        ORDER BY emissions_co2_historical DESC
        LIMIT 20
    """
    db = get_db()

    try:
        statements = db.extract_statements(sql)
    except Exception as e:
        return [{"error": f"Could not parse SQL: {e}"}]

    if len(statements) != 1:
        return [
            {"error": f"Send exactly one statement per call (got {len(statements)})."}
        ]
    if statements[0].type != duckdb.StatementType.SELECT:
        return [{"error": "Only SELECT queries are allowed."}]

    try:
        df = db.execute(sql).fetchdf()
        # Truncation is right here but wrong in the tools below: the caller
        # wrote this ORDER BY, so the first rows are the ones it asked for.
        if len(df) > MAX_RESPONSE_ROWS:
            df = df.head(MAX_RESPONSE_ROWS)
            return df.to_dict(orient="records") + [
                {
                    "note": f"Results truncated to {MAX_RESPONSE_ROWS} rows. "
                    f"Add a LIMIT clause."
                }
            ]
        return df.to_dict(orient="records")
    except Exception as e:
        return [{"error": f"Query failed: {e}"}]
