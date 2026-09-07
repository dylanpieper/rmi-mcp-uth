"""The envelope every tool returns, and the units its columns carry.

One shape for every call — rows, units, meta, notes, error — so a caller
building a CSV or a report never has to know which tool produced the payload.
Control records never sit inside `rows`: a row is data, always, and every row
in one response carries the same keys.
"""

import decimal
import math

import numpy as np
import pandas as pd


# Unit per column, looked up case-insensitively (operations_emissions spells
# them emissions_CO2 and technology_RMI). Covers the columns the tools compute
# and the raw table columns query_data and preview_table hand straight through,
# so a CSV header built from any tool can be labelled. Columns absent here are
# unitless — names, years, counts of customers or housing units, category
# labels like percent_AMI ("0-30%", "100%+"), which reads as a unit but is a
# bucket name.
#
# Verified against RMI's Data Dictionary and Methodology PDFs and against the
# magnitudes in the database. One deliberate divergence: RMI publishes emissions
# intensity in metric tons/MWh, and the tools report kg CO2/MWh — 1000x its
# figure, so a coal fleet reads 1012 here against RMI's 1.01.
UNITS = {
    # CO2, in million metric tons
    "emissions_co2": "MMT CO2",
    "emissions_co2_mmt": "MMT CO2",
    "emissions_co2_historical": "MMT CO2",
    "emissions_co2_target": "MMT CO2",
    "emissions_co2_irp": "MMT CO2",
    "emissions_co2_1point5c": "MMT CO2",
    "gap_vs_1point5c": "MMT CO2",
    "gap_mmt": "MMT CO2",
    # other pollutants, in metric tons
    "emissions_nox": "metric tons",
    "emissions_sox": "metric tons",
    # generation
    "net_generation": "TWh",
    "net_generation_twh": "TWh",
    "net_generation_twh_1point5c": "TWh",
    "potential_generation": "TWh",
    "potential_generation_twh": "TWh",
    "net_generation_mwh": "MWh",
    "net_generation_mwh_1point5c": "MWh",
    "mwh_sales_in_state": "MWh",
    "sales": "MWh",
    "fuel_consumed": "MMBtu",
    # capacity
    "capacity": "GW",
    "capacity_gw": "GW",
    "year_end_capacity": "GW",
    # RMI documents these two at plant level and states the "multiplied by
    # ownership fractions" step for every other capacity metric but not for
    # them. They run several times above the ownership share and, summed across
    # a state's utilities, above that state's real capacity — so they are
    # flagged non-additive and labelled as not being the share.
    "capacity_owned_in_state": "MW (RMI plant basis, not the ownership share)",
    "capacity_operated_in_state": "MW (RMI plant basis, not the ownership share)",
    "capacity_owned_mw": "MW",
    # intensity — RMI publishes metric tons/MWh; these are 1000x that
    "co2_intensity_kg_mwh": "kg CO2/MWh",
    "co2_intensity_1point5c_kg_mwh": "kg CO2/MWh",
    "intensity_gap_kg_mwh": "kg CO2/MWh",
    # ratios, as fractions of 1 unless the name says percent
    "capacity_factor": "fraction",
    "fraction_owned_utility": "fraction",
    "equity_ratio": "fraction",
    "equity_ratio_realized": "fraction",
    "roe": "fraction",
    "roe_realized": "fraction",
    "ror": "fraction",
    "ror_realized": "fraction",
    "interest_rate": "fraction",
    "interest_rate_realized": "fraction",
    "interest_rate_authorized": "fraction",
    "effective_fed_tax_rate": "fraction",
    "burden": "fraction of income",
    "pct_over": "%",
    "pct_over_1point5c": "%",
    # money
    "revenue": "USD",
    "revenue_residential": "USD",
    "expenditure": "USD",
    "rate_base": "USD",
    "rate_base_realized": "USD",
    "investments": "USD",
    "earnings": "USD",
    "earnings_realized": "USD",
    "earnings_authorized": "USD",
    "equity_realized": "USD",
    "equity_authorized": "USD",
    "debt_realized": "USD",
    "debt_authorized": "USD",
    "returns_realized": "USD",
    "returns_authorized": "USD",
    "interest_realized": "USD",
    "interest_authorized": "USD",
    "fed_tax_expense_realized": "USD",
    "pre_tax_net_income_realized": "USD",
    "original_cost": "USD",
    "accum_depr": "USD",
    "net_plant_balance": "USD",
    "arc_gross": "USD",
    "arc_accum_depr": "USD",
    "arc_net": "USD",
    "bill": "USD/month",
    "residential_monthly_bill": "USD/month",
    "income": "USD/year",
    # reliability — saidi and saifi are per customer, caidi per interruption;
    # `minutes` and `interruptions` are the undivided totals they come from
    "saidi": "minutes/customer",
    "saifi": "interruptions/customer",
    "caidi": "minutes/interruption",
    "minutes": "customer-minutes",
    "interruptions": "customer-interruptions",
    # location
    "latitude": "degrees",
    "longitude": "degrees",
}

