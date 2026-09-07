"""Layer 2 — units and additivity.

A unit label is a claim about magnitude. These check the claims against the
data, and check that the two lists in `response.py` still describe real columns.
"""

import pytest

from rmi_mcp_uth.response import NON_ADDITIVE, UNITS

pytestmark = pytest.mark.data

# Columns that carry a number but no unit, on purpose: identifiers, counts, and
# category labels that read like units but are bucket names.
UNITLESS = {
    "year",
    "latest_year",
    "utility_id_eia",
    "utility_id_ferc1",
    "respondent_id",
    "plant_id_eia",
    "operating_year",
    "retirement_year",
    "customers",
    "housing_units",
    "utilities",
    "match_count",
    "row_count",
    "match_rank",
}


@pytest.fixture(scope="session")
def all_columns(db):
    """Every column name in the database, lowercased."""
    names = set()
    for (table,) in db.execute("SHOW TABLES").fetchall():
        for row in db.execute(f"DESCRIBE {table}").fetchall():
            names.add(row[0].lower())
    return names


@pytest.fixture(scope="session")
def tool_columns(envelopes):
    """Every column any tool actually returned, lowercased."""
    names = set()
    for envelope in envelopes.values():
        for row in envelope["rows"][:1]:
            names.update(c.lower() for c in row)
    return names


def test_every_unit_names_a_real_column(all_columns, tool_columns):
    """A dead UNITS entry rots into a wrong label after a schema change."""
    known = all_columns | tool_columns
    orphans = {name for name in UNITS if name.lower() not in known}
    assert not orphans, f"UNITS keys matching no column anywhere: {sorted(orphans)}"


def test_every_non_additive_names_a_real_column(all_columns, tool_columns):
    known = all_columns | tool_columns
    orphans = {name for name in NON_ADDITIVE if name.lower() not in known}
    assert not orphans, f"NON_ADDITIVE names matching no column: {sorted(orphans)}"


def test_every_numeric_output_column_has_a_unit_or_is_allowlisted(envelopes):
    missing = set()
    for envelope in envelopes.values():
        units = {c.lower() for c in envelope["units"]}
        for row in envelope["rows"][:1]:
            for column, value in row.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                if column.lower() in units or column.lower() in UNITLESS:
                    continue
                missing.add(column)
    assert not missing, f"numeric columns with no unit and no allowlist entry: {sorted(missing)}"


async def test_intensity_is_kg_per_mwh_by_formula(call):
    """RMI publishes metric tons/MWh; the tools report 1000x that.

    Asserted as the formula rather than a magnitude, so the deliberate
    divergence is pinned to its definition and not to a fleet's current mix.
    """
    envelope = await call("get_emissions_trend",
                          utility_name="Wisconsin Power & Light", end_year=2024)
    checked = 0
    for row in envelope["rows"]:
        co2, twh = row["emissions_co2_historical"], row["net_generation_twh"]
        intensity = row["co2_intensity_kg_mwh"]
        if co2 is None or not twh or intensity is None:
            continue
        assert intensity == pytest.approx(co2 * 1e9 / (twh * 1e6), rel=1e-9)
        checked += 1
    assert checked, "no rows carried both emissions and generation"


def test_magnitude_bands(db):
    """Each unit implies a range. A silent 1000x would pass every other test."""
    max_utility_year_mmt, = db.execute(
        "SELECT max(emissions_co2_historical) FROM emissions_targets"
    ).fetchone()
    assert 10 < max_utility_year_mmt < 200, max_utility_year_mmt

    max_generator_gw, = db.execute(
        "SELECT max(capacity) FROM operations_emissions_by_tech WHERE owned_energy_source"
    ).fetchone()
    assert 0.5 < max_generator_gw < 2.0, max_generator_gw

    total_twh, = db.execute(
        """SELECT sum(net_generation) FROM operations_emissions_by_tech
           WHERE owned_energy_source AND year = 2023"""
    ).fetchone()
    assert 3000 < total_twh < 5500, total_twh

    # Only where generation is positive. 46 utility-years carry negative
    # net_generation_mwh, and dividing by those yields intensities to
    # -11,526 kg/MWh — see test_negative_generation_is_flagged.
    lo, hi = db.execute(
        """SELECT min(kg), max(kg) FROM (
             SELECT emissions_co2_historical * 1e9 / net_generation_mwh AS kg
             FROM emissions_targets
             WHERE net_generation_mwh > 0 AND emissions_co2_historical IS NOT NULL)"""
    ).fetchone()
    assert 0 <= lo and hi < 10_000, (lo, hi)


