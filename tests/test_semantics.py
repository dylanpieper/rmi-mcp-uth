"""Layer 3 — the four traps the data dictionary names.

Each is a claim the tools encode about what a number means. A refresh that
quietly changed one would leave every arithmetic test green.
"""

import pytest

from rmi_mcp_uth.entities import composite_entities, split_composite

from .entity_bridge import COMPOSITES

pytestmark = pytest.mark.data


# ---------------------------------------------------------------- trap 1: basis


async def test_basis_all_returns_both_row_sets_tagged(call):
    envelope = await call("get_emissions_trend",
                          utility_name="Wisconsin Power & Light", basis="all")
    bases = {row["owned_delivered"] for row in envelope["rows"]}
    assert bases == {"owned", "delivered"}


async def test_basis_all_differs_from_either_alone(call):
    per_year = {}
    for basis in ("owned", "delivered", "all"):
        envelope = await call("get_emissions_trend",
                              utility_name="Wisconsin Power & Light",
                              basis=basis, start_year=2023, end_year=2023)
        per_year[basis] = sum(
            row["emissions_co2_historical"] or 0 for row in envelope["rows"]
        )
    assert per_year["all"] == pytest.approx(per_year["owned"] + per_year["delivered"])
    assert per_year["owned"] != pytest.approx(per_year["delivered"])


async def test_rank_refuses_basis_all(call):
    envelope = await call("rank_climate_alignment", basis="all")
    assert envelope["error"] is not None
    assert "double-count" in envelope["error"]["message"]


# ------------------------------------------------- trap 1b: owned != delivered


def test_owned_and_delivered_are_neither_subset_nor_derivable(db):
    """Locks the claim against a future 'fix' recomputing delivered as owned + purchases."""
    delivered_lt_owned, no_owned, no_delivered = db.execute(
        """
        WITH p AS (
          SELECT utility_name_irp,
                 sum(emissions_co2_historical) FILTER (WHERE owned_delivered = 'owned') AS o,
                 sum(emissions_co2_historical) FILTER (WHERE owned_delivered = 'delivered') AS d
          FROM emissions_targets WHERE year = 2023 GROUP BY 1)
        SELECT count(*) FILTER (WHERE d < o),
               count(*) FILTER (WHERE o IS NULL),
               count(*) FILTER (WHERE d IS NULL)
        FROM p"""
    ).fetchone()
    # Delivered below owned is only possible if delivered is not owned + purchases.
    assert delivered_lt_owned > 10, delivered_lt_owned
    # And a quarter of utilities carry one basis only, so neither derives the other.
    assert no_owned > 10 and no_delivered > 10, (no_owned, no_delivered)


async def test_single_basis_utilities_get_an_explanation_not_a_name_error(db, call):
    """A perfect name match with no rows on the default basis must not read as 'no match'."""
    owned_only, = db.execute(
        """SELECT utility_name_irp FROM emissions_targets WHERE year = 2023
           GROUP BY 1
           HAVING sum(emissions_co2_historical) FILTER (WHERE owned_delivered='delivered') IS NULL
              AND sum(emissions_co2_historical) FILTER (WHERE owned_delivered='owned') IS NOT NULL
           LIMIT 1"""
    ).fetchone()
    envelope = await call("get_emissions_trend", utility_name=owned_only, basis="delivered")
    assert envelope["error"] is not None
    message = envelope["error"]["message"]
    assert "no match" not in message.lower()
    assert "owned" in message


# ----------------------------------------------------------- trap 2: purchases


async def test_default_returns_only_owned_generation(call, db):
    envelope = await call("get_generation_mix",
                          utility_name="Wisconsin Power & Light", year=2023)
    assert {row["owned_energy_source"] for row in envelope["rows"]} == {True}


async def test_include_purchases_adds_exactly_the_non_owned_sources(call, db):
    expected = {
        row[0] for row in db.execute(
            "SELECT DISTINCT energy_source FROM operations_emissions_by_tech "
            "WHERE NOT owned_energy_source"
        ).fetchall()
    }
    envelope = await call("get_generation_mix",
                          utility_name="Wisconsin Power & Light", year=2023,
                          include_purchases=True)
    non_owned = {row["energy_source"] for row in envelope["rows"]
                 if not row["owned_energy_source"]}
    assert non_owned <= expected
    assert non_owned, "include_purchases returned no non-owned rows"


