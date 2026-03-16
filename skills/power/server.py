import os
import sys
import json
import socket
import statistics
import threading
import webbrowser
import concurrent.futures
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
import requests

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

tennet_key = os.getenv("TENNET_API_KEY")
entsoe_key = os.getenv("ENTSOE_API_KEY")
agsi_key   = os.getenv("AGSI_API_KEY")

REG_LABELS = {1: "UP", -1: "DOWN", 0: "STABLE", 2: "UP+DOWN"}
REG_COLORS = {1: "#d4edda", -1: "#d1ecf1", 0: "#f8f9fa", 2: "#fff3cd"}

ENTSOE_NS = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}
CET       = timezone(timedelta(hours=1))

# Crisis View — bidding zones and event periods
# Periods use UTC: CET 00:00 = UTC 23:00 previous day
CRISIS_ZONES = [
    ("NL", "Netherlands",  "10YNL----------L", "gas"),
    ("DE", "Germany",      "10Y1001A1001A82H", "gas"),
    ("BE", "Belgium",      "10YBE----------2", "gas"),
    ("FR", "France",       "10YFR-RTE------C", "low"),
    ("ES", "Spain",        "10YES-REE------0", "low"),
    ("NO", "Norway South", "10YNO-2--------T", "low"),
]
CRISIS_BEFORE_START = "202601312300"  # 1 Feb 2026 00:00 CET
CRISIS_BEFORE_END   = "202602272300"  # 28 Feb 2026 00:00 CET (exclusive)
CRISIS_AFTER_START  = "202603012300"  # 2 Mar 2026 00:00 CET
CRISIS_NL_START     = "202601312300"  # same as before-start for NL trajectory

NL_ZONE              = "10YNL----------L"
ENTSOE_HISTORY_START = "202601312300"   # 1 Feb 2026 00:00 CET

# ENTSO-E generation type definitions (code, display label, chart colour)
PSR_TYPES = [
    ("B04", "Fossil Gas",          "#FF6B6B"),
    ("B02", "Coal/Lignite",        "#8B7355"),
    ("B05", "Coal-derived Gas",    "#CC8844"),
    ("B06", "Fossil Oil",          "#AA6644"),
    ("B14", "Nuclear",             "#9B59B6"),
    ("B10", "Hydro Pumped",        "#5DADE2"),
    ("B01", "Biomass",             "#A3C97A"),
    ("B09", "Geothermal",          "#48C9B0"),
    ("B11", "Hydro Run-of-river",  "#1ABC9C"),
    ("B19", "Wind Onshore",        "#4FC3F7"),
    ("B18", "Wind Offshore",       "#1E90FF"),
    ("B16", "Solar",               "#FFD700"),
]
RENEWABLE_CODES = {"B01", "B09", "B11", "B16", "B18", "B19"}
FOSSIL_CODES    = {"B02", "B04", "B05", "B06"}

# Module-level caches (keyed by date string so they refresh daily)
_gas_cache        = {"data": None, "date": None}
_renewables_cache = {"data": None, "date": None}
_heatmap_cache    = {"data": None, "date": None}

# ── helpers ──────────────────────────────────────────────────────────────────

def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None

def fmt(val):
    if val is None:
        return "—"
    return f"{float(val):.2f}"

def to_js(lst):
    """Convert a Python list (with possible Nones) to a JS array literal."""
    return "[" + ",".join("null" if v is None else str(round(v, 2)) for v in lst) + "]"

# ── data fetching ─────────────────────────────────────────────────────────────

def fetch_tennet(yesterday):
    url = "https://api.tennet.eu/publications/v1/settlement-prices"
    params = {
        "date_from": f"{yesterday.strftime('%d-%m-%Y')} 00:00:00",
        "date_to":   f"{yesterday.strftime('%d-%m-%Y')} 23:59:59",
    }
    headers = {"apikey": tennet_key, "Accept": "application/json"}
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()["Response"]["TimeSeries"][0]["Period"]["Points"]


def fetch_entsoe_da(yesterday):
    """
    Returns a list of 96 day-ahead prices (€/MWh) in CET order.
    Index 0 = 00:00 CET.  None where missing.

    ENTSO-E UTC window: (yesterday-1)T23:00Z → yesterdayT23:00Z
    Position 1 in the response = 00:00 CET, aligning with TenneT PTU index 0.
    """
    prev_day = yesterday - timedelta(days=1)
    params = {
        "securityToken": entsoe_key,
        "documentType":  "A44",
        "in_Domain":     "10YNL----------L",
        "out_Domain":    "10YNL----------L",
        "periodStart":   prev_day.strftime("%Y%m%d") + "2300",
        "periodEnd":     yesterday.strftime("%Y%m%d") + "2300",
    }
    resp = requests.get("https://web-api.tp.entsoe.eu/api", params=params, timeout=15)
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    price_by_pos = {}
    for pt in root.findall(".//ns:Point", ENTSOE_NS):
        pos   = int(pt.find("ns:position",    ENTSOE_NS).text)
        price = float(pt.find("ns:price.amount", ENTSOE_NS).text)
        price_by_pos[pos] = price

    # positions are 1-indexed; map to 0-indexed list of length 96
    return [price_by_pos.get(i + 1) for i in range(96)]


def fetch_data():
    yesterday = date.today() - timedelta(days=1)
    points    = fetch_tennet(yesterday)
    da_prices = fetch_entsoe_da(yesterday)
    return yesterday, points, da_prices


# ── crisis data ───────────────────────────────────────────────────────────────

def parse_entsoe_prices(xml_text):
    """Parse ENTSO-E A44 XML → list of (utc_datetime, price_eur_mwh) tuples."""
    root   = ET.fromstring(xml_text)
    result = []
    for period in root.findall(".//ns:Period", ENTSOE_NS):
        start_el = period.find("ns:timeInterval/ns:start", ENTSOE_NS)
        if start_el is None:
            continue
        start_dt = datetime.fromisoformat(start_el.text.replace("Z", "+00:00"))
        res      = period.find("ns:resolution", ENTSOE_NS).text
        delta    = timedelta(hours=1) if res == "PT60M" else timedelta(minutes=15)
        for pt in period.findall("ns:Point", ENTSOE_NS):
            pos_el   = pt.find("ns:position",     ENTSOE_NS)
            price_el = pt.find("ns:price.amount", ENTSOE_NS)
            if pos_el is None or price_el is None:
                continue
            result.append((start_dt + (int(pos_el.text) - 1) * delta,
                           float(price_el.text)))
    return result


def fetch_entsoe_period(zone, period_start, period_end):
    params = {
        "securityToken": entsoe_key,
        "documentType":  "A44",
        "in_Domain":     zone,
        "out_Domain":    zone,
        "periodStart":   period_start,
        "periodEnd":     period_end,
    }
    resp = requests.get("https://web-api.tp.entsoe.eu/api", params=params, timeout=25)
    if not resp.ok:
        snippet = resp.text[:400].replace("\n", " ")
        raise RuntimeError(f"HTTP {resp.status_code} for {zone}: {snippet}")
    prices = parse_entsoe_prices(resp.text)
    if not prices:
        raise RuntimeError(f"No price data in response for {zone} ({period_start}→{period_end}). "
                           f"Response start: {resp.text[:200].replace(chr(10),' ')}")
    return prices


def build_crisis_data():
    """Fetch all crisis comparison data concurrently. Called from /api/crisis."""
    yesterday = date.today() - timedelta(days=1)
    after_end = yesterday.strftime("%Y%m%d") + "2300"

    def fetch_zone(args):
        code, name, zone, group = args
        try:
            before     = fetch_entsoe_period(zone, CRISIS_BEFORE_START, CRISIS_BEFORE_END)
            after      = fetch_entsoe_period(zone, CRISIS_AFTER_START,  after_end)
            before_med = statistics.median(p for _, p in before) if before else None
            after_med  = statistics.median(p for _, p in after)  if after  else None
            error      = None
        except Exception as exc:
            print(f"[CRISIS] {code}: {exc}", flush=True)
            before_med = after_med = None
            error = str(exc)
        return {
            "code":       code,
            "name":       name,
            "group":      group,
            "before_med": round(before_med, 2) if before_med is not None else None,
            "after_med":  round(after_med,  2) if after_med  is not None else None,
            "error":      error,
        }

    def fetch_nl_traj():
        try:
            prices = fetch_entsoe_period("10YNL----------L", CRISIS_NL_START, after_end)
            by_date = {}
            for dt, price in prices:
                d = dt.astimezone(CET).date()
                by_date.setdefault(d, []).append(price)
            return [{"date": str(d), "price": round(sum(ps) / len(ps), 2)}
                    for d, ps in sorted(by_date.items())]
        except Exception:
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
        traj_fut   = ex.submit(fetch_nl_traj)
        zone_futs  = [ex.submit(fetch_zone, z) for z in CRISIS_ZONES]
        countries  = [f.result() for f in zone_futs]
        nl_traj    = traj_fut.result()

    return {"countries": countries, "nl_trajectory": nl_traj, "yesterday": str(yesterday)}


