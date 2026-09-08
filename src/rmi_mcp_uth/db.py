"""DuckDB connections, built from the RMI CSVs on first run.

Two policies, deliberately different, because the two jobs need different
powers. Ingest reads CSVs off disk and writes tables; serving reads one database
file and nothing else. Keeping them apart means the permissive one exists only
while the build runs.
"""

import duckdb

from .config import DATA_DIR, DB_PATH


_DB: duckdb.DuckDBPyConnection | None = None


# Reading the RMI CSVs is external file access, so ingest must allow it, and
# creating tables means it cannot be read-only. Short-lived by design: it is
# opened for the build and closed before anything serves a query.
INGEST_CONFIG: dict = {}

# What every tool and every query_data call runs against.
#
# A read-only connection stops writes but not reads: DuckDB lets a plain SELECT
# reach the filesystem through table functions — read_csv, read_text, glob — and
# pull in remote data through autoloaded extensions. No tool needs either, and
# query_data hands the caller arbitrary SELECT, so the serving connection gives
# up both. These settings are startup-only in DuckDB and cannot be re-enabled by
# a query, so the restriction holds for the life of the connection rather than
# depending on the statement check catching every shape.
SERVE_CONFIG: dict = {
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
}


def connect_serving() -> duckdb.DuckDBPyConnection:
    """Open the built database read-only, under the serving policy."""
    return duckdb.connect(str(DB_PATH), read_only=True, config=SERVE_CONFIG)


def get_db() -> duckdb.DuckDBPyConnection:
    """Return a cached serving connection, building the database if needed."""
    global _DB
    if _DB is not None:
        return _DB

    if DB_PATH.exists():
        probe = connect_serving()
        if probe.execute("SHOW TABLES").fetchall():
            _DB = probe
            return _DB
        probe.close()

    db = duckdb.connect(str(DB_PATH), config=INGEST_CONFIG)
    print("First run — loading RMI data into DuckDB...")

    csv_files = {
        "utility_information": "utility_information.csv",
        "utility_information_2023": "utility_information_2023.csv",
        "utility_state_map": "utility_state_map.csv",
        "utility_state_map_2023": "utility_state_map_2023.csv",
        "emissions_targets": "emissions_targets.csv",
        "operations_emissions_by_tech": "operations_emissions_by_tech.csv",
        "operations_emissions_by_fuel": "operations_emissions_by_fuel.csv",
        "operations_emissions": "operations_emissions.csv",
        "customers_sales": "customers_sales.csv",
        "assets_earnings_investments": "assets_earnings_investments.csv",
        "debt_equity_returns": "debt_equity_returns.csv",
        "expenditure_bills_burden": "expenditure_bills_burden.csv",
        "housing_units_income": "housing_units_income.csv",
        "net_plant_balance": "net_plant_balance.csv",
        "revenue_by_tech": "revenue_by_tech.csv",
        "reliability": "reliability.csv",
        "state_policies": "state_policies.csv",
    }

    for table, filename in csv_files.items():
        filepath = DATA_DIR / filename
        if filepath.exists():
            db.execute(
                f"CREATE TABLE {table} AS "
                f"SELECT * FROM read_csv_auto('{filepath}', header=true)"
            )
            count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count:,} rows")
        else:
            print(f"  SKIPPED {table}: {filename} not found")

    print("Done. Delete utility_hub.duckdb to rebuild.\n")
    db.close()
    _DB = connect_serving()
    return _DB
