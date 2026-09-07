"""Flags marking the utility-years where the 1.5C comparison breaks down."""

# RMI anchors each utility's 1.5C pathway to that utility's own 2005 emissions
# intensity. For a vertically integrated utility burning coal in 2005 the
# benchmark is a large number and the gap to it means something. For a utility
# whose 2005 default supply was already low-carbon, the benchmark starts near
# zero and decays toward it, so buying ordinary grid power reads as a
# several-hundred-percent overshoot of a fleet it does not own and did not
# choose. The flags below mark the utility-years where that comparison breaks
# down.
#
# They are deliberately narrow. Measured on the current data snapshot: of 164
# utilities with 2024 delivered data, 5 carry a pathway intensity under 100
# kg/MWh and 4 a 2005 baseline under 150, against a fleet median 2005 intensity
# of 653 kg/MWh. Those four, with 2005 intensity, 2024 benchmark, and the
# percentage overshoot the benchmark produces:
#
#   Peco Energy Co.               76 -> 43 kg/MWh    698%
#   Niagara Mohawk Power Corp.   129 -> 73 kg/MWh    203%
#   Green Mountain Power Corp.   120 -> 67 kg/MWh    141%
#   PUD No 2 of Grant County      98 -> 55 kg/MWh     43%
#
# The overshoot describes the regional grid mix these utilities buy from, not a
# fleet they operate. Utilities with a high 2005 baseline can sit well above the
# pathway too, and that is a real finding rather than an artifact, so the flags
# key on the baseline rather than on the overshoot.

from .entities import COMPOSITE_SEPARATORS

_BASELINE_YEAR = 2005
_LOW_BASELINE_KG_MWH = 150.0
_LOW_PATHWAY_KG_MWH = 100.0
_SERIES_BREAK_RATIO = 2.5
_LOAD_SHIFT_LOW = 0.6
_LOAD_SHIFT_HIGH = 1.6
_OWNED_SHARE_FLOOR = 0.05

CAVEATS = {
    "owns_no_generation": (
        "Owned-basis CO2 is under 5% of delivered CO2 — this utility buys its "
        "supply rather than generating it, so the delivered figure tracks the "
        "regional grid mix and its procurement contracts, not a fleet it can "
        "decarbonize."
    ),
    "low_baseline": (
        f"2005 emissions intensity was under {_LOW_BASELINE_KG_MWH:.0f} kg/MWh, "
        f"far below a typical fossil-heavy fleet. The pathway ramps down from "
        f"that anchor, so the benchmark is near zero and percentage overshoot "
        f"is unstable."
    ),
    "low_pathway_intensity": (
        f"The 1.5C benchmark for this year is under {_LOW_PATHWAY_KG_MWH:.0f} "
        f"kg/MWh. Percentage-over is suppressed; read the absolute gap instead."
    ),
    "series_break": (
        "Reported intensity jumped or fell more than "
        f"{_SERIES_BREAK_RATIO:g}x year-over-year while load stayed flat — a "
        "change in what the filing counts, not in physical emissions. The "
        "series is not measured consistently against its own 2005 anchor."
    ),
    "load_base_shift": (
        "Reported load changed by more than "
        f"{1 - _LOAD_SHIFT_LOW:.0%} in a single year, usually retail-choice "
        "migration moving customers off default service. The denominator is "
        "not the same customer base across the series."
    ),
    "invalid_generation": (
        "Net generation is zero, negative, or missing for this year, so "
        "intensity is undefined."
    ),
    "joint_filing": (
        "Several operating utilities file one IRP together, so this row covers "
        "a larger fleet than a single utility does. Ranked against single "
        "utilities it competes partly on being more than one of them, and its "
        "components are carried separately in the operations tables."
    ),
}


def _joint_filing_predicate(column: str) -> str:
    """SQL truth test for a name that joins several entities into one filing.

    Read off the name rather than a list of known entities, so a new joint
    filer is caught without a code change. Shares COMPOSITE_SEPARATORS with
    entities.py, which splits the same names into their components.
    """
    # Separators carry their own surrounding spaces on purpose: bare "and" is a
    # substring of ordinary place names.
    return " OR ".join(
        f"{column} LIKE '%{sep}%'" for sep in COMPOSITE_SEPARATORS
    )


