"""Layer 4 — the 1.5C pathway.

Two halves. The threshold predicates run against a two-row table built inline,
because whether 149.9 flags and 150.1 does not is answered by the constant, and
real data can only answer it approximately from whatever sits near the line.
Everything else runs on real data, selecting by property rather than by name.
"""

import duckdb
import pytest

from rmi_mcp_uth import comparability as C
from rmi_mcp_uth.comparability import CAVEATS, comparability_cte

# The columns comparability_cte() reads. A schema drift test holds this to
# DESCRIBE emissions_targets so the inline fixtures cannot go stale silently.
CTE_COLUMNS = [
    "utility_name_irp",
    "owned_delivered",
    "year",
    "emissions_co2_historical",
    "emissions_co2_1point5c",
    "net_generation_mwh",
    "net_generation_mwh_1point5c",
]

MWH = 1e7  # 10 TWh, so co2 in MMT * 100 == kg/MWh


def flags_for(rows):
    """Run comparability_cte over an inline emissions_targets and collect flags.

    `rows` are tuples in CTE_COLUMNS order. Returns {(utility, basis, year): set}.
    """
    connection = duckdb.connect(":memory:")
    columns = ", ".join(CTE_COLUMNS)
    values = ", ".join(
        "(" + ", ".join("NULL" if v is None else repr(v) for v in row) + ")"
        for row in rows
    )
    connection.execute(
        f"CREATE TABLE emissions_targets AS SELECT * FROM (VALUES {values}) AS t({columns})"
    )
    result = connection.execute(
        f"WITH {comparability_cte()} "
        "SELECT utility_name_irp, owned_delivered, year, comparability_flags FROM comparability"
    ).fetchall()
    connection.close()
    return {(u, b, y): set(f) for u, b, y, f in result}


def baseline_rows(utility, co2_2005, *, basis="delivered"):
    """A 2005 anchor plus a later year, both with clean positive generation."""
    return [
        (utility, basis, 2005, co2_2005, 1.0, MWH, MWH),
        (utility, basis, 2020, 1.0, 1.0, MWH, MWH),
    ]


# ------------------------------------------------------- threshold predicates
# No database. These catch `<` vs `<=` and a silently retuned constant.


def test_low_baseline_straddles_150_kg_mwh():
    assert C._LOW_BASELINE_KG_MWH == 150.0
    # 1.499 MMT over 10 TWh is 149.9 kg/MWh; 1.501 is 150.1.
    flags = flags_for(baseline_rows("under", 1.499) + baseline_rows("over", 1.501))
    assert "low_baseline" in flags[("under", "delivered", 2005)]
    assert "low_baseline" not in flags[("over", "delivered", 2005)]


def test_low_pathway_intensity_straddles_100_kg_mwh():
    assert C._LOW_PATHWAY_KG_MWH == 100.0
    rows = [
        ("under", "delivered", 2030, 5.0, 0.999, MWH, MWH),   # pathway 99.9 kg/MWh
        ("over", "delivered", 2030, 5.0, 1.001, MWH, MWH),    # pathway 100.1
    ]
    flags = flags_for(rows)
    assert "low_pathway_intensity" in flags[("under", "delivered", 2030)]
    assert "low_pathway_intensity" not in flags[("over", "delivered", 2030)]


def test_series_break_straddles_the_2_5x_ratio():
    assert C._SERIES_BREAK_RATIO == 2.5
    # Load held flat, so load_base_shift cannot own the step.
    breaking = [
        ("break", "delivered", 2010, 1.0, 1.0, MWH, MWH),
        ("break", "delivered", 2011, 2.51, 1.0, MWH, MWH),
    ]
    steady = [
        ("steady", "delivered", 2010, 1.0, 1.0, MWH, MWH),
        ("steady", "delivered", 2011, 2.49, 1.0, MWH, MWH),
    ]
    flags = flags_for(breaking + steady)
    assert "series_break" in flags[("break", "delivered", 2011)]
    assert "series_break" not in flags[("steady", "delivered", 2011)]


def test_series_break_fires_downward_too():
    rows = [
        ("drop", "delivered", 2010, 2.51, 1.0, MWH, MWH),
        ("drop", "delivered", 2011, 1.0, 1.0, MWH, MWH),
    ]
    assert "series_break" in flags_for(rows)[("drop", "delivered", 2011)]


