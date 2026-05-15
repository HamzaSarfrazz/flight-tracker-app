import streamlit as st
import json
import re
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Tuple
import pandas as pd
import plotly.graph_objects as go

# ============================================================================
# Constants
# ============================================================================
MONTH_MAP = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
}

CONFIG_CAP = {
    'B': 330, 'F': 218, 'G': 218, 'M': 144, 'NXA': 235, 'O': 150,
    'P': 174, 'Q': 220, 'R': 174, 'S': 342, 'T': 180, 'X': 212, 'Z': 180,
    'K': 180, 'V': 180, 'U': 180, 'W': 180, 'L': 180, 'H': 180
}

DATE_HDR_RE   = re.compile(r'(MON|TUE|WED|THU|FRI|SAT|SUN)\s*-\s*(\d+)([A-Z]{3})')
ROUTE_RE      = re.compile(r'([A-Z]{3}-[A-Z]{3})\s+PA\s+\d+')
FLT_RE        = re.compile(r'^(\d{3})$')
REV_RE        = re.compile(r'^([\d,]+)k$')
Y4_RE         = re.compile(r'Y:(\d+)-(\d+)-(\d+)-(\d+)')
Y3_RE         = re.compile(r'Y:(\d+)-(\d+)-(\d+)$')
CONFIG_RE     = re.compile(r'^([A-Z]{1,3})(?:\s|$)')
FLIGHT_SEL_RE = re.compile(r'Flight Selections:')
DOMESTIC_RE   = re.compile(r'Domestic Flights', re.IGNORECASE)
INTL_RE       = re.compile(r'International Flights', re.IGNORECASE)

# ============================================================================
# Parser Helpers
# ============================================================================
def get_correct_year(month: int, day: int) -> int:
    today = date.today()
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return today.year
    if candidate < today - timedelta(days=30):
        return today.year + 1
    return today.year

def parse_date_headers(lines: List[str]) -> List[str]:
    dates, seen = [], set()
    blob = " ".join(lines[:80])
    for m in DATE_HDR_RE.finditer(blob):
        day   = int(m.group(2))
        month = MONTH_MAP.get(m.group(3), 0)
        if month == 0:
            continue
        year = get_correct_year(month, day)
        try:
            ds = date(year, month, day).isoformat()
            if ds not in seen:
                seen.add(ds)
                dates.append(ds)
        except ValueError:
            continue
    return dates

def extract_snapshot_metadata(lines: List[str]) -> Dict[str, str]:
    metadata = {"filters": "Unknown", "type": "Unknown", "name": "Unknown", "short_code": "Unknown"}
    flight_sel_tokens = []

    for i, line in enumerate(lines):
        if FLIGHT_SEL_RE.search(line):
            j = i
            while j < len(lines):
                token_line = lines[j].strip()
                if token_line and not any(h in token_line for h in [
                    "Display Options:", "Date Range:", "Domestic Flights", "International Flights"
                ]):
                    flight_sel_tokens.extend(token_line.split())
                else:
                    if any(h in token_line for h in [
                        "Display Options:", "Date Range:", "Domestic Flights", "International Flights"
                    ]):
                        break
                j += 1
            break

    if "PA" in flight_sel_tokens:
        idx = flight_sel_tokens.index("PA")
        flight_num = None
        location_tokens = []
        i = idx + 1
        while i < len(flight_sel_tokens):
            token = flight_sel_tokens[i]
            if token.isdigit() and len(token) == 3:
                flight_num = token; i += 1; break
            if token == "[All]":
                flight_num = "All"; i += 1; break
            i += 1
        if flight_num:
            in_bracket  = False
            bracket_text = ""
            while i < len(flight_sel_tokens):
                token = flight_sel_tokens[i]
                if token.startswith('[') and not token.endswith(']'):
                    in_bracket   = True
                    bracket_text = token
                    i += 1; continue
                if in_bracket:
                    bracket_text += " " + token
                    if token.endswith(']'):
                        if 'config' in bracket_text.lower():
                            break
                        else:
                            location_tokens.append(bracket_text.strip('[]'))
                            in_bracket = False; bracket_text = ""
                    i += 1; continue
                if token.startswith('[') and token.endswith(']'):
                    if 'config' in token.lower():
                        break
                    else:
                        location_tokens.append(token.strip('[]'))
                else:
                    location_tokens.append(token)
                i += 1
            loc_parts = [t.rstrip(',') for t in location_tokens if t.rstrip(',')]
            if loc_parts:
                metadata["short_code"] = f"[{flight_num}][{', '.join(loc_parts)}]"

    for line in lines:
        if DOMESTIC_RE.search(line):
            metadata["type"] = "Domestic"
        elif INTL_RE.search(line):
            metadata["type"] = "International"

    metadata["name"] = (
        f"{metadata['short_code']} - {metadata['type']}"
        if metadata["short_code"] != "Unknown"
        else metadata["type"]
    )
    return metadata

