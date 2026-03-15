# Skill: Power Market Data

## Trigger
When the user types `/power` — or any natural language variant like "show me yesterday's power prices", "open the dashboard", or "fetch settlement prices" — immediately run the command below. Do not ask for confirmation, do not explain what you are about to do. Just run it.

## Action: run this command immediately

```bash
python skills/power/server.py
```

That's it. No configuration. No JSON. Just run the script.

The server handles everything:
- Reads `TENNET_API_KEY` and `ENTSOE_API_KEY` from `.env`
- Fetches yesterday's TenneT settlement prices (95 PTUs) and ENTSO-E day-ahead prices
- Starts a dashboard at `http://127.0.0.1:5050` and opens it in the browser
- If already running, just opens the browser tab

## After the script output
Add **2–3 notable moments** from the data. For each:
- State the time
- State the price or event
- Give a one-line market interpretation

Focus on:
- Daily price peak and trough (demand spike? solar surplus?)
- Negative prices (excess renewables forcing TenneT to pay for absorption)
- Sharp single-PTU price jumps (sudden balance shift or reserve activation)
- Unusually long UP or DOWN streaks (persistent system imbalance)
- Large shortage/surplus spread (high spread = volatile balancing conditions)

## Data fields reference
| Field | Meaning |
|---|---|
| `shortage` | Price when system is short — upward regulation price |
| `surplus` | Price when system is long — downward regulation price |
| `dispatch_up` | Marginal price of the most expensive upward reserve activated |
| `dispatch_down` | Marginal price of the cheapest downward reserve activated |
| `regulation_state` | `1`=UP · `-1`=DOWN · `0`=STABLE · `2`=UP_AND_DOWN |

## API details (for reference only — the script handles this)
- Endpoint: `https://api.tennet.eu/publications/v1/settlement-prices`
- Auth header: `apikey: <TENNET_API_KEY>`
- Date format: `dd-mm-yyyy hh:mi:ss`
- Rate limit: 1 req/s · 5/min · **25/day** — do not run repeatedly
