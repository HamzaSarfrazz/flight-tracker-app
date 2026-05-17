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
# AERO-X 2080 Theme
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

    /* Base styles */
    html, body, [class*="css"], .stApp {
        background: var(--bg) !important;
        color: var(--text) !important;
        font-family: 'Inter', sans-serif !important;
    }

    .main .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
        position: relative;
        z-index: 1;
    }

    /* Enhanced glassmorphism */
    .glassmorphism {
        background: rgba(255, 255, 255, 0.7) !important;
        backdrop-filter: blur(10px) !important;
        border: 1px solid rgba(255, 255, 255, 0.2) !important;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.08) !important;
    }

    /* Enhanced sidebar */
    [data-testid="stSidebar"] {
        background: var(--sidebar) !important;
        border-right: 1px solid var(--border);
        background: rgba(255, 255, 255, 0.8) !important;
        backdrop-filter: blur(5px) !important;
    }

    [data-testid="stSidebar"] * {
        font-family: 'Inter', sans-serif !important;
    }

    /* Enhanced header */
    .aero-header {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 28px;
        padding-bottom: 20px;
        border-bottom: 1px solid var(--border);
        background: rgba(255, 255, 255, 0.6) !important;
        backdrop-filter: blur(5px) !important;
    }

    .aero-wordmark {
        font-size: 2rem;
        font-weight: 800;
        letter-spacing: -0.05em;
        color: var(--text);
        text-shadow: 0 2px 4px rgba(0,0,0,0.05);
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
        transition: all 0.3s ease;
        box-shadow: 0 2px 8px rgba(37,99,235,0.15);
    }

    .aero-badge:hover {
        transform: scale(1.05);
        box-shadow: 0 4px 12px rgba(37,99,235,0.25);
    }

    .aero-label {
        margin-bottom: 8px;
        color: var(--muted);
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
    }

    /* Enhanced metric containers */
    [data-testid="metric-container"] {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 22px;
        padding: 22px;
        box-shadow:
            0 1px 2px rgba(0,0,0,0.04),
            0 8px 24px rgba(15,23,42,0.04);
        transition: all 0.3s ease;
        position: relative;
        overflow: hidden;
    }

    [data-testid="metric-container"]:hover {
        transform: translateY(-2px);
        box-shadow:
            0 4px 16px rgba(0,0,0,0.08),
            0 12px 32px rgba(15,23,42,0.12);
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
        transition: all 0.3s ease;
    }

    [data-testid="stMetricValue"]:hover {
        letter-spacing: -0.02em;
    }

    /* Enhanced buttons */
    .stButton > button {
        width: 100%;
        border: none !important;
        background: var(--primary) !important;
        color: white !important;
        border-radius: 14px !important;
        padding: 0.75rem 1rem !important;
        font-size: 0.85rem !important;
        font-weight: 700 !important;
        transition: all 0.3s ease;
        position: relative;
        overflow: hidden;
        box-shadow: 0 4px 12px rgba(37,99,235,0.2);
    }

    .stButton > button:hover {
        background: var(--primary-hover) !important;
        transform: translateY(-2px) scale(1.02);
        box-shadow: 0 8px 20px rgba(37,99,235,0.3);
    }

    .stButton > button:active {
        transform: translateY(0) scale(0.98);
    }

    .stButton > button::before {
        content: '';
        position: absolute;
        top: 0;
        left: -100%;
        width: 100%;
        height: 100%;
        background: linear-gradient(
            90deg,
            transparent,
            rgba(255,255,255,0.2),
            transparent
        );
        transition: all 0.6s;
    }

    .stButton > button:hover::before {
        left: 100%;
    }

    /* Enhanced inputs */
    .stTextArea textarea,
    .stSelectbox select,
    .stTextInput input {
        background: rgba(255, 255, 255, 0.8) !important;
        border: 1px solid var(--border) !important;
        border-radius: 14px !important;
        color: var(--text) !important;
        font-size: 0.9rem !important;
        backdrop-filter: blur(5px) !important;
        transition: all 0.3s ease;
    }

    .stTextArea textarea:focus,
    .stSelectbox select:focus,
    .stTextInput input:focus {
        border-color: var(--primary) !important;
        box-shadow: 0 0 0 3px rgba(37,99,235,0.2) !important;
        background: white !important;
    }

    /* Enhanced tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background: transparent !important;
        border-bottom: none !important;
        padding: 4px;
    }

    .stTabs [data-baseweb="tab"] {
        background: rgba(255, 255, 255, 0.6) !important;
        border: 1px solid var(--border) !important;
        border-radius: 14px !important;
        padding: 10px 18px !important;
        color: var(--muted) !important;
        font-weight: 700 !important;
        transition: all 0.3s ease;
        position: relative;
        overflow: hidden;
    }

    .stTabs [data-baseweb="tab"]:hover {
        background: rgba(255, 255, 255, 0.8) !important;
        transform: translateY(-2px);
    }

    .stTabs [aria-selected="true"] {
        background: var(--primary-soft) !important;
        border-color: var(--primary) !important;
        color: var(--primary) !important;
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(37,99,235,0.15);
    }

    .stTabs [aria-selected="true"]::before {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        height: 3px;
        background: var(--primary);
        animation: slideUnder 0.3s ease-out;
    }

    @keyframes slideUnder {
        from { width: 0; }
        to { width: 100%; }
    }

    /* Enhanced tables */
    table {
        border-collapse: collapse !important;
        width: 100% !important;
        background: white !important;
        border-radius: 18px !important;
        overflow: hidden !important;
        border: 1px solid var(--border) !important;
        box-shadow: 0 4px 16px rgba(0,0,0,0.05);
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
        transition: background-color 0.2s ease;
    }

    /* Enhanced terminal blocks */
    .terminal-block {
        background: rgba(255, 255, 255, 0.8) !important;
        border: 1px solid var(--border) !important;
        border-radius: 18px;
        padding: 22px;
        line-height: 1.8;
        font-size: 0.9rem;
        box-shadow:
            0 1px 2px rgba(0,0,0,0.04),
            0 8px 24px rgba(15,23,42,0.04);
        backdrop-filter: blur(5px) !important;
        transition: all 0.3s ease;
    }

    .terminal-block:hover {
        transform: translateY(-2px);
        box-shadow:
            0 4px 12px rgba(0,0,0,0.08),
            0 12px 28px rgba(15,23,42,0.12);
    }

    .terminal-block .ts {
        color: var(--muted);
        font-weight: 600;
    }

    .terminal-block .ok {
        color: var(--green);
        font-weight: 700;
    }

    .terminal-block .hi {
        color: var(--primary);
        font-weight: 700;
    }

    .terminal-block .wa {
        color: var(--amber);
        font-weight: 700;
    }

    /* Enhanced pills */
    .pill {
        display: inline-block;
        padding: 5px 12px;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 700;
        transition: all 0.2s ease;
    }

    .pill-green {
        background: var(--green-soft);
        color: #166534;
    }

    .pill-red {
        background: var(--red-soft);
        color: #991b1b;
    }

    .pill-amber {
        background: var(--amber-soft);
        color: #92400e;
    }

    .pill-cyan {
        background: var(--primary-soft);
        color: var(--primary);
    }

    hr {
        border-color: var(--border) !important;
        opacity: 0.5;
    }

    /* Enhanced footer */
    .footer-bar {
        position: fixed;
        bottom: 0;
        left: 0;
        width: 100%;
        background: rgba(255,255,255,0.8) !important;
        backdrop-filter: blur(10px) !important;
        border-top: 1px solid var(--border) !important;
        padding: 8px 20px;
        display: flex;
        justify-content: space-between;
        font-size: 0.7rem;
        color: var(--muted);
        z-index: 999;
        backdrop-filter: blur(10px) !important;
        box-shadow: 0 -4px 16px rgba(0,0,0,0.05);
    }

    /* Flying BOD and BOE elements */
    .flying-elements {
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        pointer-events: none;
        z-index: 998;
        overflow: hidden;
    }

    .flying-element {
        position: absolute;
        width: 60px;
        height: 60px;
        opacity: 0.7;
        filter: drop-shadow(0 4px 8px rgba(0,0,0,0.2));
        animation: fly linear infinite;
    }

    .flying-element.bod {
        background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="%232563eb"><path d="M12 2l-2 6h-4l6 4-2 6 6-4 6 4-2-6 6-4h-4l-2-6zm-2 14.5v-3h-3v-2h3v-3h2v3h3v2h-3v3h-2z"/></svg>');
        background-size: contain;
        background-repeat: no-repeat;
    }

    .flying-element.boe {
        background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="%23ef4444"><path d="M12 2l-2 6h-4l6 4-2 6 6-4 6 4-2-6 6-4h-4l-2-6zm-2 14.5v-3h-3v-2h3v-3h2v3h3v2h-3v3h-2z"/></svg>');
        background-size: contain;
        background-repeat: no-repeat;
    }

    /* Flight paths */
    @keyframes fly {
        0% {
            transform: translateX(-100px) translateY(-100px) rotate(0deg) scale(0.5);
            opacity: 0;
        }
        10% {
            opacity: 0.7;
        }
        90% {
            opacity: 0.7;
        }
        100% {
            transform: translateX(140vw) translateY(80vh) rotate(360deg) scale(1.2);
            opacity: 0;
        }
    }

    /* Different flight patterns */
    .flying-element:nth-child(1) {
        animation-duration: 15s;
        animation-delay: 0s;
        animation-timing-function: ease-in-out;
    }

    .flying-element:nth-child(2) {
        animation-duration: 12s;
        animation-delay: 3s;
        animation-timing-function: ease-in-out;
        transform: scale(0.8) rotate(15deg);
    }

    .flying-element:nth-child(3) {
        animation-duration: 18s;
        animation-delay: 6s;
        animation-timing-function: ease-in-out;
        transform: scale(0.6) rotate(-10deg);
    }

    .flying-element:nth-child(4) {
        animation-duration: 20s;
        animation-delay: 9s;
        animation-timing-function: ease-in-out;
        transform: scale(0.7) rotate(5deg);
    }

    /* Responsive adjustments */
    @media (max-width: 768px) {
        .flying-element {
            width: 40px;
            height: 40px;
        }

        .flying-element.bod {
            background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="%232563eb"><path d="M12 2l-2 6h-4l6 4-2 6 6-4 6 4-2-6 6-4h-4l-2-6zm-2 14.5v-3h-3v-2h3v-3h2v3h3v2h-3v3h-2z"/></svg>');
        }

        .flying-element.boe {
            background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="%23ef4444"><path d="M12 2l-2 6h-4l6 4-2 6 6-4 6 4-2-6 6-4h-4l-2-6zm-2 14.5v-3h-3v-2h3v-3h2v3h3v2h-3v3h-2z"/></svg>');
        }
    }

    </style>

    <!-- Flying elements container -->
    <div class="flying-elements">
        <div class="flying-element bod"></div>
        <div class="flying-element boe"></div>
        <div class="flying-element bod"></div>
        <div class="flying-element boe"></div>
    </div>

    <div class="footer-bar">
        <span>AirBlue_X // AIRBLUE ANALYTICS ENGINE</span>
        <span>Asia/Karachi • PKT • HAMZA 6.0</span>
    </div>

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

