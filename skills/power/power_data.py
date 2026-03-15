import os
import sys
import json
from datetime import date, timedelta
from dotenv import load_dotenv
import requests

sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

api_key = os.getenv("TENNET_API_KEY")
yesterday = date.today() - timedelta(days=1)

url = "https://api.tennet.eu/publications/v1/settlement-prices"
params = {
    "date_from": f"{yesterday.strftime('%d-%m-%Y')} 00:00:00",
    "date_to":   f"{yesterday.strftime('%d-%m-%Y')} 23:59:59",
}
headers = {
    "apikey": api_key,
    "Accept": "application/json",
}

print(f"Fetching settlement prices for {yesterday.strftime('%d-%m-%Y')}...\n")
response = requests.get(url, params=params, headers=headers, timeout=15)
response.raise_for_status()

data = response.json()
points = data["Response"]["TimeSeries"][0]["Period"]["Points"]

# ── 1. Table ─────────────────────────────────────────────────────────────────

REG_LABELS = {
    1:  "UP       ",
    -1: "DOWN     ",
    0:  "STABLE   ",
    2:  "UP+DOWN  ",
}

def fmt(val, decimals=2):
    if val is None:
        return "   —   "
    return f"{float(val):>7.2f}"

header = f"{'Time':^11} │ {'Reg State':^9} │ {'Shortage':^9} │ {'Surplus':^9} │ {'Disp Up':^9} │ {'Disp Down':^9}"
divider = "─" * len(header)

print(divider)
print(f"  SETTLEMENT PRICES  ·  {yesterday.strftime('%A %d %B %Y')}")
print(divider)
print(header)
print(divider)

for p in points:
    time_str = p["timeInterval_start"][11:16]
    reg = p.get("regulation_state", 0)
    label = REG_LABELS.get(reg, f"{reg:^9}")
    print(
        f"{time_str:^11} │ {label} │"
        f" {fmt(p.get('shortage'))} │"
        f" {fmt(p.get('surplus'))} │"
        f" {fmt(p.get('dispatch_up'))} │"
        f" {fmt(p.get('dispatch_down'))}"
    )

print(divider)

# ── 2. Summary ───────────────────────────────────────────────────────────────

from collections import Counter

reg_counts = Counter(p.get("regulation_state", 0) for p in points)
reg_names  = {1: "UP", -1: "DOWN", 0: "STABLE", 2: "UP_AND_DOWN"}

def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None

shortage_vals = [(p["timeInterval_start"][11:16], safe_float(p.get("shortage")))
                 for p in points if safe_float(p.get("shortage")) is not None]
surplus_vals  = [(p["timeInterval_start"][11:16], safe_float(p.get("surplus")))
                 for p in points if safe_float(p.get("surplus")) is not None]

max_shortage = max(shortage_vals, key=lambda x: x[1])
min_shortage = min(shortage_vals, key=lambda x: x[1])
max_surplus  = max(surplus_vals,  key=lambda x: x[1])
min_surplus  = min(surplus_vals,  key=lambda x: x[1])

print(f"\n{'═'*60}")
print("  SUMMARY")
print(f"{'═'*60}")
print(f"  Total PTUs:  {len(points)}")
print()
print("  Regulation state breakdown:")
for state, name in sorted(reg_names.items()):
    count = reg_counts.get(state, 0)
    bar = "█" * count
    print(f"    {name:<12}  {count:>3}  {bar}")

print()
print("  Shortage price (€/MWh):")
print(f"    Max:   {max_shortage[1]:>7.2f}  at {max_shortage[0]}")
print(f"    Min:   {min_shortage[1]:>7.2f}  at {min_shortage[0]}")
print(f"    Delta: {max_shortage[1] - min_shortage[1]:>7.2f}")

print()
print("  Surplus price (€/MWh):")
print(f"    Max:   {max_surplus[1]:>7.2f}  at {max_surplus[0]}")
print(f"    Min:   {min_surplus[1]:>7.2f}  at {min_surplus[0]}")
print(f"    Delta: {max_surplus[1] - min_surplus[1]:>7.2f}")