# ============================================================================
# Grid Parser
# ============================================================================
def parse_grid(raw_text: str, progress_callback=None) -> Tuple[Dict, List[str], Dict]:
    lines     = [l.strip() for l in raw_text.splitlines()]
    col_dates = parse_date_headers(lines)
    if not col_dates:
        return {}, [], {}

    metadata    = extract_snapshot_metadata(lines)
    n_cols      = len(col_dates)
    grid        = {d: [] for d in col_dates}
    idx         = 0
    total_lines = len(lines)

    def _parse_cell(clines):
        revenue_k = ticketed = reserved = capacity = 0
        departed  = False
        for cline in clines:
            rev_m = REV_RE.match(cline)
            if rev_m and revenue_k == 0:
                revenue_k = int(rev_m.group(1).replace(",", "")); continue
            y4_m = Y4_RE.search(cline)
            if y4_m and ticketed == 0:
                ticketed  = int(y4_m.group(1))
                reserved  = int(y4_m.group(2))
                capacity  = int(y4_m.group(3))
                departed  = False; continue
            y3_m = Y3_RE.search(cline)
            if y3_m and ticketed == 0:
                ticketed  = int(y3_m.group(1))
                reserved  = int(y3_m.group(2))
                departed  = True; continue
            if "XBAG" in cline:
                continue
            cfg_m = CONFIG_RE.match(cline)
            if cfg_m and capacity == 0 and not departed:
                code = cfg_m.group(1)
                if code in CONFIG_CAP:
                    capacity = CONFIG_CAP[code]
        return {
            "ticketed":  ticketed  if ticketed  != 0 else None,
            "reserved":  reserved,
            "capacity":  capacity,
            "revenue_k": revenue_k,
            "departed":  departed,
        }

    current_route = "Unknown"
    while idx < total_lines:
        line        = lines[idx]
        route_match = ROUTE_RE.search(line)
        if route_match:
            current_route = route_match.group(1)
            idx += 1
            block = []
            while idx < total_lines:
                l = lines[idx]
                if ROUTE_RE.search(l) or l.startswith("Totals:") or l.startswith("Grand Totals:"):
                    break
                if l:
                    block.append(l)
                idx += 1
            flight_num = next((bl for bl in block if FLT_RE.match(bl)), None)
            if not flight_num:
                continue
            markers = [i for i, bl in enumerate(block) if bl.lower() == "n/a" or (FLT_RE.match(bl) and bl == flight_num)]
            col_entries = []
            for i in range(len(markers)):
                start     = markers[i]
                end       = markers[i+1] if i+1 < len(markers) else len(block)
                cell_lines = block[start+1:end]
                if block[start].lower() == "n/a":
                    col_entries.append({"route": current_route, "flt": flight_num,
                                        "ticketed": None, "reserved": None, "capacity": None,
                                        "revenue_k": 0, "departed": False})
                else:
                    parsed = _parse_cell(cell_lines)
                    col_entries.append({"route": current_route, "flt": flight_num, **parsed})
            while len(col_entries) < n_cols:
                col_entries.append({"route": current_route, "flt": flight_num,
                                    "ticketed": None, "reserved": None, "capacity": None,
                                    "revenue_k": 0, "departed": False})
            col_entries = col_entries[:n_cols]
            for col_pos, entry in enumerate(col_entries):
                grid[col_dates[col_pos]].append(entry)
            if progress_callback:
                progress_callback(idx / total_lines)
            continue
        idx += 1

    return grid, col_dates, metadata

# ============================================================================
# Storage — Supabase
# ============================================================================
from supabase import create_client

def _get_supabase():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

@st.cache_data(ttl=5)
def load_db() -> List[Dict]:
    try:
        sb     = _get_supabase()
        result = sb.table("snapshots").select("*").order("pasted_at").execute()
        records = []
        for row in result.data:
            rec = json.loads(row["payload"])
            rec["_id"] = row["id"]
            if "snapshot_name" not in rec:
                dates     = rec.get("col_dates", [])
                date_part = f"{dates[0]}→{dates[-1]}" if dates else "unknown"
                meta_name = rec.get("metadata", {}).get("name", "Snapshot")
                rec["snapshot_name"] = f"{meta_name} {date_part} - {rec.get('pasted_at', 'unknown')}"
            records.append(rec)
        return records
    except Exception as e:
        st.error(f"DATABASE_LINK_FAILED: {e}")
        return []