# ============================================================================
# UI PAGES
# ============================================================================

def page_import():
    st.markdown('<div class="aero-label">// Data Link Initiate</div>', unsafe_allow_html=True)
    st.markdown("**Paste the full Flight Manager page output** into the buffer below and click SYNC.")

    raw = st.text_area(
        "DATA_STREAM_BUFFER",
        height=300,
        placeholder="Paste raw flight data here...",
        label_visibility="collapsed"
    )

    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        sync = st.button("⚡ SYNC_DATA_STREAM", type="primary", use_container_width=True)

    if sync:
        if not raw.strip():
            st.error("EMPTY_BUFFER — no data to process.")
            return

        prog  = st.progress(0, text="Initialising parser...")
        dummy = st.empty()

        def update_prog(p):
            prog.progress(min(1.0, p), text=f"Parsing... {int(p*100)}%")

        with st.spinner(""):
            grid, col_dates, metadata = parse_grid(raw, progress_callback=update_prog)

        prog.empty(); dummy.empty()

        if not col_dates:
            st.error("PARSE_FAIL — no date headers detected in stream.")
            return

        total = sum(len(v) for v in grid.values())
        if total == 0:
            st.error("PARSE_FAIL — no flight entries found.")
            return

        pkt        = datetime.now(ZoneInfo("Asia/Karachi"))
        timestamp  = pkt.strftime("%Y-%m-%d %H:%M")
        date_range = f"{col_dates[0]}→{col_dates[-1]}"
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

        st.markdown(f"""
        <div class="terminal-block">
            <span class="ts">[{ts_now()}]</span> <span class="ok">STREAM_SYNC_OK</span><br>
            <span class="ts">SNAPSHOT_ID  :</span> <span class="hi">{snapshot_name}</span><br>
            <span class="ts">DATE_RANGE   :</span> {date_range}<br>
            <span class="ts">ENTRIES      :</span> <span class="hi">{total}</span> flight-day records<br>
            <span class="ts">TYPE         :</span> {metadata.get('type','—')}
        </div>
        """, unsafe_allow_html=True)
        st.rerun()


