"""Canned workflows for common analyses."""

from .config import mcp


@mcp.prompt
def decarbonization_assessment(utility_name: str) -> str:
    """Assess a utility's decarbonization progress against the 1.5°C pathway."""
    return (
        f"Analyze the decarbonization progress of '{utility_name}' using the "
        f"RMI Utility Transition Hub data.\n\n"
        f"1. Use get_climate_alignment to compare emissions_co2_historical "
        f"against emissions_co2_1point5c (delivered basis)\n"
        f"2. Use get_emissions_trend to see where stated targets and the IRP "
        f"projection diverge from the pathway in the outer years\n"
        f"3. Use get_generation_mix for the owned technology_rmi breakdown\n"
        f"4. Summarize:\n"
        f"   - How have their CO2 emissions changed over the past decade?\n"
        f"   - What share of owned net_generation is fossil vs. clean?\n"
        f"   - Are they above or below the 1.5°C pathway, and does that hold "
        f"through 2035 under their own targets?\n"
        f"   - What are the key risks or opportunities in their transition?\n"
        f"\nState the basis (owned vs delivered) in your answer — the two "
        f"tell different stories for utilities that buy a lot of power.\n"
    )


@mcp.prompt
def state_landscape(state_abbr: str) -> str:
    """Overview of utility emissions and clean energy progress in a state."""
    return (
        f"Give me an overview of the utility landscape in {state_abbr} using the "
        f"RMI Utility Transition Hub data.\n\n"
        f"1. Use list_utilities(state_abbr='{state_abbr}') to find all utilities\n"
        f"2. For the largest 3-5 by capacity, pull emissions trends\n"
        f"3. Summarize:\n"
        f"   - Which utilities are the biggest emitters "
        f"(emissions_co2_historical)?\n"
        f"   - Who is making the most progress toward the 1.5°C pathway?\n"
        f"   - What is the overall trajectory for {state_abbr}?\n"
    )


@mcp.prompt
def investment_risk_profile(utility_name: str) -> str:
    """Assess a utility's financial exposure to fossil fuel assets."""
    return (
        f"Analyze the investment risk profile of '{utility_name}' using the "
        f"RMI Utility Transition Hub data.\n\n"
        f"1. Use query_data to pull rate_base, investments, and earnings by "
        f"group/category/detail from assets_earnings_investments, and "
        f"net_plant_balance by technology for undepreciated fossil assets\n"
        f"2. Use get_generation_mix to see their current technology breakdown\n"
        f"3. Use get_emissions_trend to see where they stand on the 1.5°C pathway\n"
        f"4. Summarize:\n"
        f"   - How much capital is tied up in fossil assets (coal, gas)?\n"
        f"   - What share of their rate base is at risk of stranding?\n"
        f"   - Does their generation mix suggest they're actively transitioning?\n"
        f"   - How does their emissions trajectory align with their investment pattern?\n"
    )