def save_db(records: List[Dict]):
    try:
        sb = _get_supabase()
        sb.table("snapshots").delete().neq("id", 0).execute()
        for rec in records:
            payload = {k: v for k, v in rec.items() if k != "_id"}
            sb.table("snapshots").insert({"payload": json.dumps(payload)}).execute()
        st.cache_data.clear()
    except Exception as e:
        st.error(f"WRITE_FAULT: {e}")

# ============================================================================
# Light theme
# ============================================================================
def apply_aero_theme():
    st.markdown("""
    <style>

    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    :root {
        --bg: #f4f7fb;
        --sidebar: #ffffff;
        --card: #ffffff;

        --border: #e2e8f0;
        --border-soft: #edf2f7;

        --text: #0f172a;
        --muted: #64748b;

        --primary: #2563eb;
        --primary-hover: #1d4ed8;
        --primary-soft: #dbeafe;

        --green: #10b981;
        --green-soft: #dcfce7;

        --amber: #f59e0b;
        --amber-soft: #fef3c7;

        --red: #ef4444;
        --red-soft: #fee2e2;
    }

    html, body, [class*="css"], .stApp {
        background: var(--bg) !important;
        color: var(--text) !important;
        font-family: 'Inter', sans-serif !important;
    }

    .main .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
    }

    [data-testid="stSidebar"] {
        background: var(--sidebar) !important;
        border-right: 1px solid var(--border);
    }

    [data-testid="stSidebar"] * {
        font-family: 'Inter', sans-serif !important;
    }

    .aero-header {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 28px;
        padding-bottom: 20px;
        border-bottom: 1px solid var(--border);
    }

    .aero-wordmark {
        font-size: 2rem;
        font-weight: 800;
        letter-spacing: -0.05em;
        color: var(--text);
    }

    .aero-wordmark span {
        color: var(--primary);
    }

    .aero-badge {
        padding: 6px 12px;
        background: var(--primary-soft);
        color: var(--primary);
        border-radius: 999px;
        font-size: 0.7rem;
        font-weight: 600;
    }

    .aero-label {
        margin-bottom: 8px;
        color: var(--muted);
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
    }

    [data-testid="metric-container"] {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 22px;
        padding: 22px;
        box-shadow:
            0 1px 2px rgba(0,0,0,0.04),
            0 8px 24px rgba(15,23,42,0.04);
    }

    [data-testid="stMetricLabel"] {
        color: var(--muted) !important;
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        letter-spacing: 0.08em;
        text-transform: uppercase;
    }

    [data-testid="stMetricValue"] {
        color: var(--text) !important;
        font-size: 2rem !important;
        font-weight: 800 !important;
        letter-spacing: -0.03em;
    }

    .stButton > button {
        width: 100%;
        border: none !important;
        background: var(--primary) !important;
        color: white !important;
        border-radius: 14px !important;
        padding: 0.75rem 1rem !important;
        font-size: 0.85rem !important;
        font-weight: 700 !important;
        transition: all 0.2s ease;
    }

    .stButton > button:hover {
        background: var(--primary-hover) !important;
        transform: translateY(-1px);
        box-shadow: 0 8px 18px rgba(37,99,235,0.25);
    }

    .stTextArea textarea,
    .stSelectbox select,
    .stTextInput input {
        background: white !important;
        border: 1px solid var(--border) !important;
        border-radius: 14px !important;
        color: var(--text) !important;
        font-size: 0.9rem !important;
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background: transparent !important;
        border-bottom: none !important;
    }

    .stTabs [data-baseweb="tab"] {
        background: white !important;
        border: 1px solid var(--border) !important;
        border-radius: 14px !important;
        padding: 10px 18px !important;
        color: var(--muted) !important;
        font-weight: 700 !important;
    }

    .stTabs [aria-selected="true"] {
        background: var(--primary-soft) !important;
        border-color: var(--primary) !important;
        color: var(--primary) !important;
    }

    table {
        border-collapse: collapse !important;
        width: 100% !important;
        background: white !important;
        border-radius: 18px !important;
        overflow: hidden !important;
        border: 1px solid var(--border) !important;
    }

    thead tr {
        background: #f8fafc !important;
    }

    th {
        padding: 14px !important;
        color: var(--muted) !important;
        font-size: 0.78rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        border-bottom: 1px solid var(--border) !important;
    }

    td {
        padding: 12px !important;
        border-bottom: 1px solid var(--border-soft) !important;
        color: var(--text) !important;
        font-size: 0.85rem !important;
    }

    tr:hover td {
        background: #f8fbff !important;
    }

    hr {
        border-color: var(--border) !important;
    }

    [data-testid="stDataFrame"] {
        border: 1px solid var(--border) !important;
        border-radius: 16px !important;
        overflow: hidden !important;
        background: white !important;
    }

    [data-testid="stDataFrame"] thead tr th {
        background: #f8fafc !important;
        color: var(--muted) !important;
        font-size: 0.75rem !important;
        font-weight: 700 !important;
    }

    [data-testid="stDataFrame"] tbody tr td {
        background: white !important;
        color: var(--text) !important;
        font-size: 0.85rem !important;
    }

    [data-testid="stDataFrame"] tbody tr:hover td {
        background: #f8fbff !important;
    }

    </style>
    """, unsafe_allow_html=True)

