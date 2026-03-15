# Power Market Skill

A Claude Code skill for monitoring Dutch power market data from TenneT and ENTSO-E.

## Overview

This skill enables Claude to fetch, parse, and summarize real-time and historical data from the Dutch electricity market, including imbalance prices, settlement prices, and cross-border flows.

## Data Sources

### TenneT
- **Settlement Prices (Onbalans)** — imbalance settlement prices per PTU (15-min period)
- **Balance Delta** — real-time system balance data
- **Incident Reserve** — activated reserve data

API base: `https://www.tennet.org/xml/`

### ENTSO-E Transparency Platform
- **Day-ahead prices** — SDAC market clearing prices for the NL bidding zone
- **Load forecasts** — actual and forecast load for the Netherlands
- **Generation per type** — breakdown by fuel type (wind, solar, gas, etc.)
- **Cross-border flows** — physical flows on NL interconnectors

API base: `https://web-api.tp.entsoe.eu/api` (requires API key)

## Planned Features

- Fetch current and historical imbalance prices from TenneT
- Fetch day-ahead electricity prices (ENTSO-E) for the NL bidding zone
- Compare imbalance vs. day-ahead price spreads
- Summarize market conditions in natural language
- Alert on extreme imbalance prices or unusual system conditions

## Usage

Once installed as a Claude Code skill, you can ask things like:

- "What is the current imbalance price?"
- "Show me today's settlement prices from TenneT"
- "Compare day-ahead prices with imbalance prices for yesterday"
- "What was the average onbalans price last week?"

## Requirements

- ENTSO-E API key (register at [transparency.entsoe.eu](https://transparency.entsoe.eu))
- Internet access to reach TenneT and ENTSO-E APIs