# ── Gas Storage (AGSI+) ───────────────────────────────────────────────────────

def build_gas_storage_data():
    headers = {"x-key": agsi_key, "Accept": "application/json"}
    resp = requests.get("https://agsi.gie.eu/api",
                        params={"country": "NL", "size": 365},
                        headers=headers, timeout=20)
    resp.raise_for_status()
    raw = resp.json()
    nl_data = raw.get("data", []) if isinstance(raw, dict) else raw
    if not nl_data:
        raise RuntimeError("No NL gas storage data returned from AGSI+")

    # Sort ascending by date (gasDayStart field)
    nl_data = sorted(nl_data, key=lambda x: x.get("gasDayStart", ""))
    latest  = nl_data[-1]

    current_full   = safe_float(latest.get("full"))         or 0.0   # %
    current_volume = safe_float(latest.get("gasInStorage")) or 0.0   # TWh (current stored volume)
    injection  = safe_float(latest.get("injection"))        or 0.0   # GWh/day
    withdrawal = safe_float(latest.get("withdrawal"))       or 0.0   # GWh/day
    daily_change = injection - withdrawal                              # + = net inject

    # days-until-empty: last 7 days average net withdrawal
    recent = nl_data[-7:]
    net_wds = [max(0.0, (safe_float(d.get("withdrawal")) or 0) - (safe_float(d.get("injection")) or 0))
               for d in recent]
    net_wds = [v for v in net_wds if v > 0]
    days_until_empty = None
    if net_wds:
        avg_nw = sum(net_wds) / len(net_wds)   # GWh/day
        if avg_nw > 0:
            days_until_empty = int(round(current_volume * 1000 / avg_nw))

    # EU average filling level (optional)
    eu_avg_full = None
    try:
        r2 = requests.get("https://agsi.gie.eu/api",
                          params={"country": "EU", "size": 1},
                          headers=headers, timeout=12)
        if r2.ok:
            eu_raw  = r2.json()
            eu_data = eu_raw.get("data", []) if isinstance(eu_raw, dict) else eu_raw
            if eu_data:
                eu_avg_full = round(safe_float(eu_data[0].get("full")) or 0, 2)
    except Exception:
        pass

    timeline = []
    for d in nl_data:
        try:
            timeline.append({"date": d["gasDayStart"],
                              "full": round(safe_float(d.get("full")) or 0, 2)})
        except (KeyError, TypeError):
            pass

    return {
        "current_full":     round(current_full,   2),
        "current_volume":   round(current_volume, 2),
        "daily_change_gwh": round(daily_change,   1),
        "days_until_empty": days_until_empty,
        "eu_avg_full":      eu_avg_full,
        "timeline":         timeline,
        "as_of":            latest.get("gasDayStart", ""),
    }


# ── Renewables NL (ENTSO-E A75) ───────────────────────────────────────────────

def parse_entsoe_generation(xml_text):
    """Parse ENTSO-E A75 XML → {psrType: [(utc_dt, mw), ...]}
    Uses {*} namespace wildcard so it works with the GL_MarketDocument
    namespace (generationloaddocument:3:0), which differs from A44's namespace."""
    root   = ET.fromstring(xml_text)
    result = {}
    for ts in root.findall(".//{*}TimeSeries"):
        psr_el = ts.find(".//{*}psrType")
        if psr_el is None:
            continue
        psr = psr_el.text
        for period in ts.findall("{*}Period"):
            start_el = period.find(".//{*}start")
            res_el   = period.find("{*}resolution")
            if start_el is None or res_el is None:
                continue
            start_dt = datetime.fromisoformat(start_el.text.replace("Z", "+00:00"))
            delta    = timedelta(hours=1) if res_el.text == "PT60M" else timedelta(minutes=15)
            for pt in period.findall("{*}Point"):
                pos_el = pt.find("{*}position")
                qty_el = pt.find("{*}quantity")
                if pos_el is None or qty_el is None:
                    continue
                result.setdefault(psr, []).append(
                    (start_dt + (int(pos_el.text) - 1) * delta, float(qty_el.text)))
    return result


def _fetch_gen_type(args):
    code, start, end = args
    params = {
        "securityToken": entsoe_key,
        "documentType":  "A75",
        "processType":   "A16",
        "in_Domain":     NL_ZONE,
        "periodStart":   start,
        "periodEnd":     end,
        "psrType":       code,
    }
    try:
        resp = requests.get("https://web-api.tp.entsoe.eu/api", params=params, timeout=30)
        if not resp.ok:
            return code, []
        return code, parse_entsoe_generation(resp.text).get(code, [])
    except Exception:
        return code, []


def build_renewables_data():
    yesterday = date.today() - timedelta(days=1)
    after_end = yesterday.strftime("%Y%m%d") + "2300"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_fetch_gen_type,
                              [(code, ENTSOE_HISTORY_START, after_end)
                               for code, _, _ in PSR_TYPES]))
    raw = dict(results)

    # Daily MW averages per source
    daily_by_code = {}
    for code, pts in raw.items():
        if not pts:
            continue
        by_date = {}
        for dt, mw in pts:
            d = dt.astimezone(CET).date()
            by_date.setdefault(d, []).append(mw)
        daily_by_code[code] = {str(d): round(sum(vs)/len(vs), 1)
                                for d, vs in sorted(by_date.items())}

    all_dates = sorted({d for dd in daily_by_code.values() for d in dd.keys()})

    # Build datasets in PSR_TYPES order (bottom→top of stacked area)
    datasets = []
    for code, name, color in PSR_TYPES:
        if code not in daily_by_code:
            continue
        data = [daily_by_code[code].get(d) for d in all_dates]
        if all(v is None or v == 0 for v in data):
            continue
        datasets.append({"code": code, "label": name, "color": color, "data": data})

    # KPIs
    latest_date = all_dates[-1] if all_dates else None
    ren_share   = None
    if latest_date:
        ren = sum((daily_by_code.get(c, {}).get(latest_date) or 0) for c in RENEWABLE_CODES)
        tot = sum((daily_by_code.get(c, {}).get(latest_date) or 0) for c, _, _ in PSR_TYPES)
        ren_share = round(ren / tot * 100, 1) if tot > 0 else None

    peak_solar = None
    if "B16" in daily_by_code and daily_by_code["B16"]:
        bd = max(daily_by_code["B16"].items(), key=lambda x: x[1])
        peak_solar = {"date": bd[0], "mw": bd[1]}

    peak_wind = None
    wind_combined = {}
    for code in ("B18", "B19"):
        for d, v in daily_by_code.get(code, {}).items():
            wind_combined[d] = wind_combined.get(d, 0) + v
    if wind_combined:
        bd = max(wind_combined.items(), key=lambda x: x[1])
        peak_wind = {"date": bd[0], "mw": round(bd[1], 1)}

    # Interpretation text
    event_date   = "2026-02-28"
    before_dates = [d for d in all_dates if d <= event_date]
    after_dates  = [d for d in all_dates if d >  event_date]

    def avg_share(dates, codes):
        shares = []
        for d in dates:
            num = sum((daily_by_code.get(c, {}).get(d) or 0) for c in codes)
            tot = sum((daily_by_code.get(c, {}).get(d) or 0) for c, _, _ in PSR_TYPES)
            if tot > 0:
                shares.append(num / tot * 100)
        return round(sum(shares)/len(shares), 1) if shares else None

    br, ar = avg_share(before_dates, RENEWABLE_CODES), avg_share(after_dates, RENEWABLE_CODES)
    bf, af = avg_share(before_dates, FOSSIL_CODES),    avg_share(after_dates, FOSSIL_CODES)

    interp = ""
    if br is not None and ar is not None:
        d1 = "rose" if ar > br else "fell"
        interp += f"Average renewables share {d1} from {br}% (1\u201327\u00a0Feb) to {ar}% after 28\u00a0Feb. "
    if bf is not None and af is not None:
        d2 = "increased" if af > bf else "decreased"
        interp += f"Fossil fuel share {d2} from {bf}% to {af}% post-event."

    return {
        "dates":          all_dates,
        "datasets":       datasets,
        "ren_share":      ren_share,
        "peak_solar":     peak_solar,
        "peak_wind":      peak_wind,
        "interpretation": interp,
        "as_of":          str(yesterday),
    }