# ============================================================================
# Plotly theme helper
# ============================================================================
PLOTLY_LAYOUT = dict(
    template="plotly_white",

    paper_bgcolor="#ffffff",
    plot_bgcolor="#ffffff",

    font=dict(
        family="Inter",
        color="#0f172a",
        size=12
    ),

    xaxis=dict(
        gridcolor="#e2e8f0",
        linecolor="#e2e8f0",
        zerolinecolor="#e2e8f0"
    ),

    yaxis=dict(
        gridcolor="#e2e8f0",
        linecolor="#e2e8f0",
        zerolinecolor="#e2e8f0"
    ),

    margin=dict(
        l=40,
        r=40,
        t=60,
        b=40
    ),

    height=420,

    hovermode="x unified",

    hoverlabel=dict(
        bgcolor="#ffffff",
        bordercolor="#cbd5e1",
        font_family="Inter",
        font_size=12,
        font_color="#0f172a"
    ),

    legend=dict(
        bgcolor="rgba(255,255,255,0.85)",
        bordercolor="#e2e8f0",
        borderwidth=1
    )
)

def ts_now():
    return datetime.now(ZoneInfo("Asia/Karachi")).strftime("%Y-%m-%d %H:%M:%S PKT")

def fmt_day(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%a %d %b")
    except ValueError:
        return iso

def flight_label(route: str, flt: str) -> str:
    return f"PA {flt} · {route}"

def collect_flights(grid: Dict, dates: List[str]) -> List[Tuple[str, str]]:
    keys, seen = [], set()
    for d in dates:
        for f in grid.get(d, []):
            k = (f["route"], f["flt"])
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys

def snapshot_cell(entry) -> str:
    if not entry or entry.get("ticketed") is None:
        return "—"
    if entry.get("departed"):
        return "Departed"
    t = entry["ticketed"]
    c = entry.get("capacity") or 0
    if c:
        return f"{t}/{c} ({t / c * 100:.0f}%)"
    return str(t)

def _style_delta(val):
    if val is None or val == "" or val == "—" or val == "Dep":
        return ""
    if isinstance(val, (int, float)) and not pd.isna(val):
        if val > 0:
            return "background-color:#dcfce7;color:#166534;font-weight:600"
        if val < 0:
            return "background-color:#fee2e2;color:#991b1b;font-weight:600"
        return "background-color:#f1f5f9;color:#64748b;font-weight:500"
    return ""

def _fmt_delta(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "—"
    if val == "Dep":
        return "Departed"
    if isinstance(val, (int, float)):
        return f"{int(val):+d}" if val != 0 else "0"
    return str(val)

def _style_snapshot_cell(val):
    if not isinstance(val, str) or "(" not in val:
        return ""
    try:
        pct = float(val.split("(")[-1].rstrip("%)"))
        if pct >= 91:
            return "background-color:#dcfce7;color:#166534"
        if pct >= 76:
            return "background-color:#fef3c7;color:#92400e"
        return "background-color:#fee2e2;color:#991b1b"
    except ValueError:
        return ""

def show_delta_table(df: pd.DataFrame, day_cols: List[str]):
    styled = (
        df.style
        .map(_style_delta, subset=day_cols + ["Net change"])
        .format(_fmt_delta, subset=day_cols + ["Net change"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=min(520, 44 + len(df) * 35))

def show_snapshot_table(df: pd.DataFrame, day_cols: List[str]):
    styled = df.style.map(_style_snapshot_cell, subset=day_cols)
    st.dataframe(styled, use_container_width=True, hide_index=True, height=min(520, 44 + len(df) * 35))

def build_compare_table(prev_grid, curr_grid, day_specs: List[Tuple[str, str, str]]):
    day_cols = [label for label, _, _ in day_specs]
    flight_keys = set()
    for _, _, cd in day_specs:
        for f in curr_grid.get(cd, []):
            flight_keys.add((f["route"], f["flt"]))

    rows, grand_total = [], 0
    for route, flt in sorted(flight_keys):
        row = {"Flight": flight_label(route, flt)}
        ftot = 0
        for col, pd, cd in day_specs:
            pe = next(
                (f for f in prev_grid.get(pd, []) if f["route"] == route and f["flt"] == flt),
                None,
            )
            ce = next(
                (f for f in curr_grid.get(cd, []) if f["route"] == route and f["flt"] == flt),
                None,
            )
            if ce and ce.get("departed"):
                row[col] = "Dep"
                continue
            if not pe and not ce:
                row[col] = None
                continue
            pt = pe["ticketed"] if pe and pe.get("ticketed") else 0
            ct = ce["ticketed"] if ce and ce.get("ticketed") else 0
            diff = ct - pt
            ftot += diff
            row[col] = diff
        row["Net change"] = ftot
        grand_total += ftot
        rows.append(row)

    rows.append({"Flight": "All flights", **{c: "" for c in day_cols}, "Net change": grand_total})
    return pd.DataFrame(rows), day_cols, grand_total

# ============================================================================
# UI PAGES
# ============================================================================

def page_import():
    st.markdown("### Import snapshot")
    st.caption("Paste the full Flight Manager export below, then import to save it.")

    raw = st.text_area(
        "Flight Manager data",
        height=280,
        placeholder="Paste raw flight data here…",
        label_visibility="collapsed",
    )

    sync = st.button("Import snapshot", type="primary")

    if sync:
        if not raw.strip():
            st.error("Paste flight data before importing.")
            return

        prog = st.progress(0, text="Parsing…")

        def update_prog(p):
            prog.progress(min(1.0, p), text=f"Parsing… {int(p * 100)}%")

        grid, col_dates, metadata = parse_grid(raw, progress_callback=update_prog)
        prog.empty()

        if not col_dates:
            st.error("Could not find date headers in the pasted data.")
            return

        total = sum(len(v) for v in grid.values())
        if total == 0:
            st.error("No flight rows were found in the pasted data.")
            return

        pkt = datetime.now(ZoneInfo("Asia/Karachi"))
        timestamp = pkt.strftime("%Y-%m-%d %H:%M")
        date_range = f"{col_dates[0]} → {col_dates[-1]}"
        sc         = metadata.get("short_code", "Unknown")
        snapshot_name = (
            f"{sc} {date_range} - {timestamp}"
            if sc != "Unknown"
            else f"{metadata['name']} {date_range} - {timestamp}"
        )

        record = {
            "pasted_at":     pkt.strftime("%Y-%m-%d %H:%M:%S"),
            "snapshot_name": snapshot_name,
            "metadata":      metadata,
            "col_dates":     col_dates,
            "grid":          grid,
        }
        db = load_db()
        db.append(record)
        save_db(db)

        st.success(
            f"Saved **{snapshot_name}** — {total:,} flight-day rows, "
            f"{metadata.get('type', 'unknown type')}."
        )
        st.rerun()


def page_snapshot():
    db = load_db()
    if not db:
        st.info("No snapshots yet. Import data on the **Import** tab.")
        return

    st.markdown("### Snapshot")
    opts = [r["snapshot_name"] for r in db]
    sel_idx = st.selectbox(
        "Snapshot",
        range(len(db)),
        format_func=lambda i: opts[i],
        index=len(db) - 1,
        label_visibility="collapsed",
    )
    snap = db[sel_idx]
    grid = snap["grid"]
    dates = snap["col_dates"]
    day_cols = [fmt_day(d) for d in dates]

    total_tix = sum(f["ticketed"] for d in dates for f in grid[d] if f.get("ticketed"))
    total_cap = sum(f["capacity"] for d in dates for f in grid[d] if f.get("capacity"))
    lf = (total_tix / total_cap * 100) if total_cap else 0
    total_rev = sum(f["revenue_k"] for d in dates for f in grid[d] if f.get("revenue_k"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Date range", f"{fmt_day(dates[0])} → {fmt_day(dates[-1])}")
    c2.metric("Tickets sold", f"{total_tix:,}")
    c3.metric("Load factor", f"{lf:.1f}%")
    c4.metric("Revenue", f"{total_rev:,}k")

    st.caption(f"Imported {snap['pasted_at']} PKT · {snap['snapshot_name']}")

    rows = []
    for route, flt in collect_flights(grid, dates):
        row = {"Flight": flight_label(route, flt)}
        for d, col in zip(dates, day_cols):
            entry = next(
                (f for f in grid[d] if f["route"] == route and f["flt"] == flt),
                None,
            )
            row[col] = snapshot_cell(entry)
        rows.append(row)

    st.markdown("**Tickets sold per flight** — sold/capacity (load %). Departed flights are marked.")
    show_snapshot_table(pd.DataFrame(rows), day_cols)

    st.markdown("### Daily totals")
    day_totals = [sum(f["ticketed"] for f in grid[d] if f.get("ticketed")) for d in dates]
    day_caps = [sum(f["capacity"] for f in grid[d] if f.get("capacity")) for d in dates]
    lfs = [t / c * 100 if c else 0 for t, c in zip(day_totals, day_caps)]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=day_cols,
        y=day_totals,
        name="Tickets",
        marker_color="#2563eb",
        marker_line_width=0,
    ))
    fig.add_trace(go.Scatter(
        x=day_cols,
        y=lfs,
        name="Load %",
        yaxis="y2",
        mode="lines+markers",
        line=dict(color="#f59e0b", width=2),
        marker=dict(size=6, color="#f59e0b"),
    ))
    fig.update_layout(
        **PLOTLY_LAYOUT,
        title="Daily tickets and load factor",
        yaxis2=dict(
            title="Load %",
            overlaying="y",
            side="right",
            range=[0, 110],
            gridcolor="rgba(0,0,0,0)",
        ),
    )
    st.plotly_chart(fig, use_container_width=True)


def page_compare():
    db = load_db()
    if len(db) < 2:
        st.info("Import at least two snapshots to compare.")
        return

    st.markdown("### Compare snapshots")
    opts = [r["snapshot_name"] for r in db]
    c1, c2 = st.columns(2)
    with c1:
        st.caption("Baseline (older)")
        idx_a = st.selectbox(
            "Baseline",
            range(len(db)),
            format_func=lambda i: opts[i],
            index=max(0, len(db) - 2),
            label_visibility="collapsed",
            key="cmp_baseline",
        )
    with c2:
        st.caption("Current (newer)")
        idx_b = st.selectbox(
            "Current",
            range(len(db)),
            format_func=lambda i: opts[i],
            index=len(db) - 1,
            label_visibility="collapsed",
            key="cmp_current",
        )

    if idx_a == idx_b:
        st.warning("Choose two different snapshots.")
        return

    prev, curr = db[idx_a], db[idx_b]
    mode = st.radio(
        "Align by",
        ["Same calendar dates", "Same day position (WoW)"],
        horizontal=True,
    )
    wow_mode = mode.startswith("Same day")

    if not wow_mode:
        overlap = sorted(set(prev["col_dates"]) & set(curr["col_dates"]))
        if not overlap:
            st.error("No overlapping dates. Use day-position mode instead.")
            return
        day_specs = [(fmt_day(d), d, d) for d in overlap]
    else:
        n_days = min(len(prev["col_dates"]), len(curr["col_dates"]))
        day_specs = [
            (
                f"{fmt_day(curr['col_dates'][i])} (day {i + 1})",
                prev["col_dates"][i],
                curr["col_dates"][i],
            )
            for i in range(n_days)
        ]

    chart_x = [label for label, _, _ in day_specs]
    prev_totals = [
        sum(f["ticketed"] for f in prev["grid"].get(pd, []) if f.get("ticketed"))
        for _, pd, _ in day_specs
    ]
    curr_totals = [
        sum(f["ticketed"] for f in curr["grid"].get(cd, []) if f.get("ticketed"))
        for _, _, cd in day_specs
    ]

    fig = go.Figure()
    if wow_mode:
        fig.add_trace(go.Bar(name="Baseline", x=chart_x, y=prev_totals, marker_color="#94a3b8"))
        fig.add_trace(go.Bar(name="Current", x=chart_x, y=curr_totals, marker_color="#2563eb"))
        fig.update_layout(**PLOTLY_LAYOUT, title="Daily tickets (by day position)", barmode="group")
    else:
        fig.add_trace(go.Scatter(
            x=chart_x, y=prev_totals, name="Baseline",
            line=dict(color="#94a3b8", width=2, dash="dot"), marker=dict(size=5),
        ))
        fig.add_trace(go.Scatter(
            x=chart_x, y=curr_totals, name="Current",
            line=dict(color="#2563eb", width=2), marker=dict(size=6),
        ))
        fig.update_layout(**PLOTLY_LAYOUT, title="Daily tickets (overlapping dates)")
    st.plotly_chart(fig, use_container_width=True)

    compare_df, day_cols, grand_total = build_compare_table(prev["grid"], curr["grid"], day_specs)
    st.markdown(
        "**Ticket change per flight** — numbers are current minus baseline "
        "(e.g. +5 means 5 more tickets sold)."
    )
    show_delta_table(compare_df, day_cols)

    m1, m2, m3 = st.columns(3)
    m1.metric("Net change", f"{grand_total:+d}")
    m2.metric("Baseline tickets", f"{sum(prev_totals):,}")
    m3.metric("Current tickets", f"{sum(curr_totals):,}")


def page_booking_curve():
    db = load_db()
    if not db:
        st.info("No snapshots yet.")
        return

    st.markdown("### Booking curve")
    st.caption("How ticket sales for one flight built up across imports.")

    flights = set()
    for snap in db:
        for d in snap["col_dates"]:
            for f in snap["grid"].get(d, []):
                flights.add((f["route"], f["flt"]))

    if not flights:
        st.warning("No flights found in any snapshot.")
        return

    flight_list = sorted([flight_label(route, flt) for route, flt in flights])
    selected_flight = st.selectbox("Flight", flight_list, label_visibility="collapsed")
    flt_part, route = selected_flight.split(" · ", 1)
    flt = flt_part.split()[1]

    dep_dates = sorted(set(d for snap in db for d in snap["col_dates"]))
    dep_date = st.selectbox(
        "Departure date",
        dep_dates,
        format_func=fmt_day,
        label_visibility="collapsed",
    )

    timestamps, tickets, capacities = [], [], []
    for snap in db:
        entry = next((f for f in snap["grid"].get(dep_date, [])
                      if f["route"] == route and f["flt"] == flt), None)
        if entry and entry["ticketed"] is not None:
            timestamps.append(snap["pasted_at"])
            tickets.append(entry["ticketed"])
            capacities.append(entry.get("capacity", 0))

    if not timestamps:
        st.warning(f"No data for {selected_flight} on {dep_date}.")
        return

    df = pd.DataFrame({
        "Snapshot": pd.to_datetime(timestamps),
        "Tickets":  tickets,
        "Capacity": capacities
    }).sort_values("Snapshot")

    valid_caps = [c for c in df["Capacity"] if c > 0]
    capacity   = max(set(valid_caps), key=valid_caps.count) if valid_caps else None

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["Snapshot"], y=df["Tickets"],
        mode="lines+markers", name="Tickets",
        line=dict(color="#2563eb", width=2),
        marker=dict(size=6, color="#2563eb"),
        fill="tozeroy", fillcolor="rgba(37,99,235,0.08)",
    ))

    if capacity:
        fig.add_hline(y=capacity, line_dash="dash", line_color="#10b981",
                      annotation_text=f"CAP: {capacity}",
                      annotation_font_color="#10b981")
        df["Load%"] = (df["Tickets"] / capacity * 100).round(1)
        fig.add_trace(go.Scatter(
            x=df["Snapshot"], y=df["Load%"],
            mode="lines+markers", name="Load %",
            yaxis="y2",
            line=dict(color="#f59e0b", width=1.5, dash="dot"),
            marker=dict(size=4)
        ))
        fig.update_layout(
            yaxis2=dict(title="Load %", overlaying="y", side="right",
                        range=[0, 110], gridcolor="rgba(0,0,0,0)",
                        color="#f59e0b")
        )

    fig.update_layout(
        **PLOTLY_LAYOUT,
        title=f"{selected_flight} · {fmt_day(dep_date)}",
        xaxis_title="Imported at (PKT)",
        yaxis_title="Tickets sold",
    )
    st.plotly_chart(fig, use_container_width=True)

    if len(tickets) >= 2:
        delta = tickets[-1] - tickets[-2]
        total_g = tickets[-1] - tickets[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Latest tickets", f"{tickets[-1]:,}")
        c2.metric("Since last import", f"{delta:+d}")
        c3.metric("Since first import", f"{total_g:+d}")


def page_history():
    db = load_db()
    if not db:
        st.info("No snapshots saved yet.")
        return

    st.markdown("### History")
    history = []
    for i, rec in enumerate(reversed(db)):
        dates = rec["col_dates"]
        total_tix = sum(
            f["ticketed"] for d in dates for f in rec["grid"].get(d, []) if f.get("ticketed")
        )
        total_cap = sum(
            f["capacity"] for d in dates for f in rec["grid"].get(d, []) if f.get("capacity")
        )
        lf = f"{total_tix / total_cap * 100:.1f}%" if total_cap else "—"
        history.append({
            "#": len(db) - i,
            "Name": rec["snapshot_name"],
            "Imported": rec["pasted_at"],
            "Dates": f"{fmt_day(dates[0])} → {fmt_day(dates[-1])}",
            "Rows": sum(len(v) for v in rec["grid"].values()),
            "Tickets": total_tix,
            "Load %": lf,
        })
    st.dataframe(pd.DataFrame(history), use_container_width=True, hide_index=True)

    st.markdown("### Manage data")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Delete latest snapshot", use_container_width=True):
            st.session_state["confirm_delete"] = True
    with c2:
        if st.button("Delete all snapshots", use_container_width=True):
            st.session_state["confirm_clear"] = True

    if st.session_state.get("confirm_delete"):
        st.warning("Delete the most recent snapshot?")
        if st.button("Yes, delete latest"):
            db.pop()
            save_db(db)
            st.session_state["confirm_delete"] = False
            st.rerun()

    if st.session_state.get("confirm_clear"):
        st.error("Delete every snapshot? This cannot be undone.")
        if st.button("Yes, delete all"):
            save_db([])
            st.session_state["confirm_clear"] = False
            st.rerun()


def page_overview():
    db = load_db()
    if not db:
        st.info("Import a snapshot to see your dashboard.")
        return

    total_tix = 0
    total_routes = set()
    for snap in db:
        for d in snap.get("col_dates", []):
            for f in snap["grid"].get(d, []):
                if f.get("ticketed"):
                    total_tix += f["ticketed"]
                total_routes.add((f["route"], f["flt"]))

    c1, c2, c3 = st.columns(3)
    c1.metric("Snapshots", len(db))
    c2.metric("Tickets (all imports)", f"{total_tix:,}")
    c3.metric("Unique flights", len(total_routes))

    snap_names = [
        s["snapshot_name"][:48] + "…" if len(s["snapshot_name"]) > 48 else s["snapshot_name"]
        for s in db
    ]
    snap_tickets = [
        sum(f["ticketed"] for d in s["col_dates"] for f in s["grid"].get(d, []) if f.get("ticketed"))
        for s in db
    ]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=list(range(1, len(db) + 1)),
        y=snap_tickets,
        marker_color="#2563eb",
        hovertext=snap_names,
        hovertemplate="%{hovertext}<br>%{y:,} tickets<extra></extra>",
    ))
    fig.update_layout(
        **PLOTLY_LAYOUT,
        title="Total tickets per import",
        xaxis_title="Import #",
        yaxis_title="Tickets",
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"Latest: **{db[-1]['snapshot_name']}** · imported {db[-1]['pasted_at']} PKT")


# ============================================================================
# Main
# ============================================================================
st.set_page_config(
    page_title="Airblue Flight Analytics",
    page_icon="✈",
    layout="wide",
    initial_sidebar_state="expanded",
)
apply_aero_theme()

with st.sidebar:
    st.markdown("### Airblue Analytics")
    st.caption("Flight Manager snapshots")
    db_sidebar = load_db()
    if db_sidebar:
        st.metric("Snapshots", len(db_sidebar))
        st.caption("Latest import")
        st.write(db_sidebar[-1].get("snapshot_name", "—"))
        st.caption(db_sidebar[-1].get("pasted_at", ""))
    else:
        st.info("No data yet. Use **Import** to add a snapshot.")

st.markdown("""
<div class="aero-header">
    <div class="aero-wordmark">Airblue <span>Analytics</span></div>
</div>
""", unsafe_allow_html=True)

tab_overview, tab_import, tab_snapshot, tab_compare, tab_curve, tab_history = st.tabs([
    "Overview",
    "Import",
    "Snapshot",
    "Compare",
    "Booking curve",
    "History",
])
with tab_overview:
    page_overview()
with tab_import:
    page_import()
with tab_snapshot:
    page_snapshot()
with tab_compare:
    page_compare()
with tab_curve:
    page_booking_curve()
with tab_history:
    page_history()