def test_load_base_shift_straddles_0_6_and_1_6():
    assert (C._LOAD_SHIFT_LOW, C._LOAD_SHIFT_HIGH) == (0.6, 1.6)
    rows = [
        ("shrink", "delivered", 2010, 1.0, 1.0, MWH, MWH),
        ("shrink", "delivered", 2011, 1.0, 1.0, MWH * 0.59, MWH),
        ("inside", "delivered", 2010, 1.0, 1.0, MWH, MWH),
        ("inside", "delivered", 2011, 1.0, 1.0, MWH * 0.61, MWH),
        ("grow", "delivered", 2010, 1.0, 1.0, MWH, MWH),
        ("grow", "delivered", 2011, 1.0, 1.0, MWH * 1.61, MWH),
    ]
    flags = flags_for(rows)
    assert "load_base_shift" in flags[("shrink", "delivered", 2011)]
    assert "load_base_shift" in flags[("grow", "delivered", 2011)]
    assert "load_base_shift" not in flags[("inside", "delivered", 2011)]


def test_owned_share_floor_straddles_5_percent():
    assert C._OWNED_SHARE_FLOOR == 0.05
    rows = [
        ("buyer", "delivered", 2020, 10.0, 1.0, MWH, MWH),
        ("buyer", "owned", 2020, 0.49, 1.0, MWH, MWH),      # 4.9% of delivered
        ("owner", "delivered", 2020, 10.0, 1.0, MWH, MWH),
        ("owner", "owned", 2020, 0.51, 1.0, MWH, MWH),      # 5.1%
    ]
    flags = flags_for(rows)
    assert "owns_no_generation" in flags[("buyer", "delivered", 2020)]
    assert "owns_no_generation" not in flags[("owner", "delivered", 2020)]


def test_invalid_generation_fires_on_zero_and_negative():
    rows = [
        ("zero", "delivered", 2020, 1.0, 1.0, 0.0, MWH),
        ("negative", "delivered", 2020, 1.0, 1.0, -50.0, MWH),
        ("fine", "delivered", 2020, 1.0, 1.0, MWH, MWH),
    ]
    flags = flags_for(rows)
    assert "invalid_generation" in flags[("zero", "delivered", 2020)]
    assert "invalid_generation" in flags[("negative", "delivered", 2020)]
    assert "invalid_generation" not in flags[("fine", "delivered", 2020)]


def test_joint_filing_reads_the_name_not_a_list():
    rows = [
        ("A Co. and B Co.", "delivered", 2020, 1.0, 1.0, MWH, MWH),
        ("Dairyland Power Coop", "delivered", 2020, 1.0, 1.0, MWH, MWH),
        ("Orange & Rockland Utilities, Inc", "delivered", 2020, 1.0, 1.0, MWH, MWH),
    ]
    flags = flags_for(rows)
    assert "joint_filing" in flags[("A Co. and B Co.", "delivered", 2020)]
    assert "joint_filing" not in flags[("Dairyland Power Coop", "delivered", 2020)]
    assert "joint_filing" not in flags[("Orange & Rockland Utilities, Inc", "delivered", 2020)]


# ------------------------------------------------------------ real-data checks


@pytest.mark.data
def test_cte_columns_still_match_the_table(db):
    """Guards the inline fixtures against schema drift."""
    actual = {row[0] for row in db.execute("DESCRIBE emissions_targets").fetchall()}
    assert set(CTE_COLUMNS) <= actual, set(CTE_COLUMNS) - actual


@pytest.mark.data
def test_pathway_is_anchored_to_each_utility_own_2005_intensity(db):
    """The claim every caveat in comparability.py rests on.

    pathway(y) divided by that utility's own 2005 historical intensity is
    near-constant across utilities within a year. If RMI ever rebuilds the
    pathway on another basis, this is where it shows.
    """
    rows = db.execute(
        """
        WITH s AS (
          SELECT utility_name_irp, owned_delivered, year,
                 sum(emissions_co2_historical) * 1e9
                   / nullif(sum(net_generation_mwh), 0) AS hist_kg,
                 sum(emissions_co2_1point5c) * 1e9
                   / nullif(sum(net_generation_mwh_1point5c), 0) AS path_kg
          FROM emissions_targets GROUP BY 1, 2, 3),
        b AS (
          SELECT utility_name_irp, owned_delivered,
                 max(hist_kg) FILTER (WHERE year = 2005) AS base
          FROM s GROUP BY 1, 2)
        SELECT s.year, count(*), min(s.path_kg / b.base), median(s.path_kg / b.base)
        FROM s JOIN b USING (utility_name_irp, owned_delivered)
        WHERE b.base > 0 AND s.path_kg IS NOT NULL AND s.year IN (2010, 2020, 2030)
        GROUP BY s.year"""
    ).fetchall()
    assert len(rows) == 3
    for year, n, low, mid in rows:
        assert n > 100, (year, n)
        assert low == pytest.approx(mid, rel=1e-3), (year, low, mid)


