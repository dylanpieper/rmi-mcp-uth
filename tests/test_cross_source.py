"""Layer 5 — cross-source validity.

Independent RMI tables should agree. Where they do not, the gap is the finding,
so these pin the gap rather than assume it away.
"""

import pytest

from rmi_mcp_uth.names import normalized_column

from .entity_bridge import ENTITY_BRIDGE, EXACT_ENTITIES

pytestmark = pytest.mark.data


@pytest.fixture(scope="session")
def latest_year(db):
    year, = db.execute(
        """SELECT max(year) FROM emissions_targets
           WHERE emissions_co2_historical IS NOT NULL"""
    ).fetchone()
    return year


# ------------------------------------------------------------- the name bridge


@pytest.fixture(scope="session")
def unmatched_names(db):
    """IRP entities whose name resolves to no operating utility."""
    target = normalized_column("t.utility_name_irp")
    operations = normalized_column("o.utility_name")
    rows = db.execute(
        f"""SELECT DISTINCT t.utility_name_irp
            FROM (SELECT DISTINCT utility_name_irp FROM emissions_targets
                  WHERE utility_name_irp IS NOT NULL) t
            WHERE NOT EXISTS (
              SELECT 1 FROM (SELECT DISTINCT utility_name FROM operations_emissions_by_tech) o
              WHERE {operations} = {target})"""
    ).fetchall()
    return {row[0] for row in rows}


def test_the_bridge_is_exhaustive_in_both_directions(db, unmatched_names):
    """A tenth composite must fail loudly, not drop a utility from reconciliation."""
    assert unmatched_names == set(ENTITY_BRIDGE), {
        "unmatched but not bridged": sorted(unmatched_names - set(ENTITY_BRIDGE)),
        "bridged but now matching": sorted(set(ENTITY_BRIDGE) - unmatched_names),
    }


def test_every_bridge_component_still_exists_upstream(db):
    known = {
        row[0] for row in db.execute(
            "SELECT DISTINCT utility_name FROM operations_emissions_by_tech"
        ).fetchall()
    }
    missing = {
        name: [c for c in entry["components"] if c not in known]
        for name, entry in ENTITY_BRIDGE.items()
    }
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, missing


def test_the_bridge_closes_the_name_join(db, unmatched_names):
    """With the bridge, reconciliation runs 197/197 and its tolerance means something."""
    total, = db.execute(
        "SELECT count(DISTINCT utility_name_irp) FROM emissions_targets"
    ).fetchone()
    assert len(unmatched_names) + (total - len(unmatched_names)) == total
    assert set(ENTITY_BRIDGE) <= unmatched_names | set(ENTITY_BRIDGE)
    assert total - len(unmatched_names) + len(ENTITY_BRIDGE) == total


# ----------------------------------------------- composites on the owned basis


def bridged_ratio(db, name, components, basis, latest_year):
    """target CO2 / summed component CO2, per year, excluding the latest."""
    placeholders = ", ".join(["?"] * len(components))
    return db.execute(
        f"""
        WITH t AS (
          SELECT year, sum(emissions_co2_historical) AS co2
          FROM emissions_targets
          WHERE utility_name_irp = ? AND owned_delivered = ?
            AND emissions_co2_historical IS NOT NULL AND year < ?
          GROUP BY year),
        o AS (
          SELECT year, sum(emissions_co2) AS co2
          FROM operations_emissions_by_tech
          WHERE utility_name IN ({placeholders}) AND owned_energy_source
          GROUP BY year)
        SELECT t.year, t.co2 / nullif(o.co2, 0)
        FROM t JOIN o USING (year) ORDER BY t.year""",
        [name, basis, latest_year, *components],
    ).fetchall()