# ── 3. ASCII bar chart ────────────────────────────────────────────────────────

BAR_WIDTH = 50

all_shortage = [v for _, v in shortage_vals]
s_min = min(all_shortage)
s_max = max(all_shortage)
s_range = s_max - s_min if s_max != s_min else 1

print(f"\n{'═'*60}")
print(f"  SHORTAGE PRICE BAR CHART  (min={s_min:.1f}  max={s_max:.1f} €/MWh)")
print(f"{'═'*60}")

for p in points:
    time_str = p["timeInterval_start"][11:16]
    val = safe_float(p.get("shortage"))
    if val is None:
        bar = ""
        label = "  —"
    else:
        length = int(round((val - s_min) / s_range * BAR_WIDTH))
        bar    = "▓" * length
        label  = f"  {val:.1f}"
    print(f"  {time_str} │{bar:<50}{label}")

# ── 4. Notable moments ───────────────────────────────────────────────────────

print(f"\n{'═'*60}")
print("  NOTABLE MOMENTS")
print(f"{'═'*60}")

# Build a structured summary for analysis
shortage_by_hour = {}
for p in points:
    hour = p["timeInterval_start"][11:13]
    v = safe_float(p.get("shortage"))
    if v is not None:
        shortage_by_hour.setdefault(hour, []).append(v)

# Find: longest consecutive DOWN streak
streaks = []
current_state = None
current_streak = []
for p in points:
    s = p.get("regulation_state")
    if s == current_state:
        current_streak.append(p)
    else:
        if current_streak:
            streaks.append((current_state, current_streak))
        current_state = s
        current_streak = [p]
if current_streak:
    streaks.append((current_state, current_streak))

down_streaks = [(s, pts) for s, pts in streaks if s == -1]
longest_down  = max(down_streaks, key=lambda x: len(x[1])) if down_streaks else None

# Find: largest single-PTU price jump
jumps = []
for i in range(1, len(points)):
    prev = safe_float(points[i-1].get("shortage"))
    curr = safe_float(points[i].get("shortage"))
    if prev is not None and curr is not None:
        jumps.append((abs(curr - prev), curr - prev, points[i]["timeInterval_start"][11:16], prev, curr))
biggest_jump = max(jumps, key=lambda x: x[0]) if jumps else None

# Find: negative price moment
neg_prices = [(p["timeInterval_start"][11:16], safe_float(p.get("shortage")))
              for p in points if safe_float(p.get("shortage")) is not None and safe_float(p.get("shortage")) < 0]

notable = []

# Notable 1: price spike
notable.append((
    max_shortage[0],
    max_shortage[1],
    f"🔺 {max_shortage[0]}  Highest shortage price of the day: {max_shortage[1]:.2f} €/MWh — "
    f"system was short and expensive upward reserves were needed."
))

# Notable 2: midday surplus collapse or negative price
if neg_prices:
    t, v = neg_prices[0]
    notable.append((t, v,
        f"⬇️  {t}  Negative shortage price ({v:.2f} €/MWh) — surplus of renewable generation "
        f"forced TenneT to pay to absorb excess power."
    ))
elif min_shortage[1] < 15:
    t, v = min_shortage
    notable.append((t, v,
        f"☀️  {t}  Shortage price collapsed to {v:.2f} €/MWh — likely high solar output "
        f"creating a system surplus and near-zero balancing cost."
    ))

# Notable 3: biggest price jump
if biggest_jump:
    delta, direction, t, prev, curr = biggest_jump
    direction_str = "spike" if direction > 0 else "drop"
    notable.append((t, curr,
        f"⚡ {t}  Largest single-PTU price {direction_str}: {prev:.2f} → {curr:.2f} €/MWh "
        f"(Δ {direction:+.2f}) — abrupt change in system balance or reserve activation."
    ))

for _, _, text in notable[:3]:
    print(f"\n  {text}")

print(f"\n{'═'*60}\n")
