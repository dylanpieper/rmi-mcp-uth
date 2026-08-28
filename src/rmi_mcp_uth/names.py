"""Fuzzy utility-name matching against RMI's abbreviated corporate names."""

import re

from .db import get_db


# RMI abbreviates corporate suffixes ("Southern Co.", "Xcel Energy, Inc."), so a
# literal LIKE on what a user types ("Southern Company") misses. Both the search
# term and the column are normalized through the same rules before comparing.
_NAME_ABBREVIATIONS = [
    ("company", "co"),
    ("corporation", "corp"),
    ("incorporated", "inc"),
    ("limited", "ltd"),
    ("association", "assn"),
]


def normalize_name(value: str) -> str:
    """Lowercase, drop punctuation, and collapse corporate suffixes."""
    text = re.sub(r"[.,]", " ", value.lower()).replace("&", " and ")
    for long_form, short_form in _NAME_ABBREVIATIONS:
        text = re.sub(rf"\b{long_form}\b", short_form, text)
    return re.sub(r"\s+", " ", text).strip()


def normalized_column(column: str) -> str:
    """SQL expression applying normalize_name's rules to a column."""
    expr = f"lower({column})"
    expr = f"regexp_replace({expr}, '[.,]', ' ', 'g')"
    expr = f"replace({expr}, '&', ' and ')"
    for long_form, short_form in _NAME_ABBREVIATIONS:
        expr = f"regexp_replace({expr}, '\\b{long_form}\\b', '{short_form}', 'g')"
    return f"trim(regexp_replace({expr}, '\\s+', ' ', 'g'))"


def name_predicate(columns: list[str]) -> str:
    """Build an OR'd fuzzy match over several name columns.

    Utilities are searchable by their own name or by their parent's, because
    people ask for "Xcel Energy" when the data files it under a subsidiary.
    """
    return " OR ".join(f"{normalized_column(c)} LIKE ?" for c in columns)


def name_params(columns: list[str], utility_name: str) -> list[str]:
    """One bound parameter per column in name_predicate."""
    return [f"%{normalize_name(utility_name)}%"] * len(columns)


def name_suggestions(
    table: str, columns: list[str], utility_name: str, limit: int = 5
) -> list[str]:
    """Names in `table` sharing a word with the failed search, to guide a retry."""
    tokens = [t for t in normalize_name(utility_name).split(" ") if len(t) > 2]
    if not tokens:
        return []
    db = get_db()
    union = " UNION ".join(
        f"SELECT DISTINCT {c} AS name FROM {table} WHERE {normalized_column(c)} LIKE ?"
        for c in columns
    )
    rows = db.execute(
        f"SELECT name FROM ({union}) WHERE name IS NOT NULL ORDER BY name LIMIT {limit}",
        [f"%{tokens[0]}%"] * len(columns),
    ).fetchall()
    return [row[0] for row in rows]


def name_rank(name_column: str, parent_column: str) -> str:
    """CASE expression scoring a match, lowest first.

    A bare LIKE ranks "Blazing Star Wind Farm, LLC" alongside "Northern States
    Power Co." for the search "Xcel Energy", because both merely contain the
    term somewhere. Exact and prefix hits on the utility's own name come first,
    then the same on the parent's, then everything else.
    """
    name = normalized_column(name_column)
    parent = normalized_column(parent_column)
    return f"""
        CASE
            WHEN {name} = ? THEN 0
            WHEN {name} LIKE ? THEN 1
            WHEN {parent} = ? THEN 2
            WHEN {parent} LIKE ? THEN 3
            ELSE 4
        END
    """


def name_rank_params(utility_name: str) -> list[str]:
    """Four bound parameters matching name_rank's CASE arms."""
    term = normalize_name(utility_name)
    return [term, f"{term}%", term, f"{term}%"]


# Every utility lookup searches the utility's own name and its parent's.
TARGET_NAME_COLUMNS = ["utility_name_irp", "parent_name"]
OPERATIONS_NAME_COLUMNS = ["utility_name", "parent_name"]