# ── Price Heatmap ─────────────────────────────────────────────────────────────

def build_heatmap_data():
    yesterday = date.today() - timedelta(days=1)
    after_end = yesterday.strftime("%Y%m%d") + "2300"
    params = {
        "securityToken": entsoe_key,
        "documentType":  "A44",
        "in_Domain":     NL_ZONE,
        "out_Domain":    NL_ZONE,
        "periodStart":   ENTSOE_HISTORY_START,
        "periodEnd":     after_end,
    }
    resp = requests.get("https://web-api.tp.entsoe.eu/api", params=params, timeout=30)
    resp.raise_for_status()
    prices = parse_entsoe_prices(resp.text)
    if not prices:
        raise RuntimeError("No price data returned for heatmap")

    grid_data = {}
    for dt, price in prices:
        cet  = dt.astimezone(CET)
        dstr = cet.strftime("%Y-%m-%d")
        grid_data.setdefault(dstr, {})[cet.hour] = price

    all_dates = sorted(grid_data.keys())
    grid = [[grid_data[d].get(h) for h in range(24)] for d in all_dates]

    flat      = [p for row in grid for p in row if p is not None]
    price_min = min(flat) if flat else 0
    price_max = max(flat) if flat else 100

    hour_avgs = []
    for h in range(24):
        vals = [grid[i][h] for i in range(len(all_dates)) if grid[i][h] is not None]
        hour_avgs.append(round(sum(vals)/len(vals), 2) if vals else None)

    valid_avgs     = [v for v in hour_avgs if v is not None]
    cheapest_hour  = hour_avgs.index(min(valid_avgs)) if valid_avgs else 0
    expensive_hour = hour_avgs.index(max(valid_avgs)) if valid_avgs else 23

    day_avgs       = [(d, round(sum(p for p in grid[i] if p is not None) /
                                max(1, sum(1 for p in grid[i] if p is not None)), 2))
                      for i, d in enumerate(all_dates)]
    biggest_day    = max(day_avgs, key=lambda x: x[1]) if day_avgs else None

    event_date    = "2026-02-28"
    before_flat   = [p for i, d in enumerate(all_dates) if d <= event_date
                     for p in grid[i] if p is not None]
    after_flat    = [p for i, d in enumerate(all_dates) if d >  event_date
                     for p in grid[i] if p is not None]
    pre_avg  = round(sum(before_flat)/len(before_flat), 2) if before_flat else None
    post_avg = round(sum(after_flat) /len(after_flat),  2) if after_flat  else None
    pct_change = (round((post_avg - pre_avg) / abs(pre_avg) * 100, 1)
                  if pre_avg and post_avg and pre_avg != 0 else None)

    return {
        "dates":          all_dates,
        "grid":           grid,
        "price_min":      round(price_min, 2),
        "price_max":      round(price_max, 2),
        "hour_avgs":      hour_avgs,
        "cheapest_hour":  cheapest_hour,
        "expensive_hour": expensive_hour,
        "biggest_day":    biggest_day,
        "pre_avg":        pre_avg,
        "post_avg":       post_avg,
        "pct_change":     pct_change,
        "as_of":          str(yesterday),
    }


# ── crisis HTML/JS templates (plain strings — no f-string escaping needed) ───

def _crisis_html():
    return (
        '<div id="crisis-view" class="tab-panel">'
        '<div class="crisis-context">'
        '<p>Comparing European day-ahead wholesale electricity prices before and after '
        'Operation Epic Fury (28&nbsp;Feb&nbsp;2026) and the '
        'Strait of Hormuz closure (2&nbsp;Mar&nbsp;2026). '
        'Source: ENTSO&#8209;E Transparency Platform.</p>'
        '</div>'
        '<div id="crisis-loading" style="text-align:center;padding:80px 24px;color:#888">'
        '<div class="spinner"></div>'
        '<p style="margin-top:12px;font-size:13px">'
        'Fetching ENTSO&#8209;E data for 6 bidding zones&hellip;</p>'
        '</div>'
        '<div id="crisis-charts" class="container" style="display:none">'
        '<div class="section-title">Gas-dependent: NL &middot; DE &middot; BE</div>'
        '<div class="chart-wrap" style="height:220px">'
        '<canvas id="chartGas"></canvas>'
        '</div>'
        '<p style="font-size:11px;color:#888;margin:-16px 0 20px 4px">'
        'Bars show hourly median price. &ldquo;Before&rdquo; covers 28&nbsp;days (1&ndash;27&nbsp;Feb); '
        '&ldquo;After&rdquo; covers fewer days and grows daily.</p>'
        '<div class="section-title" style="margin-top:8px">'
        'Low-carbon: Hydro &amp; Nuclear &mdash; FR &middot; ES &middot; NO South'
        '</div>'
        '<div class="chart-wrap" style="height:220px">'
        '<canvas id="chartLow"></canvas>'
        '</div>'
        '<p style="font-size:11px;color:#888;margin:-16px 0 20px 4px">'
        'Bars show hourly median price. &ldquo;Before&rdquo; covers 28&nbsp;days (1&ndash;27&nbsp;Feb); '
        '&ldquo;After&rdquo; covers fewer days and grows daily. '
        'Spain&rsquo;s &ldquo;Before&rdquo; median was &euro;4.69&thinsp;/MWh &mdash; February 2026 saw extreme renewable '
        'oversupply with frequent negative prices, making the percentage change vs&nbsp;March misleading. '
        'The &gt;200% badge flags this; the absolute bar lengths are the honest comparison.</p>'
        '<div class="section-title" style="margin-top:8px">'
        'Netherlands &mdash; daily average day-ahead price (1 Feb &rarr; yesterday)'
        '</div>'
        '<div class="chart-wrap" style="height:280px">'
        '<canvas id="chartNLTraj"></canvas>'
        '</div>'
        '</div>'  # /crisis-charts
        '</div>'  # /crisis-view
    )