def page_snapshot():
    db = load_db()
    if not db:
        st.info("No snapshots. Import data first.")
        return

    st.markdown('<div class="aero-label">// Snapshot Viewer</div>', unsafe_allow_html=True)
    opts        = [f"[{i}] {r['snapshot_name']}" for i, r in enumerate(db)]
    sel_idx     = st.selectbox("SELECT_SNAPSHOT", range(len(db)),
                               format_func=lambda i: opts[i], index=len(db)-1,
                               label_visibility="collapsed")
    snap        = db[sel_idx]
    grid        = snap["grid"]
    dates       = snap["col_dates"]

    total_tix = sum(f["ticketed"] for d in dates for f in grid[d] if f.get("ticketed"))
    total_cap = sum(f["capacity"] for d in dates for f in grid[d] if f.get("capacity"))
    lf        = (total_tix / total_cap * 100) if total_cap else 0
    total_rev = sum(f["revenue_k"] for d in dates for f in grid[d] if f.get("revenue_k"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("DATE_RANGE",   f"{dates[0][5:]} → {dates[-1][5:]}")
    c2.metric("TICKETS_SOLD", f"{total_tix:,}")
    c3.metric("LOAD_FACTOR",  f"{lf:.1f}%")
    c4.metric("REVENUE_K",    f"{total_rev:,}k")

    st.caption(f"Imported at {snap['pasted_at']} | {snap['snapshot_name']}")
    st.divider()

    # Build table
    flight_keys = []
    for d in dates:
        for f in grid[d]:
            key = (f["route"], f["flt"])
            if key not in flight_keys:
                flight_keys.append(key)

    rows = []
    for route, flt in flight_keys:
        row = {"FLIGHT": f"PA {flt} {route}"}
        for d in dates:
            entry = next((f for f in grid[d] if f["route"]==route and f["flt"]==flt), None)
            if not entry or entry["ticketed"] is None:
                row[d[5:]] = "—"
            elif entry["departed"]:
                row[d[5:]] = "✈ DEP"
            else:
                cap = entry["capacity"] or 1
                pct = entry["ticketed"] / cap * 100
                pill = "green" if pct >= 91 else "amber" if pct >= 76 else "red"
                row[d[5:]] = f'<span class="pill pill-{pill}">{entry["ticketed"]}/{cap} {pct:.0f}%</span>'
        rows.append(row)

    df = pd.DataFrame(rows)
    st.markdown(
        '<div style="overflow-x:auto">' + df.to_html(escape=False, index=False) + '</div>',
        unsafe_allow_html=True
    )

    # Daily totals bar chart
    st.divider()
    st.markdown('<div class="aero-label">// Daily Totals</div>', unsafe_allow_html=True)
    day_totals  = [sum(f["ticketed"] for f in grid[d] if f.get("ticketed")) for d in dates]
    day_caps    = [sum(f["capacity"] for f in grid[d] if f.get("capacity")) for d in dates]
    short_dates = [d[5:] for d in dates]
    lfs         = [t/c*100 if c else 0 for t, c in zip(day_totals, day_caps)]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=short_dates, y=day_totals, name="Tickets",
        marker_color="#00e5ff", marker_line_width=0,
        opacity=0.85
    ))
    fig.add_trace(go.Scatter(
        x=short_dates, y=lfs, name="Load %",
        yaxis="y2", mode="lines+markers",
        line=dict(color="#f59e0b", width=2),
        marker=dict(size=5, color="#f59e0b")
    ))
    fig.update_layout(
        **PLOTLY_LAYOUT,
        title="Daily Tickets & Load Factor",
        yaxis2=dict(title="Load %", overlaying="y", side="right",
                    range=[0, 110], gridcolor="rgba(0,0,0,0)"),
        barmode="group"
    )
    st.plotly_chart(fig, use_container_width=True)