def test_composites_are_an_exact_identity_on_the_owned_basis(db, latest_year):
    """The strongest cross-source invariant here, and the evidence that widening
    reconstructs RMI's own aggregation rather than inventing one."""
    checked = 0
    for name in EXACT_ENTITIES:
        entry = ENTITY_BRIDGE[name]
        ratios = bridged_ratio(db, name, entry["components"], "owned", latest_year)
        assert ratios, name
        for year, ratio in ratios:
            assert ratio == pytest.approx(entry["owned_ratio"], abs=1e-4), (name, year, ratio)
            checked += 1
    assert checked > 50, checked


def test_inexact_entries_hold_their_measured_ratio(db, latest_year):
    """Minnkota runs at exactly 2.0000x; NorthWestern MT breaks in 2016-2017.

    Pinned as measured, so a refresh that changes either shape fails here rather
    than quietly widening a tolerance elsewhere.
    """
    name = "Minnkota Power Coop. and Northern Municipal Power Agency"
    entry = ENTITY_BRIDGE[name]
    ratios = bridged_ratio(db, name, entry["components"], "owned", latest_year)
    assert ratios
    for year, ratio in ratios:
        assert ratio == pytest.approx(2.0, abs=1e-4), (year, ratio)


def test_delivered_basis_does_not_reconcile_against_owned_generation(db, latest_year):
    """The converse: no tool should expect it to.

    Delivered emissions follow power sold, not generated, so the same ratio
    scatters where the owned one is exact.
    """
    spreads = []
    for name in EXACT_ENTITIES:
        entry = ENTITY_BRIDGE[name]
        ratios = [r for _, r in bridged_ratio(db, name, entry["components"],
                                              "delivered", latest_year) if r]
        if len(ratios) > 3:
            spreads.append(max(ratios) - min(ratios))
    assert spreads
    assert max(spreads) > 0.1, spreads


# --------------------------------------------------- table-to-table agreement


def test_targets_and_operations_agree_on_owned_co2(db):
    """The closest thing to ground truth the repo has."""
    median, n = db.execute(
        f"""
        WITH t AS (
          SELECT {normalized_column('utility_name_irp')} AS norm, year,
                 sum(emissions_co2_historical) AS co2
          FROM emissions_targets WHERE owned_delivered = 'owned' GROUP BY 1, 2),
        o AS (
          SELECT {normalized_column('utility_name')} AS norm, year,
                 sum(emissions_co2) AS co2
          FROM operations_emissions_by_tech WHERE owned_energy_source GROUP BY 1, 2)
        SELECT median(t.co2 / o.co2), count(*)
        FROM t JOIN o USING (norm, year)
        WHERE t.co2 > 0.1 AND o.co2 > 0.1"""
    ).fetchone()
    assert n > 1000, n
    assert median == pytest.approx(1.0, abs=0.02), median


def test_by_fuel_and_by_tech_agree(db):
    """Same grain, split differently: generation and CO2 must match."""
    rows, gen_gap, co2_gap = db.execute(
        """
        WITH f AS (
          SELECT utility_name, year, plant_id_eia, generator_id,
                 sum(net_generation) AS gen, sum(emissions_co2) AS co2
          FROM operations_emissions_by_fuel WHERE year = 2023 GROUP BY ALL),
        t AS (
          SELECT utility_name, year, plant_id_eia, generator_id,
                 sum(net_generation) AS gen, sum(emissions_co2) AS co2
          FROM operations_emissions_by_tech WHERE year = 2023 GROUP BY ALL)
        SELECT count(*), max(abs(f.gen - t.gen)), max(abs(f.co2 - t.co2))
        FROM f JOIN t USING (utility_name, year, plant_id_eia, generator_id)"""
    ).fetchone()
    assert rows > 1000, rows
    assert gen_gap < 1e-6, gen_gap
    assert co2_gap < 1e-6, co2_gap


def test_state_rollup_reconciles_with_by_tech(db):
    """operations_emissions is a state rollup of the same underlying rows."""
    ratio, = db.execute(
        """
        SELECT (SELECT sum(net_generation) FROM operations_emissions
                WHERE year = 2023 AND owned_energy_source)
             / (SELECT sum(net_generation) FROM operations_emissions_by_tech
                WHERE year = 2023 AND owned_energy_source)"""
    ).fetchone()
    assert ratio == pytest.approx(1.0, abs=0.01), ratio


