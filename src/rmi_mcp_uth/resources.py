"""Static context the model can attach: the data dictionary and RMI's PDFs."""

import pymupdf

from .config import DATA_DIR, mcp
from .db import get_db


@mcp.resource("rmi://data-dictionary")
def data_dictionary() -> str:
    """Overview of the RMI Utility Transition Hub datasets and key columns."""
    # Read the span off the data rather than stating it: a data refresh moves
    # both ends, and a frozen year here would be quoted back as fact.
    first, last, horizon = get_db().execute(
        """
        SELECT min(year), max(year) FILTER (WHERE emissions_co2_historical IS NOT NULL),
               max(year)
        FROM emissions_targets
        """
    ).fetchone()
    return f"""
    RMI Utility Transition Hub — Data Overview
    ===========================================
    Source: https://utilitytransitionhub.rmi.org/data-download/
    License: CC BY 4.0
    Coverage: measured data {first}-{last}; targets and 1.5C pathway to {horizon}.

    Join keys — there is no single key across all tables
    ----------------------------------------------------
    utility_id_eia    utility_information, utility_state_map,
                      operations_emissions_by_tech, operations_emissions_by_fuel,
                      reliability
    utility_id_ferc1  utility_information, customers_sales, revenue_by_tech,
                      assets_earnings_investments, debt_equity_returns,
                      net_plant_balance
    respondent_id     expenditure_bills_burden, housing_units_income only
    emissions_targets has no id column — join on utility_name_irp / parent_name.
    utility_name and parent_name appear in most tables directly.

    Four traps
    ----------
    1. emissions_targets carries separate 'owned' and 'delivered' row sets for
       each utility-year. Filter owned_delivered, or you double-count.
       They are two attribution boundaries over different quantities, not a
       subset and a superset: 'owned' is Scope 1 from the plants the utility
       owns, whoever consumes the power; 'delivered' is what is attributed to
       the power it sells, whoever generated it. Delivered is NOT owned plus
       purchases, and is lower wherever a utility generates more than it sells
       and moves the surplus wholesale. Neither is derivable from the other,
       and many utilities carry only one — a utility that owns no generation
       has no 'owned' rows at all. RMI's data dictionary and methodology PDFs
       predate this column and document only the owned, Scope 1 side; 'basis'
       is this server's parameter name for the choice, not RMI's term.
    2. operations_emissions_by_tech carries owned generation AND non-owned
       supply (wholesale purchases, net exchanges, wheeled power, energy
       efficiency, demand response, and negative transmission losses).
       Filter `owned_energy_source` for generation; split on energy_source
       to see supply.
    3. A jointly owned utility is split across parents, one row set per owner,
       pro-rated by ownership share — the same generators appear once per
       owner, each row carrying that owner's fraction. A minority owner may be
       a holding company or a foreign investor that shares no name with the
       utility. Filtering to one parent_name therefore drops the other owners'
       shares and understates the utility. Aggregate by utility_name, or sum
       every parent row. utility_state_map needs the same care: join it on
       parent_name as well as utility_id_eia, or each owner picks up the
       others' capacity.
    4. The 1.5C pathway is anchored to each utility's OWN 2005 emissions
       intensity, so benchmarks are not comparable across utilities. A
       restructured wires-only utility whose 2005 default supply was nuclear
       gets a near-zero benchmark, and buying ordinary grid power then reads as
       a several-hundred-percent overshoot of a fleet it does not own. Rank on
       absolute gap, not percent over, and prefer rank_climate_alignment, which
       flags and excludes these cases, over hand-written SQL.

    Tables
    ------
    utility_information / utility_information_2023
        parent_name, parent_lei, ticker, isin, utility_name, utility_id_ferc1,
        utility_id_eia, utility_lei, fraction_owned_utility, entity_type_eia,
        utility_type_rmi, public_private_unmapped, duplicate_utility_id_eia

    utility_state_map / utility_state_map_2023
        parent_name, utility_name, utility_id_eia, year, state, state_abbr,
        capacity_owned_in_state (MW), capacity_operated_in_state (MW),
        mwh_sales_in_state (MWh)

    emissions_targets
        owned_delivered, parent_name, utility_name_irp, utility_type_rmi, year,
        emissions_co2_historical (MMT), emissions_co2_target (MMT),
        emissions_co2_irp (MMT), emissions_co2_1point5c (MMT),
        net_generation_mwh, net_generation_mwh_1point5c
        Note: historical columns stop where projections begin; there is no
        separate historical-vs-projected generation column.

    operations_emissions_by_tech
        year, parent_name, utility_name, utility_id_eia, utility_type_rmi,
        plant_id_eia, plant_name_eia, generator_id, state, city, county,
        latitude, longitude, balancing_authority_code_eia, iso_rto_code,
        nerc_region, operational_status_code, operating_year, retirement_year,
        energy_source, owned_energy_source, technology_eia, technology_rmi,
        capacity (GW), year_end_capacity (GW), net_generation (TWh),
        potential_generation (TWh), capacity_factor, fuel_consumed,
        emissions_co2 (MMT), emissions_nox (t), emissions_sox (t)

    operations_emissions_by_fuel
        Same grain but split by energy_source_code / fuel_type_category;
        no capacity or capacity_factor columns.

    operations_emissions
        State-level rollup: parent_name, utility_name, utility_type_rmi, year,
        state, owned_energy_source, technology_RMI, capacity,
        potential_generation, fuel_type_category, net_generation,
        emissions_CO2, emissions_nox, emissions_sox

    customers_sales
        parent_name, utility_name, utility_id_ferc1, year, customer_type,
        customer_type_rmi (residential / commercial / industrial /
        sales_for_resale / other), customers, sales (MWh), revenue ($)

    assets_earnings_investments / revenue_by_tech
        parent_name, utility_name, utility_id_ferc1, year, group, category,
        detail, rate_base ($), investments ($), earnings ($), revenue ($).
        revenue_by_tech adds component, revenue_residential,
        residential_monthly_bill.

    net_plant_balance
        parent_name, utility_name, utility_id_ferc1, year, technology_ferc,
        technology, original_cost, accum_depr, net_plant_balance,
        arc_gross, arc_accum_depr, arc_net

    debt_equity_returns
        parent_name, ticker, utility_name, utility_id_ferc1, year,
        rate_base_realized, equity_ratio, roe, ror, interest_rate,
        and the _realized / _authorized variants

    expenditure_bills_burden / housing_units_income
        parent_name, utility_name, respondent_id, year, percent_AMI,
        ownership, plus expenditure/bill/burden or housing_units/income

    reliability
        parent_name, utility_name, utility_id_eia, utility_type_rmi, year,
        state, reliability_standard, outage_conditions (normal_conditions /
        major_events), customers, interruptions, minutes, saidi, saifi, caidi

    state_policies
        state, state_abbr, securitization_policy, market_indexing_policy,
        fuel_pass_through, governor_party, legislation_majority_party
    """


def read_pdf(filename: str) -> str:
    """Extract text from a PDF in the data directory."""
    pdf_path = DATA_DIR / filename
    if not pdf_path.exists():
        return f"{filename} not found in {DATA_DIR}."
    doc = pymupdf.open(str(pdf_path))
    return "\n".join(page.get_text() for page in doc)


@mcp.resource("rmi://methodology")
def methodology() -> str:
    """RMI's methodology document: data sources, assumptions, and calculations."""
    return read_pdf("RMI Utility Transition Hub Methodology.pdf")


@mcp.resource("rmi://data-dictionary-full")
def data_dictionary_full() -> str:
    """Full data dictionary: definitions, units, sources, and methodology for every field."""
    return read_pdf("Data Dictionary.pdf")