def test_negative_generation_is_flagged(db):
    """Negative net_generation_mwh must reach the caller flagged, not silently.

    `comparability_cte` guards `WHEN mwh > 0`, so it computes no intensity and
    raises `invalid_generation` for every one of these rows. The tool SQL guards
    only `nullif(net_generation_mwh, 0)`, so it still divides by a negative
    denominator and emits the intensity anyway (.claude/ISSUES.md issue 6).
    This pins the flag; the tool-level gap is asserted in test_semantics.
    """
    from rmi_mcp_uth.comparability import comparability_cte

    total, flagged = db.execute(
        f"""WITH {comparability_cte()}
        SELECT count(*),
               count(*) FILTER (WHERE list_contains(c.comparability_flags, 'invalid_generation'))
        FROM emissions_targets e
        JOIN comparability c
          ON c.utility_name_irp = e.utility_name_irp
         AND c.owned_delivered = e.owned_delivered
         AND c.year = e.year
        WHERE e.net_generation_mwh < 0 AND e.emissions_co2_historical IS NOT NULL"""
    ).fetchone()
    assert total > 0, "no negative-generation rows left; the branch went empty"
    assert flagged == total, f"{total - flagged} negative-generation rows unflagged"


async def test_computed_capacity_factor_is_a_fraction(call):
    """The value the tool computes, not the raw column.

    The tool divides summed generation by summed potential rather than averaging
    the column, which is why its result behaves while the raw column does not
    (see .claude/ISSUES.md issue 3). Storage is legitimately negative: it
    consumes more than it returns.
    """
    envelope = await call("get_generation_mix", utility_name="Alliant",
                          group_by="technology", year=2023)
    factors = [r["capacity_factor"] for r in envelope["rows"] if r["capacity_factor"] is not None]
    assert factors
    assert all(-0.2 <= f <= 1.05 for f in factors), sorted(factors)[:3] + sorted(factors)[-3:]


async def test_non_additive_rates_are_earned(call):
    """Summing a rate must differ materially from recomputing it off the totals."""
    envelope = await call("get_generation_mix", utility_name="Alliant",
                          group_by="technology", year=2023)
    rows = [r for r in envelope["rows"] if r["net_generation_twh"] and r["potential_generation_twh"]]
    assert len(rows) > 1
    summed = sum(r["capacity_factor"] for r in rows)
    recomputed = (sum(r["net_generation_twh"] for r in rows)
                  / sum(r["potential_generation_twh"] for r in rows))
    assert abs(summed - recomputed) > 0.5, (summed, recomputed)
    assert "capacity_factor" in envelope["meta"]["non_additive"]


def test_capacity_owned_in_state_is_not_the_ownership_share(db):
    """Plain MW, still not additive: summed over a state it exceeds the state's fleet."""
    summed, = db.execute(
        """SELECT sum(capacity_owned_in_state) FROM utility_state_map
           WHERE state_abbr = 'WI' AND year = 2023"""
    ).fetchone()
    prorated, = db.execute(
        """SELECT sum(year_end_capacity) * 1000 FROM operations_emissions_by_tech
           WHERE state = 'WI' AND year = 2023 AND owned_energy_source"""
    ).fetchone()
    assert summed > 2 * prorated, (summed, prorated)


async def test_capacity_owned_in_state_is_declared_non_additive(call):
    envelope = await call("list_utilities", state_abbr="WI")
    assert "capacity_owned_in_state" in envelope["meta"]["non_additive"]
    # And its unit string says so in words, since a bare "MW" would not.
    assert "not the ownership share" in envelope["units"]["capacity_owned_in_state"]