_UNITS = {name.lower(): unit for name, unit in UNITS.items()}


# Columns that must never be summed across rows, even where the rows are
# distinct. Most are rates and have to be recomputed from the two totals
# underneath them. The exception is worth stating: capacity_owned_in_state and
# capacity_operated_in_state are plain absolute MW, yet still not additive —
# RMI credits every co-owner the whole plant, so adding them up counts a
# jointly owned plant once per owner. A unit string alone cannot express that,
# which is why this is its own list rather than a rule read off `units`.
NON_ADDITIVE = {
    # whole-plant capacity, counted once per owner
    "capacity_owned_in_state",
    "capacity_operated_in_state",
    # rates: recompute from the numerator and denominator
    "capacity_factor",
    "co2_intensity_kg_mwh",
    "co2_intensity_1point5c_kg_mwh",
    "intensity_gap_kg_mwh",
    "pct_over",
    "pct_over_1point5c",
    "saidi",
    "saifi",
    "caidi",
    "bill",
    "burden",
    "residential_monthly_bill",
    "fraction_owned_utility",
    "equity_ratio",
    "equity_ratio_realized",
    "roe",
    "roe_realized",
    "ror",
    "ror_realized",
    "interest_rate",
    "interest_rate_realized",
    "interest_rate_authorized",
    "effective_fed_tax_rate",
    # coordinates
    "latitude",
    "longitude",
}

_NON_ADDITIVE = {name.lower() for name in NON_ADDITIVE}


def non_additive_in(columns) -> list:
    """Which of these columns must not be summed across rows."""
    return [c for c in columns if str(c).lower() in _NON_ADDITIVE]


def units_for(columns) -> dict:
    """The unit of every column that has one, in the order the columns appear."""
    return {c: _UNITS[str(c).lower()] for c in columns if str(c).lower() in _UNITS}


def jsonable(value):
    """A pandas or numpy value as JSON: NaN and NaT to None, arrays to lists.

    DuckDB hands back numpy scalars and, for a LIST column, a numpy array —
    neither serializes, so the MCP response fails validation without this.
    """
    if isinstance(value, (list, tuple, np.ndarray)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is pd.NaT or value is pd.NA:
        return None
    return value


def respond(
    rows, grain: list[str] | None, meta: dict | None = None, notes: list | None = None
) -> dict:
    """Wrap a DataFrame or a list of dicts in the standard envelope.

    `grain` is the list of columns that together identify one row — the one
    thing a caller cannot recover from the columns alone when several tools
    roll the same data up to different levels. A prose phrase would read just
    as well and could quietly stop being true; naming the columns lets a caller
    check the claim, and lets the tests check it on every tool. None means the
    rows have no key this side of the call, as when the caller wrote the query.
    """
    if hasattr(rows, "to_dict"):
        columns = list(rows.columns)
        rows = rows.to_dict(orient="records")
    else:
        columns = list(rows[0]) if rows else []
    if grain:
        missing = [c for c in grain if c not in columns]
        assert not missing, f"grain names columns that are not in the rows: {missing}"
    head = {"grain": grain}
    unsummable = non_additive_in(columns)
    if unsummable:
        head["non_additive"] = unsummable
    return {
        "rows": [{k: jsonable(v) for k, v in row.items()} for row in rows],
        "units": units_for(columns),
        "meta": {**head, **(meta or {})},
        "notes": notes or [],
        "error": None,
    }


def fail(message: str, **detail) -> dict:
    """An error envelope. `rows` is empty whenever `error` is set."""
    return {
        "rows": [],
        "units": {},
        "meta": {},
        "notes": [],
        "error": {"message": message, **detail},
    }


def note(kind: str, message: str, **detail) -> dict:
    """A warning that travels beside the rows rather than inside them."""
    return {"kind": kind, "message": message, **detail}
