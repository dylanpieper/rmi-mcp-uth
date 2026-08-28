"""Paths and the shared FastMCP instance."""

from pathlib import Path

from fastmcp import FastMCP

# The CSVs and the database sit at the repo root, two levels above this file
# (src/rmi_mcp_uth/config.py) — not inside the package.
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = ROOT / "utility_hub.duckdb"

mcp = FastMCP("rmi-mcp-uth")
