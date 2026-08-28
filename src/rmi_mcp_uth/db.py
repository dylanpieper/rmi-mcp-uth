"""DuckDB connection, built from the RMI CSVs on first run."""

import duckdb

from .config import DATA_DIR, DB_PATH


_DB: duckdb.DuckDBPyConnection | None = None


def get_db() -> duckdb.DuckDBPyConnection:
    """Return a cached read-only DuckDB connection, building it from CSVs if needed."""
    global _DB
    if _DB is not None:
        return _DB

    if DB_PATH.exists():
        probe = duckdb.connect(str(DB_PATH), read_only=True)
        if probe.execute("SHOW TABLES").fetchall():
            _DB = probe
            return _DB
        probe.close()

    db = duckdb.connect(str(DB_PATH))
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
    _DB = duckdb.connect(str(DB_PATH), read_only=True)
    return _DB
