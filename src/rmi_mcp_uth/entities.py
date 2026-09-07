"""Reconciling composite filing entities with the operating utilities behind them.

emissions_targets keys on utility_name_irp — the entity that files the IRP —
while the operations tables key on the EIA operating utility. Usually those are
the same name. Where several utilities file jointly, they are not: RMI carries
one target row naming all of them, and the operations tables carry each
separately.

A search term then resolves to a wider fleet on one side than the other, and
two tools answer at different scopes with nothing saying so. The size of that
gap depends on how much generation the extra components own, so it can be large
for one entity and nil for the next — which makes it invisible in the common
case rather than obviously wrong.

Nothing here hardcodes which entities are composite, or which names to split.
Components are read out of the name and confirmed against the target table at
call time, so a new joint filer or a renamed component is picked up without a
code change.

Composites in the current data snapshot, with the components that resolve to
operating utilities carrying generation:

  Louisville Gas & Electric Co. and Kentucky Utilities Co.   both
  Evergy Kansas South, Inc. and Evergy Kansas Central, Inc.  both
  Monongahela Power Co. and The Potomac Edison Co.           one; Potomac
      Edison owns no generation
  Minnkota Power Coop. and Northern Municipal Power Agency   one; NMPA is a
      joint action agency and owns no generators

On the owned basis each composite's target equals the summed owned emissions of
its components exactly (ratio 1.0000, 2005-2023), which is why widening a
generation query to the components reconstructs RMI's own aggregation rather
than inventing one. On the delivered basis it does not and cannot: delivered
emissions follow power sold, not generated.
"""

from .names import (
    TARGET_NAME_COLUMNS,
    name_params,
    name_predicate,
    normalize_name,
    normalized_column,
)


# Only " and ". Never "&": it is a conjunction inside single names, not between
# them, in 24 of 197 IRP names and 277 operations names — splitting on it would
# halve every one of them. Add a separator here if RMI adopts another.
COMPOSITE_SEPARATORS = (" and ",)


def split_composite(name: str, separators=COMPOSITE_SEPARATORS) -> list[str]:
    """The component names inside a composite entity name; empty if it is not one.

    Splits the raw name, not the normalized one. normalize_name rewrites "&" to
    " and ", so normalizing first would split the many single names that carry
    an ampersand.
    """
    for separator in separators:
        if separator in name:
            parts = [part.strip() for part in name.split(separator) if part.strip()]
            if len(parts) > 1:
                return parts
    return []


def resolve_names(db, table: str, column: str, names: list[str]) -> list[str]:
    """Values of `column` in `table` that the given names match, fuzzily.

    One scan for the whole list rather than one per name. A name that resolves
    to nothing is simply absent from the result: a component may be an entity
    that files jointly but owns no assets of the kind this table records.
    """
    names = [n for n in names if n and n.strip()]
    if not names:
        return []
    clause = " OR ".join([f"{normalized_column(column)} LIKE ?"] * len(names))
    rows = db.execute(
        f"SELECT DISTINCT {column} FROM {table} WHERE {clause}",
        [f"%{normalize_name(n)}%" for n in names],
    ).fetchall()
    return sorted(row[0] for row in rows if row[0] is not None)


def composite_entities(
    db,
    utility_name: str,
    source_table: str = "emissions_targets",
    source_column: str = "utility_name_irp",
    source_search_columns: list[str] = TARGET_NAME_COLUMNS,
    target_table: str = "operations_emissions_by_tech",
    target_column: str = "utility_name",
) -> dict[str, list[str]]:
    """Composite entities this search matches, each mapped to its components.

    Empty for the ordinary case, where every matched entity names a single
    operating utility. The defaults describe the one direction the tools need —
    filing entity to operating utility — but neither end is baked in.
    """
    matched = db.execute(
        f"""
        SELECT DISTINCT {source_column} FROM {source_table}
        WHERE ({name_predicate(source_search_columns)})
          AND {source_column} IS NOT NULL
        """,
        name_params(source_search_columns, utility_name),
    ).fetchall()

    composites = {}
    for (entity,) in matched:
        parts = split_composite(entity)
        if not parts:
            continue
        found = resolve_names(db, target_table, target_column, parts)
        if found:
            composites[entity] = found
    return composites