def comparability_cte() -> str:
    """CTEs defining `targets_by_utility` and `comparability`.

    Flags are computed per (utility, basis, year). Every arithmetic input is
    summed across parent rows first: a jointly owned utility is split into one
    pro-rated row set per owner, so reading a single parent's row as the
    utility's total understates it (see trap 3 in the data dictionary).
    """
    joint = _joint_filing_predicate("s.utility_name_irp")
    return f"""
    targets_by_utility AS (
        SELECT
            utility_name_irp,
            owned_delivered,
            year,
            sum(emissions_co2_historical) AS co2,
            sum(emissions_co2_1point5c) AS co2_1point5c,
            sum(net_generation_mwh) AS mwh,
            sum(net_generation_mwh_1point5c) AS mwh_1point5c
        FROM emissions_targets
        GROUP BY utility_name_irp, owned_delivered, year
    ),
    basis_series AS (
        SELECT
            *,
            CASE WHEN mwh > 0 THEN co2 * 1e9 / mwh END AS kg_mwh,
            CASE WHEN mwh_1point5c > 0
                 THEN co2_1point5c * 1e9 / mwh_1point5c END AS pathway_kg_mwh
        FROM targets_by_utility
    ),
    owned_co2 AS (
        SELECT utility_name_irp, year, co2
        FROM targets_by_utility
        WHERE owned_delivered = 'owned'
    ),
    steps AS (
        SELECT
            utility_name_irp,
            owned_delivered,
            kg_mwh,
            mwh,
            lag(kg_mwh) OVER w AS prev_kg_mwh,
            lag(mwh) OVER w AS prev_mwh
        FROM basis_series
        WHERE co2 IS NOT NULL
        WINDOW w AS (PARTITION BY utility_name_irp, owned_delivered ORDER BY year)
    ),
    breaks AS (
        SELECT
            utility_name_irp,
            owned_delivered,
            -- A step change in intensity while load holds steady is a change in
            -- what the filing counts. If load moved too, load_base_shift owns it.
            max(CASE
                WHEN prev_kg_mwh > 0 AND kg_mwh > 0 AND prev_mwh > 0
                 AND mwh / prev_mwh BETWEEN {_LOAD_SHIFT_LOW} AND {_LOAD_SHIFT_HIGH}
                 AND (kg_mwh / prev_kg_mwh > {_SERIES_BREAK_RATIO}
                      OR kg_mwh / prev_kg_mwh < {1 / _SERIES_BREAK_RATIO})
                THEN 1 ELSE 0 END) AS series_break,
            max(CASE
                WHEN prev_mwh > 0
                 AND (mwh / prev_mwh < {_LOAD_SHIFT_LOW}
                      OR mwh / prev_mwh > {_LOAD_SHIFT_HIGH})
                THEN 1 ELSE 0 END) AS load_shift
        FROM steps
        GROUP BY utility_name_irp, owned_delivered
    ),
    baseline AS (
        SELECT
            utility_name_irp,
            owned_delivered,
            max(kg_mwh) FILTER (WHERE year = {_BASELINE_YEAR}) AS baseline_kg_mwh
        FROM basis_series
        GROUP BY utility_name_irp, owned_delivered
    ),
    comparability AS (
        SELECT
            s.utility_name_irp,
            s.owned_delivered,
            s.year,
            b.baseline_kg_mwh,
            s.pathway_kg_mwh,
            list_filter([
                CASE WHEN s.co2 > 0
                      AND coalesce(o.co2, 0) < {_OWNED_SHARE_FLOOR} * s.co2
                     THEN 'owns_no_generation' END,
                CASE WHEN b.baseline_kg_mwh < {_LOW_BASELINE_KG_MWH}
                     THEN 'low_baseline' END,
                CASE WHEN s.pathway_kg_mwh < {_LOW_PATHWAY_KG_MWH}
                     THEN 'low_pathway_intensity' END,
                CASE WHEN br.series_break = 1 THEN 'series_break' END,
                CASE WHEN br.load_shift = 1 THEN 'load_base_shift' END,
                CASE WHEN s.co2 IS NOT NULL AND coalesce(s.mwh, 0) <= 0
                     THEN 'invalid_generation' END,
                CASE WHEN {joint}
                     THEN 'joint_filing' END
            ], x -> x IS NOT NULL) AS comparability_flags
        FROM basis_series s
        LEFT JOIN owned_co2 o
               ON o.utility_name_irp = s.utility_name_irp AND o.year = s.year
        LEFT JOIN breaks br
               ON br.utility_name_irp = s.utility_name_irp
              AND br.owned_delivered = s.owned_delivered
        LEFT JOIN baseline b
               ON b.utility_name_irp = s.utility_name_irp
              AND b.owned_delivered = s.owned_delivered
    )
    """


def caveat_notes(flags) -> list[dict]:
    """Expand raw flag names into the explanations a caller needs to read a row."""
    seen = []
    for flag in flags:
        if flag not in seen:
            seen.append(flag)
    return [{"flag": f, "note": CAVEATS[f]} for f in seen if f in CAVEATS]