def _crisis_js():
    # Plain string: no {{ }} escaping needed
    # Note: pctBadgePlugin, eventLinesPlugin, rollingAvg defined in _shared_js()
    return """<script>
let crisisRequested = false;

function loadCrisisData() {
  if (crisisRequested) return;
  crisisRequested = true;
  fetch('/api/crisis')
    .then(r => r.json())
    .then(data => {
      document.getElementById('crisis-loading').style.display = 'none';
      document.getElementById('crisis-charts').style.display  = 'block';
      renderCrisisCharts(data);
    })
    .catch(err => {
      document.getElementById('crisis-loading').innerHTML =
        '<p style="color:#dc3545;margin-top:40px;font-size:13px">Failed to load: ' + err + '</p>';
    });
}

function makeGroupChart(canvasId, countries, afterColors) {
  const ch = Chart.getChart(canvasId);
  if (ch) ch.destroy();
  return new Chart(document.getElementById(canvasId), {
    type: 'bar',
    plugins: [pctBadgePlugin],
    data: {
      labels: countries.map(c => c.name + (c.error ? ' \u26a0' : '')),
      datasets: [
        {
          label: 'Before  1\u201327 Feb  (median)',
          data: countries.map(c => c.before_med),
          backgroundColor: 'rgba(120,120,120,0.28)',
          borderColor:     'rgba(120,120,120,0.55)',
          borderWidth: 1,
          barThickness: 26,
        },
        {
          label: 'After  2 Mar\u2013yesterday  (median)',
          data: countries.map(c => c.after_med),
          backgroundColor: afterColors.map(c => c + '44'),
          borderColor:     afterColors,
          borderWidth: 1.5,
          barThickness: 26,
        }
      ]
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { right: 80 } },
      plugins: {
        legend: {
          position: 'top',
          labels: {
            font: { size: 11 },
            generateLabels(chart) {
              const items = Chart.defaults.plugins.legend.labels.generateLabels(chart);
              items.forEach((item, i) => {
                if (i === 1) {
                  item.fillStyle   = afterColors[0] + 'aa';
                  item.strokeStyle = afterColors[0];
                  item.lineWidth   = 1.5;
                }
              });
              return items;
            }
          }
        },
        pctBadge: { enabled: true },
        tooltip:  {
          callbacks: {
            label: c => ' ' + c.dataset.label + ': ' +
              (c.parsed.x != null ? c.parsed.x.toFixed(2) + ' \u20ac/MWh' : '\u2014')
          }
        }
      },
      scales: {
        x: {
          beginAtZero: true,
          title: { display: true, text: '\u20ac/MWh', font: { size: 11 } },
          ticks: { font: { size: 11 } },
          grid:  { color: 'rgba(0,0,0,0.05)' }
        },
        y: { ticks: { font: { size: 12 } }, grid: { display: false } }
      }
    }
  });
}

function rollingAvg(arr, win) {
  return arr.map((_, i) => {
    const slice = arr.slice(Math.max(0, i - win + 1), i + 1).filter(v => v != null);
    return slice.length ? slice.reduce((a, b) => a + b, 0) / slice.length : null;
  });
}

function renderNLTraj(trajectory) {
  const ch = Chart.getChart('chartNLTraj');
  if (ch) ch.destroy();
  const labels      = trajectory.map(d => d.date);
  const prices      = trajectory.map(d => d.price);
  const rollingData = rollingAvg(prices, 7);
  const epfIdx  = labels.indexOf('2026-02-28');
  const hormIdx = labels.indexOf('2026-03-02');
  new Chart(document.getElementById('chartNLTraj'), {
    type: 'line',
    plugins: [eventLinesPlugin],
    data: {
      labels,
      datasets: [
        {
          label: 'NL day-ahead avg (\u20ac/MWh)',
          data: prices,
          borderColor: '#2196F3',
          backgroundColor: 'rgba(33,150,243,0.08)',
          borderWidth: 1.5,
          pointRadius: 2,
          fill: true,
          tension: 0.3,
          spanGaps: true,
        },
        {
          label: '7-day rolling avg',
          data: rollingData,
          borderColor: '#FF9800',
          backgroundColor: 'transparent',
          borderWidth: 2,
          pointRadius: 0,
          fill: false,
          tension: 0.3,
          spanGaps: true,
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 28 } },
      plugins: {
        legend: { position: 'top', labels: { font: { size: 11 } } },
        eventLines: {
          lines: [
            { idx: epfIdx,  color: '#dc3545', label: 'Op. Epic Fury' },
            { idx: hormIdx, color: '#fd7e14', label: 'Hormuz closed', below: true },
          ]
        },
        tooltip: {
          callbacks: {
            title: items => {
              const d = new Date(items[0].label + 'T12:00:00');
              return d.toLocaleDateString('en-GB', {day:'numeric',month:'short',year:'numeric'});
            },
            label: c => ' ' + c.parsed.y.toFixed(2) + ' \u20ac/MWh'
          }
        }
      },
      scales: {
        x: {
          ticks: {
            maxTicksLimit: 22,
            font: { size: 10 },
            maxRotation: 45,
            callback(val) {
              const d = new Date(labels[val] + 'T12:00:00');
              return d.toLocaleDateString('en-GB', {day:'numeric', month:'short'});
            }
          },
          grid: { color: 'rgba(0,0,0,0.05)' }
        },
        y: {
          title: { display: true, text: '\u20ac/MWh', font: { size: 11 } },
          ticks: { font: { size: 11 } },
          grid:  { color: 'rgba(0,0,0,0.05)' }
        }
      }
    }
  });
}

function renderCrisisCharts(data) {
  const gas = data.countries.filter(c => c.group === 'gas');
  const low = data.countries.filter(c => c.group === 'low');
  // Gas-dependent: NL=blue, DE=amber, BE=purple
  makeGroupChart('chartGas', gas, ['#2196F3', '#FF9800', '#9C27B0']);
  // Low-carbon: FR=indigo, ES=red, NO=green
  makeGroupChart('chartLow', low, ['#3F51B5', '#F44336', '#4CAF50']);
  renderNLTraj(data.nl_trajectory);
}
</script>"""


# ── Shared JS plugins (used by multiple tabs) ─────────────────────────────────

def _shared_js():
    return """<script>
// ── Shared Chart.js plugins & utilities ──────────────────────────────────────

const pctBadgePlugin = {
  id: 'pctBadge',
  afterDatasetsDraw(chart, _args, opts) {
    if (!opts || !opts.enabled) return;
    const {ctx, data} = chart;
    const before = data.datasets[0].data;
    const after  = data.datasets[1].data;
    chart.getDatasetMeta(1).data.forEach((bar, i) => {
      if (before[i] == null || after[i] == null) return;
      const pct  = ((after[i] - before[i]) / Math.abs(before[i])) * 100;
      const sign = pct >= 0 ? '+' : '';
      const text = Math.abs(pct) > 200
        ? (pct >= 0 ? '>' : '<-') + '200%'
        : sign + pct.toFixed(1) + '%';
      const fg = pct >= 0 ? '#dc3545' : '#198754';
      const bg = pct >= 0 ? 'rgba(220,53,69,0.12)' : 'rgba(25,135,84,0.12)';
      const x = bar.x + 4, y = bar.y;
      ctx.save();
      ctx.font = 'bold 11px system-ui';
      const tw = ctx.measureText(text).width;
      ctx.fillStyle = bg;
      ctx.fillRect(x, y - 9, tw + 10, 18);
      ctx.fillStyle = fg;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, x + 5, y);
      ctx.restore();
    });
  }
};

const eventLinesPlugin = {
  id: 'eventLines',
  afterDatasetsDraw(chart, _args, opts) {
    if (!opts || !opts.lines) return;
    const {ctx, scales, chartArea} = chart;
    opts.lines.forEach(({idx, color, label, below}) => {
      if (idx === -1) return;
      const x = scales.x.getPixelForValue(idx);
      if (x < chartArea.left || x > chartArea.right) return;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth   = 1.5;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(x, chartArea.top);
      ctx.lineTo(x, chartArea.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.font = 'bold 10px system-ui';
      ctx.textAlign = 'center';
      const labelY = below ? chartArea.top + 16 : chartArea.top - 7;
      ctx.fillText(label, x, labelY);
      ctx.restore();
    });
  }
};

function rollingAvg(arr, win) {
  return arr.map((_, i) => {
    const slice = arr.slice(Math.max(0, i - win + 1), i + 1).filter(v => v != null);
    return slice.length ? slice.reduce((a, b) => a + b, 0) / slice.length : null;
  });
}
</script>"""


# ── Gas Storage HTML/JS ───────────────────────────────────────────────────────

def _gas_storage_html():
    return (
        '<div id="gas-storage" class="tab-panel">'
        '<div class="crisis-context">'
        '<p>Dutch underground natural gas storage filling levels over the past 12 months. '
        'The dashed line marks the current EU&nbsp;average for context. '
        'Vertical lines mark Operation Epic Fury (28&nbsp;Feb&nbsp;2026) '
        'and the Strait of Hormuz closure (2&nbsp;Mar&nbsp;2026). '
        'Source: GIE AGSI+ Transparency Platform.</p>'
        '</div>'
        '<div id="gs-loading" style="text-align:center;padding:80px 24px;color:#888">'
        '<div class="spinner"></div>'
        '<p style="margin-top:12px;font-size:13px">Fetching AGSI+ storage data&hellip;</p>'
        '</div>'
        '<div id="gs-charts" class="container" style="display:none">'
        '<div id="gs-cards" class="cards"></div>'
        '<div class="section-title">NL gas storage filling level — last 365 days</div>'
        '<div class="chart-wrap" style="height:300px">'
        '<canvas id="chartGasStorage"></canvas>'
        '</div>'
        '<p style="font-size:11px;color:#888;margin:-16px 0 20px 4px">'
        'Source: GIE AGSI+ Transparency Platform. Updated daily at 19:30&nbsp;CET.</p>'
        '</div>'
        '</div>'
    )