def test_owned_rows_can_be_negative_too(db):
    """Storage consumes more than it returns, so the owned basis is not sign-clean.

    The get_generation_mix docstring frames negative rows as a property of the
    non-owned supply rows. Owned storage rolls up negative on its own
    (.claude/ISSUES.md issue 4), so a default call can return a negative row.
    """
    twh, = db.execute(
        """SELECT sum(net_generation) FROM operations_emissions_by_tech
           WHERE owned_energy_source AND year = 2023 AND technology_rmi = 'Storage'"""
    ).fetchone()
    assert twh < 0, twh


# ------------------------------------------------------- trap 3: joint ownership


def test_jointly_owned_generators_carry_one_row_per_owner(db):
    counts = dict(db.execute(
        """WITH g AS (
             SELECT plant_id_eia, generator_id, count(DISTINCT parent_name) AS owners
             FROM operations_emissions_by_tech
             WHERE year = 2023 AND owned_energy_source GROUP BY 1, 2)
           SELECT owners, count(*) FROM g WHERE owners > 1 GROUP BY 1"""
    ).fetchall())
    assert counts.get(2, 0) > 100, counts
    assert counts.get(3, 0) > 50, counts
    assert max(counts) >= 8, counts


async def test_technology_rollup_sums_every_owner_not_one_parent(call, db):
    """Filtering to one parent_name understates a jointly owned utility."""
    utility, = db.execute(
        """SELECT utility_name FROM operations_emissions_by_tech
           WHERE year = 2023 AND owned_energy_source
           GROUP BY utility_name
           HAVING count(DISTINCT parent_name) > 1
           ORDER BY sum(net_generation) DESC NULLS LAST LIMIT 1"""
    ).fetchone()

    everything = await call("get_generation_mix", utility_name=utility,
                            year=2023, group_by="technology")
    total = sum(r["net_generation_twh"] or 0 for r in everything["rows"])

    one_parent, = db.execute(
        """SELECT max(twh) FROM (
             SELECT sum(net_generation) AS twh FROM operations_emissions_by_tech
             WHERE utility_name = ? AND year = 2023 AND owned_energy_source
             GROUP BY parent_name)""", [utility]
    ).fetchone()
    assert total > one_parent, (utility, total, one_parent)


def test_list_utilities_gives_each_owner_its_own_share(db):
    """Regression on the join at tools.py:185-191.

    Joining on utility_id_eia alone lets every owner's row see every other
    owner's capacity, and max_by then hands each of them the largest share.
    """
    rows = db.execute(
        """SELECT u.utility_name, u.parent_name,
                  max_by(m.capacity_owned_in_state, m.year) AS cap
           FROM utility_information u
           JOIN utility_state_map m
             ON m.utility_id_eia = u.utility_id_eia
            AND m.parent_name IS NOT DISTINCT FROM u.parent_name
           WHERE m.capacity_owned_in_state > 0
           GROUP BY 1, 2
           HAVING count(*) > 0"""
    ).fetchall()
    by_utility = {}
    for name, parent, cap in rows:
        by_utility.setdefault(name, set()).add(cap)
    shared = [n for n, caps in by_utility.items() if len(caps) > 1]
    assert shared, "no jointly owned utility carries differing per-owner capacity"


# ------------------------------------------------------ trap 4 / scope parity


def test_runtime_decomposition_still_finds_every_composite(db):
    """A fifth joint filer, or a renamed component, must fail here."""
    found = {}
    for name in COMPOSITES:
        composites = composite_entities(db, name)
        found.update(composites)
    assert set(found) == set(COMPOSITES), (sorted(found), sorted(COMPOSITES))
    for name, components in COMPOSITES.items():
        assert set(found[name]) == set(components), (name, found[name], components)


def test_only_and_joins_entities_never_ampersand(db):
    """`&` appears inside single names; splitting on it would halve them.

    Composites carry both — "Louisville Gas & Electric Co. and Kentucky
    Utilities Co." — so the claim is about names where `&` is the only
    candidate separator, not about every name containing one.
    """
    names = [row[0] for row in db.execute(
        "SELECT DISTINCT utility_name_irp FROM emissions_targets WHERE utility_name_irp IS NOT NULL"
    ).fetchall()]
    ampersand_only = [n for n in names if "&" in n and " and " not in n]
    assert ampersand_only, "no ampersand-only names left to guard against"
    assert all(not split_composite(n) for n in ampersand_only), ampersand_only


def test_bare_and_inside_a_place_name_does_not_split(db):
    """`Dairyland`, `Rockland`, `Long Island`, `Portland` must not decompose."""
    names = [row[0] for row in db.execute(
        "SELECT DISTINCT utility_name_irp FROM emissions_targets "
        "WHERE utility_name_irp ILIKE '%and%' AND utility_name_irp NOT LIKE '% and %'"
    ).fetchall()]
    assert len(names) > 5, names
    assert all(not split_composite(n) for n in names), names