def page_compare():
    db = load_db()

    if len(db) < 2:
        st.info("Need at least 2 snapshots to compare.")
        return

    st.markdown(
        '<div class="aero-label">// Comparison Matrix</div>',
        unsafe_allow_html=True
    )

    opts = [f"[{i}] {r['snapshot_name']}" for i, r in enumerate(db)]

    col1, col2 = st.columns(2)

    with col1:
        st.markdown(
            '<div class="aero-label">BASELINE_SNAPSHOT</div>',
            unsafe_allow_html=True
        )

        idx_a = st.selectbox(
            "A",
            range(len(db)),
            format_func=lambda i: opts[i],
            index=max(0, len(db)-2),
            label_visibility="collapsed"
        )

    with col2:
        st.markdown(
            '<div class="aero-label">CURRENT_SNAPSHOT</div>',
            unsafe_allow_html=True
        )

        idx_b = st.selectbox(
            "B",
            range(len(db)),
            format_func=lambda i: opts[i],
            index=len(db)-1,
            label_visibility="collapsed"
        )

    if idx_a == idx_b:
        st.warning("SELECT_DISTINCT_SNAPSHOTS — choose two different snapshots.")
        return

    prev = db[idx_a]
    curr = db[idx_b]

    mode = st.radio(
        "ALIGN_MODE",
        ["By Date (overlapping)", "By Day Offset (WoW)"],
        horizontal=True
    )

    # =====================================================================
    # DATAFRAME STYLE
    # =====================================================================

    st.markdown("""
    <style>

    /* dataframe wrapper */
    [data-testid="stDataFrame"] {
        border: 1px solid #1e293b !important;
        border-radius: 20px !important;
        overflow: hidden !important;
        background: #0b1220 !important;
        box-shadow:
            0 4px 18px rgba(0,0,0,0.25),
            0 0 0 1px rgba(255,255,255,0.02);
    }

    /* toolbar */
    [data-testid="stDataFrameToolbar"] {
        background: #0f172a !important;
        border-bottom: 1px solid #1e293b !important;
    }

    /* header */
    [data-testid="stDataFrame"] thead tr th {
        background: #111827 !important;
        color: #94a3b8 !important;
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        border-bottom: 1px solid #1e293b !important;
        letter-spacing: 0.05em;
    }

    /* body cells */
    [data-testid="stDataFrame"] tbody tr td {
        background: #0b1220 !important;
        color: #e2e8f0 !important;
        border-bottom: 1px solid #162033 !important;
        font-size: 0.82rem !important;
        font-weight: 500 !important;
    }

    /* hover */
    [data-testid="stDataFrame"] tbody tr:hover td {
        background: #111827 !important;
    }

    /* scrollbar */
    ::-webkit-scrollbar {
        height: 10px;
        width: 10px;
    }

    ::-webkit-scrollbar-thumb {
        background: #334155;
        border-radius: 999px;
    }

    </style>
    """, unsafe_allow_html=True)

    # =====================================================================
    # OVERLAPPING DATE MODE
    # =====================================================================

    if mode.startswith("By Date"):

        overlap = sorted(
            set(prev["col_dates"]) &
            set(curr["col_dates"])
        )

        if not overlap:
            st.error(
                "No overlapping dates — use Day Offset mode for WoW comparison."
            )
            return

        prev_totals = [
            sum(
                f["ticketed"]
                for f in prev["grid"].get(d, [])
                if f.get("ticketed")
            )
            for d in overlap
        ]

        curr_totals = [
            sum(
                f["ticketed"]
                for f in curr["grid"].get(d, [])
                if f.get("ticketed")
            )
            for d in overlap
        ]

        fig = go.Figure()

        fig.add_trace(go.Scatter(
            x=overlap,
            y=prev_totals,
            name="Baseline",
            line=dict(
                color="#64748b",
                width=2,
                dash="dot"
            ),
            marker=dict(size=4)
        ))

        fig.add_trace(go.Scatter(
            x=overlap,
            y=curr_totals,
            name="Current",
            line=dict(
                color="#00e5ff",
                width=3
            ),
            marker=dict(size=6)
        ))

        fig.update_layout(
            **PLOTLY_LAYOUT,
            title="Daily Tickets — Overlap Period"
        )

        st.plotly_chart(
            fig,
            use_container_width=True
        )

        # ================================================================
        # TABLE BUILD
        # ================================================================

        flight_keys = set()

        for d in overlap:
            for f in curr["grid"].get(d, []):
                flight_keys.add((f["route"], f["flt"]))

        rows = []
        grand_total = 0

        for route, flt in sorted(flight_keys):

            row = {
                "FLIGHT": f"PA {flt} {route}"
            }

            ftot = 0

            for d in overlap:

                pe = next(
                    (
                        f for f in prev["grid"].get(d, [])
                        if f["route"] == route and f["flt"] == flt
                    ),
                    None
                )

                ce = next(
                    (
                        f for f in curr["grid"].get(d, [])
                        if f["route"] == route and f["flt"] == flt
                    ),
                    None
                )

                if not ce or ce.get("departed"):
                    row[d[5:]] = "✈ DEP"
                    continue

                pt = pe["ticketed"] if pe and pe["ticketed"] else 0
                ct = ce["ticketed"] if ce and ce["ticketed"] else 0

                diff = ct - pt
                ftot += diff

                if diff > 0:
                    icon = "▲"
                elif diff < 0:
                    icon = "▼"
                else:
                    icon = "◆"

                row[d[5:]] = f"{icon} {pt} → {ct} ({diff:+d})"

            grand_total += ftot

            if ftot > 0:
                total_icon = "▲"
            elif ftot < 0:
                total_icon = "▼"
            else:
                total_icon = "◆"

            row["TOTAL_Δ"] = f"{total_icon} {ftot:+d}"

            rows.append(row)

        rows.append({
            "FLIGHT": "GRAND_TOTAL",
            **{d[5:]: "" for d in overlap},
            "TOTAL_Δ": f"{'▲' if grand_total > 0 else '▼'} {grand_total:+d}"
        })

        compare_df = pd.DataFrame(rows)

        def color_compare(val):

            if isinstance(val, str):

                if "▲" in val:
                    return (
                        "background-color: rgba(16,185,129,0.12);"
                        "color: #4ade80;"
                        "font-weight: 700;"
                    )

                elif "▼" in val:
                    return (
                        "background-color: rgba(239,68,68,0.12);"
                        "color: #f87171;"
                        "font-weight: 700;"
                    )

                elif "◆" in val:
                    return (
                        "background-color: rgba(59,130,246,0.10);"
                        "color: #38bdf8;"
                        "font-weight: 700;"
                    )

            return ""

        styled_df = compare_df.style.map(color_compare)

        st.dataframe(
            styled_df,
            use_container_width=True,
            hide_index=True,
            height=620
        )

        c1, c2, c3 = st.columns(3)

        c1.metric(
            "NET_TICKETS_SOLD",
            f"{grand_total:+d}"
        )

        c2.metric(
            "BASELINE_TOTAL",
            f"{sum(prev_totals):,}"
        )

        c3.metric(
            "CURRENT_TOTAL",
            f"{sum(curr_totals):,}"
        )

    # =====================================================================
    # WOW MODE
    # =====================================================================

    else:

        n_days = min(
            len(prev["col_dates"]),
            len(curr["col_dates"])
        )

        prev_dates = prev["col_dates"][:n_days]
        curr_dates = curr["col_dates"][:n_days]

        labels = []

        dow_names = [
            'MON', 'TUE', 'WED',
            'THU', 'FRI', 'SAT', 'SUN'
        ]

        for i in range(n_days):

            try:
                d = date.fromisoformat(curr_dates[i])
                dow = dow_names[d.weekday()]
                labels.append(f"D{i+1} {dow}")

            except:
                labels.append(f"D{i+1}")

        prev_totals = [
            sum(
                f["ticketed"]
                for f in prev["grid"].get(prev_dates[i], [])
                if f.get("ticketed")
            )
            for i in range(n_days)
        ]

        curr_totals = [
            sum(
                f["ticketed"]
                for f in curr["grid"].get(curr_dates[i], [])
                if f.get("ticketed")
            )
            for i in range(n_days)
        ]

        fig = go.Figure()

        fig.add_trace(go.Bar(
            name="Baseline",
            x=labels,
            y=prev_totals,
            marker_color="#475569",
            opacity=0.7
        ))

        fig.add_trace(go.Bar(
            name="Current",
            x=labels,
            y=curr_totals,
            marker_color="#00e5ff",
            opacity=0.9
        ))

        fig.update_layout(
            **PLOTLY_LAYOUT,
            title="WoW — Daily Tickets",
            barmode="group"
        )

        st.plotly_chart(
            fig,
            use_container_width=True
        )

        all_flights = set()

        for i in range(n_days):

            for f in prev["grid"].get(prev_dates[i], []):
                all_flights.add((f["route"], f["flt"]))

            for f in curr["grid"].get(curr_dates[i], []):
                all_flights.add((f["route"], f["flt"]))

        rows = []

        grand_total = 0

        for route, flt in sorted(all_flights):

            row = {
                "FLIGHT": f"PA {flt} {route}"
            }

            ftot = 0

            for i in range(n_days):

                pe = next(
                    (
                        f for f in prev["grid"].get(prev_dates[i], [])
                        if f["route"] == route and f["flt"] == flt
                    ),
                    None
                )

                ce = next(
                    (
                        f for f in curr["grid"].get(curr_dates[i], [])
                        if f["route"] == route and f["flt"] == flt
                    ),
                    None
                )

                if not pe and not ce:
                    row[labels[i]] = "—"
                    continue

                if ce and ce.get("departed"):
                    row[labels[i]] = "✈ DEP"
                    continue

                pt = pe["ticketed"] if pe and pe["ticketed"] else 0
                ct = ce["ticketed"] if ce and ce["ticketed"] else 0

                diff = ct - pt
                ftot += diff

                if diff > 0:
                    icon = "▲"
                elif diff < 0:
                    icon = "▼"
                else:
                    icon = "◆"

                row[labels[i]] = f"{icon} {pt} → {ct} ({diff:+d})"

            grand_total += ftot

            if ftot > 0:
                total_icon = "▲"
            elif ftot < 0:
                total_icon = "▼"
            else:
                total_icon = "◆"

            row["TOTAL_Δ"] = f"{total_icon} {ftot:+d}"

            rows.append(row)

        rows.append({
            "FLIGHT": "GRAND_TOTAL",
            **{l: "" for l in labels},
            "TOTAL_Δ": f"{'▲' if grand_total > 0 else '▼'} {grand_total:+d}"
        })

        compare_df = pd.DataFrame(rows)

        styled_df = compare_df.style.map(color_compare)

        st.dataframe(
            styled_df,
            use_container_width=True,
            hide_index=True,
            height=620
        )

        st.metric(
            "NET_TICKETS_WoW",
            f"{grand_total:+d}"
        )