def _gas_storage_js():
    return """<script>
let gsRequested = false;

function loadGasStorageData() {
  if (gsRequested) return;
  gsRequested = true;
  fetch('/api/gas-storage')
    .then(r => r.json())
    .then(data => {
      if (data.error) throw new Error(data.error);
      document.getElementById('gs-loading').style.display = 'none';
      document.getElementById('gs-charts').style.display  = 'block';
      renderGasStorageKPIs(data);
      renderGasStorageChart(data);
    })
    .catch(err => {
      document.getElementById('gs-loading').innerHTML =
        '<p style="color:#dc3545;margin-top:40px;font-size:13px">Failed to load: ' + err + '</p>';
    });
}

function renderGasStorageKPIs(data) {
  const sign  = data.daily_change_gwh >= 0 ? '+' : '';
  const chgLbl = data.daily_change_gwh >= 0 ? 'net injection' : 'net withdrawal';
  const cards = [
    { label: 'Filling Level',     value: data.current_full.toFixed(1) + '%',
      sub: 'as of ' + data.as_of, cls: '' },
    { label: 'Volume in Storage', value: data.current_volume.toFixed(1) + ' TWh',
      sub: 'working gas', cls: '' },
    { label: 'Daily Change',
      value: sign + data.daily_change_gwh.toFixed(0) + ' GWh/d',
      sub: chgLbl,
      cls: data.daily_change_gwh >= 0 ? '' : 'peak' },
    { label: 'Days Until Empty',
      value: data.days_until_empty ? data.days_until_empty + ' days' : '\u2014',
      sub: '7-day avg withdrawal rate', cls: '' },
  ];
  document.getElementById('gs-cards').innerHTML = cards.map(c =>
    '<div class="card ' + c.cls + '">' +
    '<div class="label">' + c.label + '</div>' +
    '<div class="value">' + c.value + '</div>' +
    '<div class="sub">' + c.sub + '</div></div>'
  ).join('');
}

function renderGasStorageChart(data) {
  const labels  = data.timeline.map(d => d.date);
  const fillPct = data.timeline.map(d => d.full);
  const epfIdx  = labels.indexOf('2026-02-28');
  const hormIdx = labels.indexOf('2026-03-02');

  const datasets = [{
    label: 'NL filling level (%)',
    data: fillPct,
    borderColor: '#2196F3',
    backgroundColor: 'rgba(33,150,243,0.1)',
    borderWidth: 2,
    pointRadius: 0,
    fill: true,
    tension: 0.3,
    spanGaps: true,
  }];

  if (data.eu_avg_full !== null && data.eu_avg_full !== undefined) {
    datasets.push({
      label: 'EU average (' + data.eu_avg_full.toFixed(1) + '%)',
      data: Array(labels.length).fill(data.eu_avg_full),
      borderColor: '#888',
      borderDash: [6, 4],
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      spanGaps: true,
    });
  }

  const ch = Chart.getChart('chartGasStorage');
  if (ch) ch.destroy();
  new Chart(document.getElementById('chartGasStorage'), {
    type: 'line',
    plugins: [eventLinesPlugin],
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 28 } },
      plugins: {
        legend: { position: 'top', labels: { font: { size: 11 } } },
        eventLines: {
          lines: [
            { idx: epfIdx,  color: '#dc3545', label: 'Op. Epic Fury' },
            { idx: hormIdx, color: '#fd7e14', label: 'Hormuz closed', below: true },
          ]
        },
        tooltip: {
          callbacks: {
            title: items => {
              const d = new Date(items[0].label + 'T12:00:00');
              return d.toLocaleDateString('en-GB', {day:'numeric',month:'short',year:'numeric'});
            },
            label: c => ' ' + c.dataset.label + ': ' + c.parsed.y.toFixed(1) + '%'
          }
        }
      },
      scales: {
        x: {
          ticks: {
            maxTicksLimit: 20, font: { size: 10 }, maxRotation: 45,
            callback(val) {
              const d = new Date(labels[val] + 'T12:00:00');
              return d.toLocaleDateString('en-GB', {day:'numeric', month:'short'});
            }
          },
          grid: { color: 'rgba(0,0,0,0.05)' }
        },
        y: {
          min: 0, max: 100,
          title: { display: true, text: '% full', font: { size: 11 } },
          ticks: { font: { size: 11 }, callback: v => v + '%' },
          grid:  { color: 'rgba(0,0,0,0.05)' }
        }
      }
    }
  });
}
</script>"""


# ── Renewables NL HTML/JS ─────────────────────────────────────────────────────

def _renewables_html():
    return (
        '<div id="renewables-nl" class="tab-panel">'
        '<div class="crisis-context">'
        '<p>Netherlands actual electricity generation by source (ENTSO&#8209;E A75), '
        '1&nbsp;Feb&nbsp;2026 to yesterday. Stacked area = total generation mix. '
        'Vertical lines mark Operation Epic Fury and Hormuz closure.</p>'
        '</div>'
        '<div id="ren-loading" style="text-align:center;padding:80px 24px;color:#888">'
        '<div class="spinner"></div>'
        '<p style="margin-top:12px;font-size:13px">'
        'Fetching ENTSO&#8209;E generation data for 12 source types&hellip;</p>'
        '</div>'
        '<div id="ren-charts" class="container" style="display:none">'
        '<div id="ren-cards" class="cards"></div>'
        '<div class="section-title">NL generation mix — daily average (MW)</div>'
        '<div class="chart-wrap" style="height:380px">'
        '<canvas id="chartRenewables"></canvas>'
        '</div>'
        '<div id="ren-interpretation" style="background:#1a1a3a;border-left:3px solid #4f8ef7;'
        'color:#aaa;font-size:12px;line-height:1.7;padding:12px 20px;margin-bottom:24px;'
        'border-radius:0 6px 6px 0"></div>'
        '</div>'
        '</div>'
    )


def _renewables_js():
    return """<script>
let renRequested = false;

function loadRenewablesData() {
  if (renRequested) return;
  renRequested = true;
  fetch('/api/renewables')
    .then(r => r.json())
    .then(data => {
      if (data.error) throw new Error(data.error);
      document.getElementById('ren-loading').style.display = 'none';
      document.getElementById('ren-charts').style.display  = 'block';
      renderRenewablesKPIs(data);
      renderRenewablesChart(data);
      if (data.interpretation) {
        document.getElementById('ren-interpretation').textContent = data.interpretation;
      }
    })
    .catch(err => {
      document.getElementById('ren-loading').innerHTML =
        '<p style="color:#dc3545;margin-top:40px;font-size:13px">Failed to load: ' + err + '</p>';
    });
}

function renderRenewablesKPIs(data) {
  function fmtDate(ds) {
    const d = new Date(ds + 'T12:00:00');
    return d.toLocaleDateString('en-GB', {day:'numeric', month:'short', year:'numeric'});
  }
  const cards = [
    { label: 'Renewables Share (latest day)',
      value: data.ren_share !== null ? data.ren_share + '%' : '\u2014',
      sub: 'wind + solar + hydro + biomass', cls: '' },
    { label: 'Peak Solar Day',
      value: data.peak_solar ? data.peak_solar.mw.toFixed(0) + ' MW avg' : '\u2014',
      sub: data.peak_solar ? fmtDate(data.peak_solar.date) : '', cls: '' },
    { label: 'Peak Wind Day (on+offshore)',
      value: data.peak_wind ? data.peak_wind.mw.toFixed(0) + ' MW avg' : '\u2014',
      sub: data.peak_wind ? fmtDate(data.peak_wind.date) : '', cls: '' },
  ];
  document.getElementById('ren-cards').innerHTML = cards.map(c =>
    '<div class="card ' + c.cls + '">' +
    '<div class="label">' + c.label + '</div>' +
    '<div class="value">' + c.value + '</div>' +
    '<div class="sub">' + c.sub + '</div></div>'
  ).join('');
}

function renderRenewablesChart(data) {
  const labels  = data.dates;
  const epfIdx  = labels.indexOf('2026-02-28');
  const hormIdx = labels.indexOf('2026-03-02');

  const datasets = data.datasets.map(d => ({
    label: d.label,
    data: d.data.map(v => v === null ? 0 : v),
    backgroundColor: d.color + 'bb',
    borderColor: d.color,
    borderWidth: 0.5,
    fill: true,
    pointRadius: 0,
    tension: 0.2,
    spanGaps: true,
  }));

  const ch = Chart.getChart('chartRenewables');
  if (ch) ch.destroy();
  new Chart(document.getElementById('chartRenewables'), {
    type: 'line',
    plugins: [eventLinesPlugin],
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 28 } },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          position: 'top',
          labels: { font: { size: 11 }, boxWidth: 12, padding: 8 }
        },
        eventLines: {
          lines: [
            { idx: epfIdx,  color: '#dc3545', label: 'Op. Epic Fury' },
            { idx: hormIdx, color: '#fd7e14', label: 'Hormuz closed', below: true },
          ]
        },
        tooltip: {
          callbacks: {
            title: items => {
              const d = new Date(items[0].label + 'T12:00:00');
              return d.toLocaleDateString('en-GB', {day:'numeric',month:'short',year:'numeric'});
            },
            label: c => ' ' + c.dataset.label + ': ' + c.parsed.y.toFixed(0) + ' MW'
          }
        }
      },
      scales: {
        x: {
          ticks: {
            maxTicksLimit: 20, font: { size: 10 }, maxRotation: 45,
            callback(val) {
              const d = new Date(labels[val] + 'T12:00:00');
              return d.toLocaleDateString('en-GB', {day:'numeric', month:'short'});
            }
          },
          grid: { color: 'rgba(0,0,0,0.05)' }
        },
        y: {
          stacked: true,
          title: { display: true, text: 'MW (daily avg)', font: { size: 11 } },
          ticks: { font: { size: 11 } },
          grid:  { color: 'rgba(0,0,0,0.05)' }
        }
      }
    }
  });
}
</script>"""


