import os
import sys
import json
import socket
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
            before_avg = sum(p for _, p in before) / len(before) if before else None
            after_avg  = sum(p for _, p in after)  / len(after)  if after  else None
            error      = None
        except Exception as exc:
            print(f"[CRISIS] {code}: {exc}", flush=True)
            before_avg = after_avg = None
            error = str(exc)
        return {
            "code":       code,
            "name":       name,
            "group":      group,
            "before_avg": round(before_avg, 2) if before_avg is not None else None,
            "after_avg":  round(after_avg,  2) if after_avg  is not None else None,
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
        '<div class="chart-wrap" style="height:155px">'
        '<canvas id="chartGas"></canvas>'
        '</div>'
        '<p style="font-size:11px;color:#888;margin:-16px 0 20px 4px">'
        'Note: &ldquo;Before&rdquo; covers 28&nbsp;days (1&ndash;27&nbsp;Feb); '
        '&ldquo;After&rdquo; covers fewer days and grows daily.</p>'
        '<div class="section-title" style="margin-top:8px">'
        'Low-carbon: Hydro &amp; Nuclear &mdash; FR &middot; ES &middot; NO South'
        '</div>'
        '<div class="chart-wrap" style="height:155px">'
        '<canvas id="chartLow"></canvas>'
        '</div>'
        '<p style="font-size:11px;color:#888;margin:-16px 0 20px 4px">'
        'Note: &ldquo;Before&rdquo; covers 28&nbsp;days (1&ndash;27&nbsp;Feb); '
        '&ldquo;After&rdquo; covers fewer days and grows daily.</p>'
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

// Plugin: % change badges drawn inline at right end of the "after" bar
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
      const text = sign + pct.toFixed(1) + '%';
      const fg   = pct >= 0 ? '#dc3545' : '#198754';
      const bg   = pct >= 0 ? 'rgba(220,53,69,0.12)' : 'rgba(25,135,84,0.12)';
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

// Plugin: vertical dashed event lines on the NL trajectory
// Pass { below: true } on a line to draw label below the top instead of above
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
      ctx.fillStyle  = color;
      ctx.font       = 'bold 10px system-ui';
      ctx.textAlign  = 'center';
      const labelY   = below ? chartArea.top + 16 : chartArea.top - 7;
      ctx.fillText(label, x, labelY);
      ctx.restore();
    });
  }
};

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
          label: 'Before  1\u201327 Feb',
          data: countries.map(c => c.before_avg),
          backgroundColor: 'rgba(120,120,120,0.28)',
          borderColor:     'rgba(120,120,120,0.55)',
          borderWidth: 1,
          barThickness: 26,
        },
        {
          label: 'After  2 Mar\u2013yesterday',
          data: countries.map(c => c.after_avg),
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
          min: 50,
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

  <script>
    function showTab(id, btn) {{
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.tab-nav button').forEach(b => b.classList.remove('active'));
      document.getElementById(id).classList.add('active');
      btn.classList.add('active');
      if (id === 'crisis-view') loadCrisisData();
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
  CRISIS_JS_PLACEHOLDER
</body>
</html>"""
    html = html.replace("CRISIS_VIEW_PLACEHOLDER", _crisis_html())
    html = html.replace("CRISIS_JS_PLACEHOLDER",   _crisis_js())
    return html

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/crisis":
            self._serve_crisis_api()
        else:
            self._serve_dashboard()

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