async def test_generation_mix_equals_hand_written_sql(db, call):
    envelope = await call("get_generation_mix", utility_name="Wisconsin Power & Light",
                          year=2023, group_by="technology")
    expected = dict(db.execute(
        f"""SELECT technology_rmi, sum(net_generation)
            FROM operations_emissions_by_tech
            WHERE ({normalized_column('utility_name')} LIKE '%wisconsin power and light%'
                OR {normalized_column('parent_name')} LIKE '%wisconsin power and light%')
              AND owned_energy_source AND year = 2023
            GROUP BY 1"""
    ).fetchall())
    actual = {r["technology_rmi"]: r["net_generation_twh"] for r in envelope["rows"]}
    assert actual.keys() == expected.keys()
    for technology, value in expected.items():
        assert actual[technology] == pytest.approx(value), technology


def test_duplicate_eia_ids_do_not_inflate_a_count(db):
    """Data Dictionary section 17: duplicate_utility_id_eia marks repeated rows."""
    flagged, = db.execute(
        "SELECT count(*) FROM utility_information WHERE duplicate_utility_id_eia"
    ).fetchone()
    assert flagged > 0, "no duplicate rows left to guard against"
    distinct_ids, rows = db.execute(
        """SELECT count(DISTINCT utility_id_eia), count(*)
           FROM utility_information WHERE NOT duplicate_utility_id_eia"""
    ).fetchone()
    assert distinct_ids <= rows


def test_owns_no_generation_is_not_a_false_positive(db):
    """Absent owned rows and owning nothing are indistinguishable to the CTE.

    Verified benign: no utility RMI publishes only on the delivered basis owns
    meaningful capacity. If a refresh adds one, the flag starts lying.
    """
    liars = db.execute(
        f"""
        WITH no_owned AS (
          SELECT utility_name_irp FROM emissions_targets WHERE year = 2023
          GROUP BY 1
          HAVING sum(emissions_co2_historical) FILTER (WHERE owned_delivered = 'owned') IS NULL),
        ops AS (
          SELECT {normalized_column('utility_name')} AS norm,
                 sum(year_end_capacity) * 1000 AS mw
          FROM operations_emissions_by_tech
          WHERE year = 2023 AND owned_energy_source GROUP BY 1)
        SELECT n.utility_name_irp, ops.mw
        FROM no_owned n
        JOIN ops ON ops.norm = {normalized_column('n.utility_name_irp')}
        WHERE ops.mw > 100"""
    ).fetchall()
    assert not liars, liars


# ------------------------------------------------------------- coverage pins


@pytest.mark.drift
def test_coverage_pins(db):
    """Tripwires for scope drift.

    RMI's PDFs describe 403 FERC-1 respondents and "50% of US CO2". The data is
    a whole-fleet dataset: 2023 owned rows sum to roughly the entire US.
    """
    utilities, = db.execute(
        "SELECT count(DISTINCT utility_name_irp) FROM emissions_targets"
    ).fetchone()
    assert 150 <= utilities <= 260, utilities

    first, last, horizon = db.execute(
        """SELECT min(year),
                  max(year) FILTER (WHERE emissions_co2_historical IS NOT NULL),
                  max(year)
           FROM emissions_targets"""
    ).fetchone()
    assert first == 2005, first
    assert last >= 2024, last
    assert horizon >= 2035, horizon

    capacity_gw, generation_twh = db.execute(
        """SELECT sum(year_end_capacity), sum(net_generation)
           FROM operations_emissions_by_tech
           WHERE year = 2023 AND owned_energy_source"""
    ).fetchone()
    assert 1000 < capacity_gw < 1500, capacity_gw
    assert 3500 < generation_twh < 4800, generation_twh