def page_booking_curve():
    db = load_db()
    if not db:
        st.info("No snapshots yet.")
        return

    st.markdown('<div class="aero-label">// Booking Curve Analysis</div>', unsafe_allow_html=True)

    flights = set()
    for snap in db:
        for d in snap["col_dates"]:
            for f in snap["grid"].get(d, []):
                flights.add((f["route"], f["flt"]))

    if not flights:
        st.warning("No flights found in any snapshot.")
        return

    flight_list     = sorted([f"PA {flt} {route}" for route, flt in flights])
    selected_flight = st.selectbox("SELECT_FLIGHT", flight_list, label_visibility="collapsed")
    parts           = selected_flight.split()
    flt, route      = parts[1], parts[2]

    dep_dates = sorted(set(d for snap in db for d in snap["col_dates"]))
    dep_date  = st.selectbox("DEPARTURE_DATE", dep_dates, label_visibility="collapsed")

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
        line=dict(color="#00e5ff", width=2),
        marker=dict(size=6, color="#00e5ff",
                    line=dict(width=1, color="#04080f")),
        fill="tozeroy", fillcolor="rgba(0,229,255,0.05)"
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
        title=f"Booking Curve — {selected_flight} // {dep_date}",
        xaxis_title="Snapshot Time (PKT)",
        yaxis_title="Tickets Sold",
    )
    st.plotly_chart(fig, use_container_width=True)

    # Trend stats
    if len(tickets) >= 2:
        delta   = tickets[-1] - tickets[-2]
        total_g = tickets[-1] - tickets[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("LATEST_TICKETS",  f"{tickets[-1]:,}")
        c2.metric("LAST_SNAPSHOT_Δ", f"{delta:+d}")
        c3.metric("TOTAL_GROWTH",    f"{total_g:+d}")


def page_history():
    db = load_db()
    if not db:
        st.info("No history.")
        return

    st.markdown('<div class="aero-label">// Snapshot Registry</div>', unsafe_allow_html=True)

    history = []
    for i, rec in enumerate(reversed(db)):
        total_tix = sum(f["ticketed"] for d in rec["col_dates"] for f in rec["grid"].get(d, []) if f.get("ticketed"))
        total_cap = sum(f["capacity"] for d in rec["col_dates"] for f in rec["grid"].get(d, []) if f.get("capacity"))
        lf        = f"{total_tix/total_cap*100:.1f}%" if total_cap else "—"
        history.append({
            "IDX":           len(db)-i,
            "SNAPSHOT_NAME": rec["snapshot_name"],
            "IMPORTED_PKT":  rec["pasted_at"],
            "DATE_RANGE":    f"{rec['col_dates'][0]}→{rec['col_dates'][-1]}",
            "FLIGHTS":       sum(len(v) for v in rec["grid"].values()),
            "TICKETS":       f"{total_tix:,}",
            "LOAD_FACTOR":   lf,
        })
    st.dataframe(pd.DataFrame(history), use_container_width=True, hide_index=True)

    st.divider()
    st.markdown('<div class="aero-label">// Danger Zone</div>', unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑 DELETE_LATEST", use_container_width=True):
            st.session_state["confirm_delete"] = True
    with c2:
        if st.button("⚠️ PURGE_ALL_RECORDS", use_container_width=True):
            st.session_state["confirm_clear"] = True

    if st.session_state.get("confirm_delete"):
        st.warning("Confirm deletion of the most recent snapshot?")
        if st.button("✅ CONFIRM_DELETE"):
            db.pop(); save_db(db)
            st.session_state["confirm_delete"] = False
            st.rerun()

    if st.session_state.get("confirm_clear"):
        st.error("IRREVERSIBLE — delete ALL snapshots?")
        if st.button("✅ CONFIRM_PURGE"):
            save_db([])
            st.session_state["confirm_clear"] = False
            st.rerun()


def page_nexus():
    """System status / dashboard page."""
    db = load_db()

    total_snaps = len(db)
    total_tix   = 0
    total_routes = set()
    for snap in db:
        for d in snap.get("col_dates", []):
            for f in snap["grid"].get(d, []):
                if f.get("ticketed"):
                    total_tix += f["ticketed"]
                total_routes.add((f["route"], f["flt"]))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("SNAPSHOTS_LOADED", f"{total_snaps}")
    c2.metric("TOTAL_TICKETS",    f"{total_tix:,}")
    c3.metric("UNIQUE_FLIGHTS",   f"{len(total_routes)}")
    c4.metric("SYSTEM_STATUS",    "NOMINAL")

    st.markdown(f"""
    <div class="terminal-block" style="margin-top:24px">
        <span class="ts">[BOOT]</span>  AERO_X ENGINE ONLINE<br>
        <span class="ts">[INIT]</span>  SUPABASE_LINK........<span class="ok">ESTABLISHED</span><br>
        <span class="ts">[INIT]</span>  PARSER_CORE...........<span class="ok">READY</span><br>
        <span class="ts">[DATA]</span>  SNAPSHOTS_INDEXED.....<span class="hi">{total_snaps}</span><br>
        <span class="ts">[DATA]</span>  TOTAL_TICKETS.........<span class="hi">{total_tix:,}</span><br>
        <span class="ts">[DATA]</span>  UNIQUE_ROUTES.........<span class="hi">{len(total_routes)}</span><br>
        <span class="ts">[SYS ]</span>  TIMESTAMP.............<span class="wa">{ts_now()}</span><br>
        <span class="ts">[SYS ]</span>  TZ....................<span class="hi">Asia/Karachi (PKT, UTC+5)</span><br>
        <span class="ts">[SYS ]</span>  BUILD.................<span class="ok">AERO_X_v6.0_2080</span>
    </div>
    """, unsafe_allow_html=True)

    if db:
        # Trend chart across all snapshots
        st.divider()
        st.markdown('<div class="aero-label">// Total Tickets per Snapshot</div>', unsafe_allow_html=True)
        snap_names   = [s["snapshot_name"][:40] + "…" if len(s["snapshot_name"]) > 40
                        else s["snapshot_name"] for s in db]
        snap_tickets = [
            sum(f["ticketed"] for d in s["col_dates"] for f in s["grid"].get(d, []) if f.get("ticketed"))
            for s in db
        ]
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=list(range(len(db))), y=snap_tickets,
            marker_color="#00e5ff", marker_line_width=0, opacity=0.8,
            hovertext=snap_names, hovertemplate="%{hovertext}<br>%{y:,} tickets<extra></extra>"
        ))
        fig.update_layout(
            **PLOTLY_LAYOUT,
            title="Tickets per Snapshot (chronological)",
            xaxis_title="Snapshot Index",
            yaxis_title="Total Tickets",
        )
        st.plotly_chart(fig, use_container_width=True)


