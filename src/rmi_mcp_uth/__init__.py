"""
RMI Utility Transition Hub — MCP Server
========================================
An MCP server that loads RMI's public Utility Transition Hub data into DuckDB
and exposes tools for any MCP client to query US utility emissions,
generation mix, and climate alignment via natural language.

See README.md for setup instructions.
"""

from .config import mcp

# Imported for the side effect of registering with `mcp`.
from . import prompts, resources, tools  # noqa: F401

__all__ = ["mcp", "main"]


def main() -> None:
    mcp.run()