@pytest.mark.drift
def test_documented_2020_scaling_does_not_hold(db):
    """Counter-test to RMI Methodology p.14 and Data Dictionary section 6.

    Both describe the pathway as a national trajectory scaled by the company's
    2020 share of sector emissions. If that held, pathway(y)/pathway(2020) would
    be one number per year. It is not — the ratio spans orders of magnitude
    within a single year. Both PDFs are dated 2022 and describe 2005-2020 data;
    this database runs to 2035.

    If RMI ever republishes on the documented basis this flips, and the comments
    in comparability.py have to be rewritten rather than quietly becoming false.
    """
    low, high = db.execute(
        """
        WITH s AS (
          SELECT utility_name_irp, owned_delivered, year,
                 sum(emissions_co2_1point5c) AS p
          FROM emissions_targets GROUP BY 1, 2, 3),
        b AS (
          SELECT utility_name_irp, owned_delivered,
                 max(p) FILTER (WHERE year = 2020) AS p20
          FROM s GROUP BY 1, 2)
        SELECT min(s.p / b.p20), max(s.p / b.p20)
        FROM s JOIN b USING (utility_name_irp, owned_delivered)
        WHERE b.p20 > 0 AND s.p IS NOT NULL AND s.year = 2030"""
    ).fetchone()
    assert high / max(low, 1e-9) > 10, (low, high)


@pytest.mark.data
async def test_negative_benchmark_years_suppress_pct_over(db, call):
    """Every utility's benchmark goes negative by 2035.

    `status` then reads 'above pathway' for any positive emissions, which is
    true but useless, so the percentage must be withheld rather than computed
    against a negative denominator.
    """
    utility, = db.execute(
        """SELECT utility_name_irp FROM emissions_targets
           WHERE year = 2035 AND emissions_co2_1point5c < 0
           ORDER BY emissions_co2_1point5c LIMIT 1"""
    ).fetchone()
    envelope = await call("get_climate_alignment", utility_name=utility,
                          basis="delivered", start_year=2035, end_year=2035)
    rows = [r for r in envelope["rows"] if (r["emissions_co2_1point5c"] or 0) <= 0]
    assert rows, "no negative-benchmark row returned"
    for row in rows:
        assert row["pct_over_1point5c"] is None, row
        assert row["status"] is not None


@pytest.mark.data
async def test_every_caveat_fires_for_at_least_one_utility_year(db, call):
    """Selected by property, never by name, and asserted non-empty.

    A branch that went empty upstream would otherwise iterate zero times and
    pass green.
    """
    seen = set()
    rows = db.execute(
        f"""WITH {comparability_cte()}
        SELECT f, count(*) FROM (SELECT unnest(comparability_flags) AS f FROM comparability)
        GROUP BY 1"""
    ).fetchall()
    for flag, count in rows:
        assert count > 0
        seen.add(flag)
    assert seen == set(CAVEATS), set(CAVEATS) ^ seen


@pytest.mark.data
async def test_flags_reach_get_climate_alignment(db, call):
    """A flag computed in SQL must surface in notes and in comparability_flags."""
    utility, = db.execute(
        f"""WITH {comparability_cte()}
        SELECT utility_name_irp FROM comparability
        WHERE list_contains(comparability_flags, 'low_baseline')
          AND owned_delivered = 'delivered' LIMIT 1"""
    ).fetchone()
    envelope = await call("get_climate_alignment", utility_name=utility, basis="delivered")
    flags = {f for row in envelope["rows"] for f in row.get("comparability_flags", [])}
    assert "low_baseline" in flags
    caveated = [n for n in envelope["notes"] if n["kind"] == "not_comparable"]
    assert caveated, envelope["notes"]
    assert any(c["flag"] == "low_baseline" for c in caveated[0]["caveats"])


@pytest.mark.data
async def test_scope_values_partition_the_population(call):
    """comparable + flagged == candidates, counted on meta rather than rows.

    `limit` clamps at 100 and the population is larger, so row counts truncate.
    """
    scopes = {}
    for scope in ("comparable", "all", "flagged"):
        envelope = await call("rank_climate_alignment", year=2024, basis="delivered",
                              scope=scope, limit=100)
        scopes[scope] = envelope
        assert envelope["error"] is None, (scope, envelope["error"])
        assert envelope["meta"]["scope"] == scope

    candidates = scopes["all"]["meta"]["candidates"]
    assert all(e["meta"]["candidates"] == candidates for e in scopes.values())

    comparable = {r["utility_name"] for r in scopes["comparable"]["rows"]}
    flagged = {r["utility_name"] for r in scopes["flagged"]["rows"]}
    assert not (comparable & flagged), comparable & flagged


@pytest.mark.data
async def test_caveats_ride_along_in_every_scope(call):
    for scope in ("comparable", "all", "flagged"):
        envelope = await call("rank_climate_alignment", year=2024, scope=scope)
        kinds = {n["kind"] for n in envelope["notes"]}
        assert "not_comparable" in kinds, (scope, kinds)