def search_term(components):
    """A term a caller would plausibly type: the first component's leading words.

    The full composite name matches nothing in the generation table — that is
    issue 7, pinned separately — so scope-note coverage is asserted against a
    term that actually resolves.
    """
    return " ".join(components[0].split()[:2])


async def test_composite_default_call_carries_the_scope_note(call):
    for name, components in COMPOSITES.items():
        envelope = await call("get_generation_mix",
                              utility_name=search_term(components), year=2023)
        kinds = {note["kind"] for note in envelope["notes"]}
        assert "entity_scope" in kinds, (name, kinds, envelope["error"])
        assert envelope["meta"]["matched_irp_entity"] is False


async def test_composite_name_is_not_a_dead_end(call):
    """The name get_emissions_trend reports must be usable in get_generation_mix.

    Currently fails: the composite name matches nothing in the generation table
    and the tool returns `no_name_match`, discarding the composite it had
    already detected and never naming `match_irp_entity=True`.
    See .claude/ISSUES.md issue 7.
    """
    name = "Louisville Gas & Electric Co. and Kentucky Utilities Co."
    envelope = await call("get_generation_mix", utility_name=name, year=2023,
                          group_by="technology")
    if envelope["error"] is None:
        return
    assert "match_irp_entity" in envelope["error"]["message"], (
        f"dead end: {envelope['error']['message']}"
    )


async def test_widening_reconciles_with_the_emissions_tool(call):
    """match_irp_entity=True must bring the two tools to the same fleet.

    Owned basis only: RMI built the joint row by summing exactly these
    utilities' owned emissions, and delivered emissions follow power sold.
    """
    name = "Louisville Gas & Electric Co. and Kentucky Utilities Co."
    trend = await call("get_emissions_trend", utility_name=name, basis="owned",
                       start_year=2023, end_year=2023)
    target = sum(r["emissions_co2_historical"] or 0 for r in trend["rows"])

    narrow = await call("get_generation_mix", utility_name=name, year=2023,
                        group_by="technology")
    wide = await call("get_generation_mix", utility_name=name, year=2023,
                      group_by="technology", match_irp_entity=True)
    narrow_co2 = sum(r["emissions_co2_mmt"] or 0 for r in narrow["rows"])
    wide_co2 = sum(r["emissions_co2_mmt"] or 0 for r in wide["rows"])

    assert wide_co2 > narrow_co2, (narrow_co2, wide_co2)
    assert wide_co2 == pytest.approx(target, rel=1e-3), (wide_co2, target)


async def test_every_composite_either_agrees_on_scope_or_says_it_does_not(call):
    """The bug this replaced: one search term, two fleets, nothing flagging it."""
    for name, components in COMPOSITES.items():
        term = search_term(components)
        trend = await call("get_emissions_trend", utility_name=term, basis="owned",
                           start_year=2023, end_year=2023)
        mix = await call("get_generation_mix", utility_name=term, year=2023,
                         group_by="technology")
        target = sum(r["emissions_co2_historical"] or 0 for r in trend["rows"])
        generation = sum(r["emissions_co2_mmt"] or 0 for r in mix["rows"])
        agrees = generation and abs(target / generation - 1) < 0.01
        says_so = any(n["kind"] == "entity_scope" for n in mix["notes"])
        assert agrees or says_so, (name, target, generation)


# --------------------------------------------- the tool-level gap from issue 6


async def test_negative_generation_intensity_is_not_silent(db, call):
    """A negative denominator must not yield a large negative intensity unremarked.

    Currently fails: get_emissions_trend divides by nullif(mwh, 0) and runs no
    comparability, so it ships -11,526 kg/MWh with an empty notes array.
    See .claude/ISSUES.md issue 6.
    """
    utility, year = db.execute(
        """SELECT utility_name_irp, year FROM emissions_targets
           WHERE net_generation_mwh < 0 AND emissions_co2_historical IS NOT NULL
           ORDER BY emissions_co2_historical * 1e9 / net_generation_mwh LIMIT 1"""
    ).fetchone()
    envelope = await call("get_emissions_trend", utility_name=utility, basis="all",
                          start_year=year, end_year=year)
    for row in envelope["rows"]:
        intensity = row["co2_intensity_kg_mwh"]
        if intensity is not None and intensity < 0:
            pytest.fail(
                f"{utility} {year}: co2_intensity_kg_mwh={intensity:.1f} with "
                f"notes={envelope['notes']}"
            )
