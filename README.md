# MCP Server for RMI's Utility Transition Hub

This MCP server loads [RMI's public Utility Transition Hub data](https://utilitytransitionhub.rmi.org/data-download/) into DuckDB. Any MCP client can then query U.S. utility emissions, generation mix, and climate alignment with natural language.

> [!WARNING]
> **This is a demo. Use it at your own risk.**
>
> This software is not audited, validated, or ready for production. The tools can
> return errors. The tools can also aggregate the data incorrectly, and a language
> model can interpret the results incorrectly.
>
> Check every result against the [Utility Transition Hub](https://utilitytransitionhub.rmi.org/)
> and the RMI methodology before you cite it, publish it, or make a decision with
> it. RMI does not endorse this tool. This tool has no affiliation with RMI.

## Setup

### 1. Install the Dependencies

```bash
pip install fastmcp duckdb pandas pymupdf
```

Or use uv:

```bash
uv init rmi-mcp-uth
cd rmi-mcp-uth
uv add fastmcp duckdb pandas pymupdf
```

### 2. Download the RMI Data

Download the data from the [Utility Transition Hub](https://utilitytransitionhub.rmi.org/data-download/). Extract the data into a `data/` directory:

```bash
mkdir data
cd data

curl -LO https://utilitytransitionhub.rmi.org/static/data_download/data_download_all_pt_1.zip
curl -LO https://utilitytransitionhub.rmi.org/static/data_download/data_download_all_pt_2.zip

unzip -q -o -j data_download_all_pt_1.zip
unzip -q -o -j data_download_all_pt_2.zip

rm data_download_all_pt_1.zip data_download_all_pt_2.zip
```

### 3. Test With the MCP Inspector

```bash
npx @modelcontextprotocol/inspector python server.py
```

This command opens a browser interface. In the interface, you can call each tool, examine the resources, and monitor the protocol traffic.

### 4. Connect a Client

The server uses the stdio transport, and any MCP client can connect to it. See your provider's documentation or ask your agent (e.g., Claude Code) to set it up.

## What the Server Exposes

### Tools

| Tool | What it does |
|---|---|
| `list_tables` | Show all tables, columns, and row counts |
| `preview_table` | Show sample rows from a table |
| `list_utilities` | Find utilities by state or name. The best matches come first. |
| `get_emissions_trend` | Show CO2 emissions over time for a utility. `start_year` and `end_year` limit the range. |
| `get_generation_mix` | Show the generation breakdown by `technology_rmi`. `group_by="technology"` combines the subsidiaries of a parent into one fleet. |
| `get_climate_alignment` | Compare the actual CO2 to the 1.5°C pathway. `start_year` and `end_year` limit the range. |
| `rank_climate_alignment` | Rank utilities or parents by distance from the 1.5°C pathway. Excludes the utilities whose benchmark makes the comparison meaningless, and says which. |
| `query_data` | Run read-only SQL in DuckDB syntax |

### Resources

| URI | Description |
|---|---|
| `rmi://data-dictionary` | A summary of the datasets, columns, and units |
| `rmi://data-dictionary-full` | The full data dictionary PDF. It gives the definitions, sources, and methodology for each field. |
| `rmi://methodology` | The RMI methodology document. It gives the data sources, assumptions, and calculations. |

### Prompts

| Prompt | Description |
|---|---|
| `decarbonization_assessment` | Analyze the climate progress of a utility |
| `state_landscape` | Show an overview of the utilities in a state |
| `investment_risk_profile` | Show the financial exposure of a utility to fossil fuel assets |

## Example Conversations

- "Which US utilities are furthest off the 1.5°C pathway?"
- "Which Wisconsin utilities have the highest CO2 emissions?"
- "Compare Alliant Energy and Xcel Energy's generation mix"
- "Show me Wisconsin Power & Light's emissions trend since 2015"
- "Is Alliant Energy on track for 1.5°C, or is WEC Energy Group closer?"
- "What share of Wisconsin generation comes from renewables?"

## Rebuilding the Database

Delete `utility_hub.duckdb` and restart the server. The server builds the database again from the CSV files.

## Data License

RMI licenses the Utility Transition Hub data under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