@pytest.mark.data
async def test_flagged_scope_on_an_unflagged_population_fails_with_a_message(db, call):
    """An empty result must explain itself rather than return zero rows."""
    envelope = await call("rank_climate_alignment", year=2024, scope="flagged",
                          utility_type="no such type")
    assert envelope["error"] is not None
    assert envelope["rows"] == []


@pytest.mark.data
def test_no_unflagged_entity_hides_a_second_operating_utility(db):
    """Structural guard sharing no assumption with the ` and ` rule.

    An entity secretly covering two operating utilities needs several summed to
    reconcile its owned CO2, so its target/operations ratio sits far above the
    noise band that single entities occupy. A real composite roughly doubles it.
    """
    from rmi_mcp_uth.names import normalized_column

    rows = db.execute(
        f"""
        WITH t AS (
          SELECT utility_name_irp, sum(emissions_co2_historical) AS co2
          FROM emissions_targets
          WHERE owned_delivered = 'owned' AND year = 2023
          GROUP BY 1),
        o AS (
          SELECT {normalized_column('utility_name')} AS norm, sum(emissions_co2) AS co2
          FROM operations_emissions_by_tech
          WHERE owned_energy_source AND year = 2023
          GROUP BY 1)
        SELECT t.utility_name_irp, t.co2 / nullif(o.co2, 0) AS ratio
        FROM t JOIN o ON o.norm = {normalized_column('t.utility_name_irp')}
        WHERE t.co2 > 1 AND o.co2 > 1"""
    ).fetchall()
    assert len(rows) > 50, len(rows)
    # Single entities cluster at 1.0; anything near 2 is a second utility hiding.
    suspicious = [(name, ratio) for name, ratio in rows if ratio and ratio > 1.5]
    assert not suspicious, suspicious


@pytest.mark.data
async def test_flags_are_computed_before_the_parent_rollup(db, call):
    """One wires-only subsidiary must not disqualify its whole parent.

    Selected by property: the largest-emitting parent that carries both a
    flagged and an unflagged utility in the ranked year. If the filter ran after
    the rollup, that parent would be missing from scope='comparable' entirely;
    if it ran before, the parent survives carrying only its clean subsidiaries.
    """
    blocking = ", ".join(repr(f) for f in sorted(set(CAVEATS) - {"low_pathway_intensity"}))
    parent_name, clean, flagged = db.execute(
        f"""WITH {comparability_cte()},
        x AS (
          SELECT e.parent_name, e.utility_name_irp,
                 sum(e.emissions_co2_historical) AS co2,
                 len(list_filter(coalesce(any_value(c.comparability_flags), []),
                                 f -> f IN ({blocking}))) > 0 AS is_flagged
          FROM emissions_targets e
          LEFT JOIN comparability c
                 ON c.utility_name_irp = e.utility_name_irp
                AND c.owned_delivered = e.owned_delivered
                AND c.year = e.year
          WHERE e.owned_delivered = 'delivered' AND e.year = 2024
          GROUP BY e.parent_name, e.utility_name_irp)
        SELECT parent_name,
               list_sort(list(utility_name_irp) FILTER (WHERE NOT is_flagged)),
               list_sort(list(utility_name_irp) FILTER (WHERE is_flagged))
        FROM x GROUP BY 1
        HAVING count(*) FILTER (WHERE is_flagged) > 0
           AND count(*) FILTER (WHERE NOT is_flagged) > 0
        ORDER BY sum(co2) DESC NULLS LAST LIMIT 1"""
    ).fetchone()

    envelope = await call("rank_climate_alignment", year=2024, basis="delivered",
                          group_by="parent", scope="comparable", limit=100)
    row = next((r for r in envelope["rows"] if r["parent_name"] == parent_name), None)
    assert row is not None, f"{parent_name} dropped from the comparable ranking"
    assert set(row["utilities"]) == set(clean), (row["utilities"], clean)
    assert not set(row["utilities"]) & set(flagged), flagged


@pytest.mark.drift
def test_threshold_calibration(db):
    """Distribution of each flagged quantity against its constant.

    Reported, not asserted as a bug: a change here is information about the
    refresh. The bands are wide enough that only a structural shift trips them.
    """
    under_150, band = db.execute(
        """
        WITH s AS (
          SELECT utility_name_irp, owned_delivered, year,
                 sum(emissions_co2_historical) * 1e9
                   / nullif(sum(net_generation_mwh), 0) AS kg
          FROM emissions_targets GROUP BY 1, 2, 3),
        b AS (
          SELECT utility_name_irp, owned_delivered,
                 max(kg) FILTER (WHERE year = 2005) AS base
          FROM s GROUP BY 1, 2)
        SELECT count(*) FILTER (WHERE base < 150),
               count(*) FILTER (WHERE base >= 150 AND base <= 220)
        FROM b WHERE base IS NOT NULL"""
    ).fetchone()
    assert 15 <= under_150 <= 40, under_150
    assert 1 <= band <= 15, band