# ============================================================================
# Main
# ============================================================================
st.set_page_config(
    page_title="AERO_X // Airblue Analytics",
    page_icon="✈",
    layout="wide",
    initial_sidebar_state="expanded"
)
apply_aero_theme()

# Sidebar
with st.sidebar:
    st.markdown("""
    <div style="padding:16px 0 24px 0; border-bottom:1px solid #1a2640; margin-bottom:20px">
        <div style="font-family:'Syne',sans-serif; font-size:1.4rem; font-weight:800; color:#fff; letter-spacing:-0.03em">
            AERO_<span style="color:#00e5ff">X</span>
        </div>
        <div style="font-size:0.6rem; color:#4a6080; letter-spacing:0.2em; margin-top:4px">
            AIRBLUE ANALYTICS ENGINE
        </div>
    </div>
    """, unsafe_allow_html=True)

    db_sidebar = load_db()
    if db_sidebar:
        st.markdown(f"""
        <div style="margin-bottom:20px">
            <span class="status-dot"></span>
            <span style="font-size:0.65rem; color:#4a6080; letter-spacing:0.1em; margin-left:8px">
                {len(db_sidebar)} SNAPSHOT(S) LOADED
            </span>
        </div>
        """, unsafe_allow_html=True)
        st.markdown(f"""
        <div class="stat-card">
            <div class="aero-label">LATEST_SNAPSHOT</div>
            <div style="font-size:0.72rem; color:#c8d8e8; margin-top:4px; word-break:break-all">
                {db_sidebar[-1].get('snapshot_name','—')}
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown('<div style="font-size:0.72rem; color:#4a6080">NO_DATA — import first</div>',
                    unsafe_allow_html=True)

    st.markdown('<div class="aero-label" style="margin-top:20px">BUILD_INFO</div>', unsafe_allow_html=True)
    st.markdown('<div style="font-size:0.65rem; color:#2a3a50; line-height:1.8">v6.0 // 2080.6<br>ENCRYPT: AES-4096-Q<br>TZ: Asia/Karachi</div>',
                unsafe_allow_html=True)

# Header
st.markdown("""
<div class="aero-header">
    <div class="aero-wordmark">AERO_<span>X</span></div>
    <div class="aero-badge">Airblue Analytics Engine</div>
    <div class="aero-badge">BUILD 2080.6</div>
</div>
""", unsafe_allow_html=True)

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "◈ NEXUS", "⊕ IMPORT", "◉ SNAPSHOT", "⊗ COMPARE", "∿ BOOKING_CURVE", "⊘ HISTORY"
])
with tab1: page_nexus()
with tab2: page_import()
with tab3: page_snapshot()
with tab4: page_compare()
with tab5: page_booking_curve()
with tab6: page_history()
