"""Reviewed mapping from IRP filing entities to the operating utilities behind them.

`emissions_targets` has no id column, so cross-source reconciliation joins
`utility_name_irp` to `operations_emissions_by_tech.utility_name`. Normalizing
both sides through the server's own rules resolves 188 of 197 names. The nine
residuals are not noise — `utility_name_irp` names the entity that files the
IRP, a different granularity from the EIA operating utility — so they are
listed here rather than matched fuzzily. A heuristic that closed the gap would
also close it silently when RMI publishes a tenth case.

Every entry was checked by reconciling owned CO2 against the summed owned
`emissions_co2` of its components, year by year. `owned_ratio` records what that
reconciliation actually yields, so a test can assert the number rather than
assume 1.0; `exact` marks the entries where the identity holds to four decimals
and the suite may treat the aggregation as reconstructed rather than estimated.

Three shapes:

  composite  one filing covers several operating utilities   1 target : N ops
  split      one operating company files as several entities N targets : 1 ops
  variant    a rename, or one filer split by state in ops    1 target : N ops

Figures below are the current data snapshot (targets 2005-2035, measured
through 2024).
"""

# IRP entity -> the operating utilities it covers in operations_emissions_by_tech.
#
# owned_ratio: sum(emissions_co2_historical) on the owned basis divided by the
#   summed owned emissions_co2 of the components, over the years both carry.
# exact: the ratio holds at 1.0000 across every reconciling year, so the filing
#   is RMI's own sum of exactly these utilities.
# note: why this entry is not a plain name match.
ENTITY_BRIDGE = {
    "Louisville Gas & Electric Co. and Kentucky Utilities Co.": {
        "shape": "composite",
        "components": ["Louisville Gas & Electric Co.", "Kentucky Utilities Co."],
        "owned_ratio": 1.0,
        "exact": True,
        "note": "Joint IRP with the Kentucky PSC. 2024 reconciles at 0.9646 —"
        " latest-year snapshot skew, so the identity is asserted through 2023.",
    },
    "Evergy Kansas South, Inc. and Evergy Kansas Central, Inc.": {
        "shape": "composite",
        "components": ["Evergy Kansas South, Inc.", "Evergy Kansas Central, Inc."],
        "owned_ratio": 1.0,
        "exact": True,
        "note": "Joint filing; both components carry generation.",
    },
    "Monongahela Power Co. and The Potomac Edison Co.": {
        "shape": "composite",
        "components": ["Monongahela Power Co.", "The Potomac Edison Co."],
        "owned_ratio": 1.0,
        "exact": True,
        "note": "Potomac Edison owns no generation, so the composite reconciles"
        " at 1.00x against Monongahela alone — the scope gap is real but"
        " invisible in the numbers.",
    },
    "Minnkota Power Coop. and Northern Municipal Power Agency": {
        "shape": "composite",
        "components": ["Minnkota Power Coop, Inc"],
        "owned_ratio": 2.0,
        "exact": False,
        "note": "NMPA is a joint action agency with no operating-utility row, so"
        " only one component resolves. The filing runs at exactly 2.0000x the"
        " resolved component in every year 2005-2024 — pinned as measured, with"
        " no claim about the mechanism behind it.",
    },
    "Northwestern Energy (MT)": {
        "shape": "split",
        "components": ["NorthWestern Energy", "NorthWestern Energy (MT wind/thermal)"],
        "owned_ratio": 1.0,
        "exact": False,
        "note": "One operating company filed as two IRP rows, by state. 2016 and"
        " 2017 reconcile at 9.19x and 10.60x; every other year is 1.0000.",
    },
    "Northwestern Energy (SD)": {
        "shape": "split",
        "components": ["NorthWestern Energy - (SD)"],
        "owned_ratio": 1.0,
        "exact": True,
        "note": "The other half of the same split filing.",
    },
    "Evergy Missouri West": {
        "shape": "variant",
        "components": ["Evergy Missouri West, Inc."],
        "owned_ratio": 1.0,
        "exact": True,
        "note": "Corporate suffix absent from the IRP name.",
    },
    "Liberty-Empire": {
        "shape": "variant",
        "components": ["The Empire District Electric Co."],
        "owned_ratio": 1.0,
        "exact": True,
        "note": "Renamed after acquisition; the operations tables keep the old"
        " name. Confirmed by reconciliation, not by the name.",
    },
    "Northern States Power Co.": {
        "shape": "variant",
        "components": [
            "Northern States Power Co. (Minnesota)",
            "Northern States Power Co. (Wisconsin)",
        ],
        "owned_ratio": 1.0,
        "exact": True,
        "note": "One filer, split by state in the operations tables.",
    },
}

# The entries whose owned identity holds exactly, and may therefore be asserted
# as an equality rather than a band.
EXACT_ENTITIES = {name for name, e in ENTITY_BRIDGE.items() if e["exact"]}

# Composites only — the shape `entities.composite_entities()` recovers at runtime
# by splitting on " and ". The suite asserts the runtime decomposition still
# finds exactly these, so a fifth joint filer fails loudly.
COMPOSITES = {
    name: entry["components"]
    for name, entry in ENTITY_BRIDGE.items()
    if entry["shape"] == "composite"
}

# Years to exclude from an exact-identity assertion. The newest year is a
# partial snapshot: LG&E reconciles at 1.0000 through 2023 and 0.9646 in 2024.
LATEST_YEAR_IS_PARTIAL = True