# ── Price Heatmap HTML/JS ─────────────────────────────────────────────────────

def _heatmap_html():
    return (
        '<div id="price-heatmap" class="tab-panel">'
        '<div class="crisis-context">'
        '<p>NL day-ahead hourly prices (ENTSO&#8209;E A44) reshaped as a heatmap: '
        'each row is one day, each column is one hour. '
        'Green = cheap, yellow = moderate, red = expensive. '
        'Red dashed line marks the 28&nbsp;Feb event boundary. '
        'Hover a cell for the exact price.</p>'
        '</div>'
        '<div id="hm-loading" style="text-align:center;padding:80px 24px;color:#888">'
        '<div class="spinner"></div>'
        '<p style="margin-top:12px;font-size:13px">Fetching price heatmap data&hellip;</p>'
        '</div>'
        '<div id="hm-charts" class="container" style="display:none">'
        '<div id="hm-cards" class="cards"></div>'
        '<div class="section-title" style="margin-bottom:12px">Hourly price heatmap — NL day-ahead (1 Feb &rarr; yesterday)</div>'
        '<div style="overflow-x:auto;margin-bottom:8px">'
        '<canvas id="heatmapCanvas" style="display:block"></canvas>'
        '</div>'
        '<p style="font-size:11px;color:#888;margin-bottom:24px">'
        'Source: ENTSO&#8209;E Transparency Platform. Day-ahead prices, NL bidding zone (10YNL----------L).</p>'
        '</div>'
        '<div id="heatmapTooltip" style="position:fixed;display:none;background:rgba(0,0,0,0.82);'
        'color:#fff;padding:6px 10px;border-radius:4px;font-size:12px;'
        'pointer-events:none;z-index:9999;white-space:nowrap"></div>'
        '</div>'
    )


def _heatmap_js():
    return """<script>
let hmRequested = false;

function loadHeatmapData() {
  if (hmRequested) return;
  hmRequested = true;
  fetch('/api/heatmap')
    .then(r => r.json())
    .then(data => {
      if (data.error) throw new Error(data.error);
      document.getElementById('hm-loading').style.display = 'none';
      document.getElementById('hm-charts').style.display  = 'block';
      renderHeatmapKPIs(data);
      renderHeatmapCanvas(data);
    })
    .catch(err => {
      document.getElementById('hm-loading').innerHTML =
        '<p style="color:#dc3545;margin-top:40px;font-size:13px">Failed to load: ' + err + '</p>';
    });
}

function renderHeatmapKPIs(data) {
  function fmtHour(h) { return String(h).padStart(2,'0') + ':00'; }
  function fmtDate(ds) {
    const d = new Date(ds + 'T12:00:00');
    return d.toLocaleDateString('en-GB', {day:'numeric', month:'short', year:'numeric'});
  }
  const pctSign = data.pct_change !== null ? (data.pct_change >= 0 ? '+' : '') : '';
  const cards = [
    { label: 'Cheapest Hour (avg)',
      value: fmtHour(data.cheapest_hour),
      sub: (data.hour_avgs[data.cheapest_hour] || 0).toFixed(2) + ' \u20ac/MWh avg', cls: 'trough' },
    { label: 'Most Expensive Hour (avg)',
      value: fmtHour(data.expensive_hour),
      sub: (data.hour_avgs[data.expensive_hour] || 0).toFixed(2) + ' \u20ac/MWh avg', cls: 'peak' },
    { label: 'Highest Day Average',
      value: data.biggest_day ? data.biggest_day[1].toFixed(2) + ' \u20ac' : '\u2014',
      sub: data.biggest_day ? fmtDate(data.biggest_day[0]) : '', cls: 'peak' },
    { label: 'Price Change Post-Event',
      value: data.pct_change !== null ? pctSign + data.pct_change + '%' : '\u2014',
      sub: (data.pre_avg ? '\u20ac' + data.pre_avg.toFixed(1) : '?') + ' \u2192 ' +
           (data.post_avg ? '\u20ac' + data.post_avg.toFixed(1) : '?') + ' /MWh avg',
      cls: (data.pct_change !== null && data.pct_change > 0) ? 'peak' : '' },
  ];
  document.getElementById('hm-cards').innerHTML = cards.map(c =>
    '<div class="card ' + c.cls + '">' +
    '<div class="label">' + c.label + '</div>' +
    '<div class="value">' + c.value + '</div>' +
    '<div class="sub">' + c.sub + '</div></div>'
  ).join('');
}

function renderHeatmapCanvas(data) {
  const nDays   = data.dates.length;
  const nHours  = 24;
  const cellW   = 28;
  const cellH   = 15;
  const topPad  = 28;
  const leftPad = 72;
  const botPad  = 34;
  const logW    = leftPad + nHours * cellW;
  const logH    = topPad + nDays * cellH + botPad;

  const canvas = document.getElementById('heatmapCanvas');
  const dpr    = window.devicePixelRatio || 1;
  canvas.width        = logW * dpr;
  canvas.height       = logH * dpr;
  canvas.style.width  = logW + 'px';
  canvas.style.height = logH + 'px';

  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  function priceColor(price) {
    if (price === null || price === undefined) return '#2e2e3e';
    const t  = Math.max(0, Math.min(1, (price - data.price_min) / (data.price_max - data.price_min)));
    let r, g, b;
    if (t < 0.5) {
      const s = t * 2;
      r = Math.round(34  + (234 - 34)  * s);
      g = Math.round(197 + (179 - 197) * s);
      b = Math.round(94  + (8   - 94)  * s);
    } else {
      const s = (t - 0.5) * 2;
      r = Math.round(234 + (239 - 234) * s);
      g = Math.round(179 + (68  - 179) * s);
      b = Math.round(8   + (68  - 8)   * s);
    }
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  // Hour headers
  ctx.fillStyle = '#999';
  ctx.font = '9px system-ui';
  ctx.textAlign = 'center';
  for (let h = 0; h < 24; h++) {
    ctx.fillText(String(h).padStart(2,'0'), leftPad + h * cellW + cellW / 2, topPad - 7);
  }

  // Rows
  for (let i = 0; i < nDays; i++) {
    const y = topPad + i * cellH;
    // Date label
    ctx.fillStyle = '#999';
    ctx.font = '9px system-ui';
    ctx.textAlign = 'right';
    const d = new Date(data.dates[i] + 'T12:00:00');
    ctx.fillText(d.toLocaleDateString('en-GB',{day:'numeric',month:'short'}), leftPad - 4, y + cellH/2 + 3);
    // Cells
    for (let h = 0; h < 24; h++) {
      ctx.fillStyle = priceColor(data.grid[i][h]);
      ctx.fillRect(leftPad + h * cellW + 1, y + 1, cellW - 2, cellH - 2);
    }
    // Red dashed boundary after 28 Feb
    if (data.dates[i] === '2026-02-28') {
      ctx.save();
      ctx.strokeStyle = '#ef4444';
      ctx.lineWidth = 2;
      ctx.setLineDash([5, 3]);
      ctx.beginPath();
      ctx.moveTo(leftPad, y + cellH);
      ctx.lineTo(leftPad + 24 * cellW, y + cellH);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();
    }
  }

  // Color scale legend
  const scaleY = topPad + nDays * cellH + 12;
  const scaleW = nHours * cellW;
  const grad   = ctx.createLinearGradient(leftPad, 0, leftPad + scaleW, 0);
  grad.addColorStop(0,   '#22c55e');
  grad.addColorStop(0.5, '#eab308');
  grad.addColorStop(1,   '#ef4444');
  ctx.fillStyle = grad;
  ctx.fillRect(leftPad, scaleY, scaleW, 8);
  ctx.fillStyle = '#999';
  ctx.font = '9px system-ui';
  ctx.textAlign = 'left';
  ctx.fillText('\u20ac' + data.price_min.toFixed(0) + '/MWh', leftPad, scaleY + 20);
  ctx.textAlign = 'right';
  ctx.fillText('\u20ac' + data.price_max.toFixed(0) + '/MWh', leftPad + scaleW, scaleY + 20);
  ctx.textAlign = 'center';
  ctx.fillText('price scale', leftPad + scaleW / 2, scaleY + 20);

  // Hover tooltip
  const tooltip = document.getElementById('heatmapTooltip');
  canvas.onmousemove = function(e) {
    const rect  = canvas.getBoundingClientRect();
    const scX   = logW / rect.width;
    const scY   = logH / rect.height;
    const mx    = (e.clientX - rect.left) * scX;
    const my    = (e.clientY - rect.top)  * scY;
    const h     = Math.floor((mx - leftPad) / cellW);
    const i     = Math.floor((my - topPad)  / cellH);
    if (h >= 0 && h < 24 && i >= 0 && i < nDays) {
      const price = data.grid[i][h];
      if (price !== null && price !== undefined) {
        const hEnd = h + 1;
        tooltip.textContent =
          data.dates[i] + '  \u2014  ' +
          String(h).padStart(2,'0') + ':00\u2013' + String(hEnd).padStart(2,'00') + ':00  \u2014  ' +
          '\u20ac' + price.toFixed(2) + '/MWh';
        tooltip.style.display = 'block';
        tooltip.style.left = (e.pageX + 14) + 'px';
        tooltip.style.top  = (e.pageY - 36) + 'px';
        return;
      }
    }
    tooltip.style.display = 'none';
  };
  canvas.onmouseleave = () => { tooltip.style.display = 'none'; };
}
</script>"""


# ── HTML rendering ────────────────────────────────────────────────────────────

def render_html(yesterday, points, da_prices):
    shortage_vals = [(p["timeInterval_start"][11:16], safe_float(p.get("shortage")))
                     for p in points if safe_float(p.get("shortage")) is not None]
    surplus_vals  = [(p["timeInterval_start"][11:16], safe_float(p.get("surplus")))
                     for p in points if safe_float(p.get("surplus")) is not None]

    max_shortage = max(shortage_vals, key=lambda x: x[1])
    min_shortage = min(shortage_vals, key=lambda x: x[1])
    max_surplus  = max(surplus_vals,  key=lambda x: x[1])

    s_min   = min_shortage[1]
    s_max   = max_shortage[1]
    s_range = s_max - s_min if s_max != s_min else 1

    reg_counts = Counter(p.get("regulation_state", 0) for p in points)

    # ── spread stats ──────────────────────────────────────────────────────────
    spread_list = []
    for i, p in enumerate(points):
        da       = da_prices[i] if i < len(da_prices) else None
        shortage = safe_float(p.get("shortage"))
        if da is not None and shortage is not None:
            spread_list.append(da - shortage)

    avg_spread       = sum(spread_list) / len(spread_list) if spread_list else None
    avg_spread_str   = f"{avg_spread:+.2f}" if avg_spread is not None else "—"
    avg_spread_color = "#198754" if (avg_spread is not None and avg_spread >= 0) else "#dc3545"

    # ── chart data ────────────────────────────────────────────────────────────
    chart_labels  = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]
    shortage_data = [safe_float(p.get("shortage")) for p in points]
    surplus_data  = [safe_float(p.get("surplus"))  for p in points]
    da_data       = [da_prices[i] if i < len(da_prices) else None for i in range(len(points))]

    js_labels   = str(chart_labels).replace("'", '"')
    js_shortage = to_js(shortage_data)
    js_surplus  = to_js(surplus_data)
    js_da       = to_js(da_data)

    # ── table rows ────────────────────────────────────────────────────────────
    rows = []
    for i, p in enumerate(points):
        time_str = p["timeInterval_start"][11:16]
        reg      = p.get("regulation_state", 0)
        color    = REG_COLORS.get(reg, "#ffffff")
        shortage = safe_float(p.get("shortage"))
        da       = da_prices[i] if i < len(da_prices) else None

        bar_pct   = max(0, int(round((shortage - s_min) / s_range * 100))) if shortage is not None else 0
        bar_color = "#dc3545" if (shortage is not None and shortage < 0) else "#0d6efd"

        if da is not None and shortage is not None:
            spread = da - shortage
            sc     = "#198754" if spread >= 0 else "#dc3545"
            spread_cell = f'<span style="color:{sc};font-weight:600">{spread:+.2f}</span>'
        else:
            spread_cell = "—"

        rows.append(f"""
        <tr style="background:{color}">
          <td>{time_str}</td>
          <td>{REG_LABELS.get(reg, str(reg))}</td>
          <td class="num">{fmt(p.get('shortage'))}</td>
          <td class="num">{fmt(p.get('surplus'))}</td>
          <td class="num">{fmt(da)}</td>
          <td class="num">{spread_cell}</td>
          <td class="num">{fmt(p.get('dispatch_up'))}</td>
          <td class="num">{fmt(p.get('dispatch_down'))}</td>
          <td class="bar-cell"><div class="bar" style="width:{bar_pct}%;background:{bar_color}"></div></td>
        </tr>""")

    # ── regulation breakdown bar ──────────────────────────────────────────────
    reg_bars = ""
    total = len(points)
    for state, name in sorted(REG_LABELS.items()):
        count = reg_counts.get(state, 0)
        pct   = count / total * 100 if total else 0
        color = REG_COLORS.get(state, "#eee")
        reg_bars += (f'<div class="reg-bar" style="width:{pct:.1f}%;background:{color};'
                     f'border:1px solid #ccc" title="{name}: {count}">{name} {count}</div>')

    # ── HTML ──────────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Power Dashboard · {yesterday.strftime('%d %B %Y')}</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; font-size: 13px; background: #f5f5f5; color: #222; }}
    header {{ background: #1a1a2e; color: #fff; padding: 16px 24px; }}
    header h1 {{ font-size: 18px; font-weight: 600; }}
    header p {{ color: #aaa; font-size: 12px; margin-top: 4px; }}

    /* Tab nav */
    .tab-nav {{ background: #13132a; border-bottom: 1px solid #2a2a4a; display: flex; gap: 0; padding: 0 24px; }}
    .tab-nav button {{
      background: none; border: none; color: #888; cursor: pointer;
      font-size: 13px; font-weight: 500; padding: 12px 20px;
      border-bottom: 2px solid transparent; margin-bottom: -1px;
      transition: color .15s, border-color .15s;
    }}
    .tab-nav button:hover {{ color: #ccc; }}
    .tab-nav button.active {{ color: #fff; border-bottom-color: #4f8ef7; }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}

    .container {{ max-width: 1200px; margin: 24px auto; padding: 0 16px; }}

    /* Crisis View */
    .crisis-context {{ background: #1a1a3a; border-left: 3px solid #4f8ef7; color: #aaa;
                       font-size: 13px; line-height: 1.7; padding: 14px 28px; }}
    .crisis-context strong {{ color: #ddd; }}
    .spinner {{ width: 36px; height: 36px; border: 3px solid #e0e0e0;
                border-top-color: #4f8ef7; border-radius: 50%;
                animation: spin 0.7s linear infinite; margin: 0 auto 12px; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

    /* KPI cards */
    .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
    .card {{ background: #fff; border-radius: 8px; padding: 16px 20px; flex: 1; min-width: 160px;
             box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
    .card .label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: .5px; }}
    .card .value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
    .card .sub   {{ font-size: 11px; color: #666; margin-top: 2px; }}
    .card.peak   .value {{ color: #dc3545; }}
    .card.trough .value {{ color: #0d6efd; }}

    /* Chart */
    .chart-wrap {{ background: #fff; border-radius: 8px; padding: 20px;
                   box-shadow: 0 1px 3px rgba(0,0,0,.1); margin-bottom: 24px; position: relative; height: 300px; }}

    /* Regulation breakdown */
    .section-title {{ font-size: 12px; font-weight: 600; color: #666;
                      text-transform: uppercase; letter-spacing: .5px; margin-bottom: 8px; }}
    .reg-row {{ display: flex; width: 100%; height: 32px; border-radius: 6px; overflow: hidden;
                margin-bottom: 24px; gap: 2px; align-items: center; }}
    .reg-bar {{ height: 100%; display: flex; align-items: center; justify-content: center;
                font-size: 11px; font-weight: 600; overflow: hidden; white-space: nowrap; border-radius: 4px; }}

    /* Table */
    .table-wrap {{ background: #fff; border-radius: 8px; overflow: auto;
                   box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
    table {{ border-collapse: collapse; width: 100%; }}
    th {{ background: #1a1a2e; color: #fff; padding: 8px 12px; text-align: left;
          font-size: 12px; position: sticky; top: 0; white-space: nowrap; }}
    td {{ padding: 5px 12px; border-bottom: 1px solid #f0f0f0; white-space: nowrap; }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    td.bar-cell {{ width: 120px; padding: 5px 8px; }}
    .bar {{ height: 12px; border-radius: 3px; min-width: 1px; }}
    tr:last-child td {{ border-bottom: none; }}
    th.da  {{ background: #1a3a6e; }}
    th.spr {{ background: #1a4a2e; }}
  </style>
</head>
<body>
  <header>
    <h1>Power Dashboard &nbsp;·&nbsp; {yesterday.strftime('%A %d %B %Y')}</h1>
    <p>TenneT settlement prices &amp; ENTSO-E day-ahead · {len(points)} PTUs · NL (10YNL----------L)</p>
  </header>

  <nav class="tab-nav">
    <button class="active" onclick="showTab('daily-imbalance', this)">Daily Imbalance</button>
    <button onclick="showTab('crisis-view', this)">Crisis View</button>
    <button onclick="showTab('gas-storage', this)">Gas Storage</button>
    <button onclick="showTab('renewables-nl', this)">Renewables NL</button>
    <button onclick="showTab('price-heatmap', this)">Price Heatmap</button>
  </nav>

  <div id="daily-imbalance" class="tab-panel active">
  <div class="container">

    <!-- KPI cards -->
    <div class="cards">
      <div class="card peak">
        <div class="label">Peak Shortage</div>
        <div class="value">{max_shortage[1]:.2f} <span style="font-size:14px;font-weight:400">€/MWh</span></div>
        <div class="sub">at {max_shortage[0]}</div>
      </div>
      <div class="card trough">
        <div class="label">{'Negative' if min_shortage[1] < 0 else 'Trough'} Shortage</div>
        <div class="value">{min_shortage[1]:.2f} <span style="font-size:14px;font-weight:400">€/MWh</span></div>
        <div class="sub">at {min_shortage[0]}</div>
      </div>
      <div class="card">
        <div class="label">Peak Surplus</div>
        <div class="value" style="color:#198754">{max_surplus[1]:.2f} <span style="font-size:14px;font-weight:400">€/MWh</span></div>
        <div class="sub">at {max_surplus[0]}</div>
      </div>
      <div class="card">
        <div class="label">Shortage Range</div>
        <div class="value">{s_max - s_min:.2f} <span style="font-size:14px;font-weight:400">€/MWh</span></div>
        <div class="sub">daily spread</div>
      </div>
      <div class="card">
        <div class="label">Avg DA – Shortage Spread</div>
        <div class="value" style="color:{avg_spread_color}">{avg_spread_str} <span style="font-size:14px;font-weight:400">€/MWh</span></div>
        <div class="sub">+&nbsp;market overestimated &nbsp;·&nbsp; − underestimated</div>
      </div>
    </div>

    <!-- Line chart -->
    <div class="section-title">Price comparison — all 96 PTUs</div>
    <div class="chart-wrap">
      <canvas id="priceChart"></canvas>
    </div>

    <!-- Regulation breakdown -->
    <div class="section-title">Regulation state breakdown</div>
    <div class="reg-row">{reg_bars}</div>

    <!-- PTU table -->
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Time</th>
          <th>State</th>
          <th>Shortage</th>
          <th>Surplus</th>
          <th class="da">DA Price</th>
          <th class="spr">Spread (DA−Short)</th>
          <th>Disp Up</th>
          <th>Disp Down</th>
          <th>Shortage Chart</th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>

  </div><!-- /container -->
  </div><!-- /tab daily-imbalance -->

  CRISIS_VIEW_PLACEHOLDER
  GAS_STORAGE_PLACEHOLDER
  RENEWABLES_PLACEHOLDER
  HEATMAP_PLACEHOLDER

  <script>
    function showTab(id, btn) {{
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.tab-nav button').forEach(b => b.classList.remove('active'));
      document.getElementById(id).classList.add('active');
      btn.classList.add('active');
      if (id === 'crisis-view')   loadCrisisData();
      if (id === 'gas-storage')   loadGasStorageData();
      if (id === 'renewables-nl') loadRenewablesData();
      if (id === 'price-heatmap') loadHeatmapData();
    }}
  </script>

  <script>
    const ctx = document.getElementById('priceChart').getContext('2d');
    new Chart(ctx, {{
      type: 'line',
      data: {{
        labels: {js_labels},
        datasets: [
          {{
            label: 'DA Price (ENTSO-E)',
            data: {js_da},
            borderColor: '#0d6efd',
            backgroundColor: 'rgba(13,110,253,0.06)',
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.25,
            spanGaps: true,
            fill: false,
          }},
          {{
            label: 'Shortage (TenneT)',
            data: {js_shortage},
            borderColor: '#dc3545',
            backgroundColor: 'rgba(220,53,69,0.06)',
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.25,
            spanGaps: true,
            fill: false,
          }},
          {{
            label: 'Surplus (TenneT)',
            data: {js_surplus},
            borderColor: '#198754',
            backgroundColor: 'rgba(25,135,84,0.06)',
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.25,
            spanGaps: true,
            fill: false,
          }},
        ]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{
          legend: {{ position: 'top', labels: {{ font: {{ size: 12 }} }} }},
          tooltip: {{
            callbacks: {{
              label: ctx => `${{ctx.dataset.label}}: ${{ctx.parsed.y !== null ? ctx.parsed.y.toFixed(2) + ' €/MWh' : '—'}}`
            }}
          }}
        }},
        scales: {{
          x: {{
            ticks: {{ maxTicksLimit: 25, font: {{ size: 11 }}, maxRotation: 0 }},
            grid: {{ color: 'rgba(0,0,0,0.05)' }}
          }},
          y: {{
            title: {{ display: true, text: '€/MWh', font: {{ size: 11 }} }},
            ticks: {{ font: {{ size: 11 }} }},
            grid: {{ color: 'rgba(0,0,0,0.05)' }}
          }}
        }}
      }}
    }});
  </script>
  SHARED_JS_PLACEHOLDER
  CRISIS_JS_PLACEHOLDER
  GAS_STORAGE_JS_PLACEHOLDER
  RENEWABLES_JS_PLACEHOLDER
  HEATMAP_JS_PLACEHOLDER
</body>
</html>"""
    html = html.replace("CRISIS_VIEW_PLACEHOLDER",   _crisis_html())
    html = html.replace("GAS_STORAGE_PLACEHOLDER",   _gas_storage_html())
    html = html.replace("RENEWABLES_PLACEHOLDER",    _renewables_html())
    html = html.replace("HEATMAP_PLACEHOLDER",       _heatmap_html())
    html = html.replace("SHARED_JS_PLACEHOLDER",     _shared_js())
    html = html.replace("CRISIS_JS_PLACEHOLDER",     _crisis_js())
    html = html.replace("GAS_STORAGE_JS_PLACEHOLDER", _gas_storage_js())
    html = html.replace("RENEWABLES_JS_PLACEHOLDER", _renewables_js())
    html = html.replace("HEATMAP_JS_PLACEHOLDER",    _heatmap_js())
    return html

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if   self.path == "/api/crisis":      self._serve_crisis_api()
        elif self.path == "/api/gas-storage": self._serve_json_api(build_gas_storage_data, _gas_cache)
        elif self.path == "/api/renewables":  self._serve_json_api(build_renewables_data,  _renewables_cache)
        elif self.path == "/api/heatmap":     self._serve_json_api(build_heatmap_data,     _heatmap_cache)
        else:                                 self._serve_dashboard()

    def _serve_dashboard(self):
        try:
            yesterday, points, da_prices = fetch_data()
            body = render_html(yesterday, points, da_prices).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        except Exception as e:
            import traceback
            body = f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _serve_json_api(self, builder, cache):
        """Generic cached JSON API endpoint."""
        today = str(date.today())
        if cache["date"] == today and cache["data"] is not None:
            data = cache["data"]
        else:
            try:
                data = builder()
                cache["data"] = data
                cache["date"] = today
            except Exception as exc:
                import traceback
                data = {"error": str(exc), "detail": traceback.format_exc()}
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_crisis_api(self):
        try:
            data = build_crisis_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        except Exception as exc:
            import traceback
            body = json.dumps({"error": str(exc),
                               "detail": traceback.format_exc()}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress request logs

# ── entry point ───────────────────────────────────────────────────────────────

def port_in_use(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0

if __name__ == "__main__":
    host = "127.0.0.1"
    port = int(os.environ.get("PORT", 5050))
    url  = f"http://{host}:{port}"

    if port_in_use(host, port):
        print(f"Dashboard already running — opening {url}", flush=True)
        webbrowser.open(url)
    else:
        print(f"Starting Power Dashboard at {url}", flush=True)
        threading.Timer(1.5, webbrowser.open, args=[url]).start()
        server = HTTPServer((host, port), Handler)
        server.serve_forever()
