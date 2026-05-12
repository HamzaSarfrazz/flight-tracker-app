import streamlit as st
import json
import re
import os
from datetime import date, datetime, timedelta
from typing import List, Dict, Tuple
import pandas as pd
import plotly.graph_objects as go

# ============================================================================
# Constants
# ============================================================================
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flight_data.json")

MONTH_MAP = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
}

CONFIG_CAP = {
    'B': 330, 'F': 218, 'G': 218, 'M': 144, 'NXA': 235, 'O': 150,
    'P': 174, 'Q': 220, 'R': 174, 'S': 342, 'T': 180, 'X': 212, 'Z': 180,
    'K': 180, 'V': 180, 'U': 180, 'W': 180, 'L': 180, 'H': 180
}

# Regex patterns
DATE_HDR_RE = re.compile(r'(MON|TUE|WED|THU|FRI|SAT|SUN)\s*-\s*(\d+)([A-Z]{3})')
ROUTE_RE = re.compile(r'([A-Z]{3}-[A-Z]{3})\s+PA\s+\d+')
FLT_RE = re.compile(r'^(\d{3})$')
REV_RE = re.compile(r'^([\d,]+)k$')
Y4_RE = re.compile(r'Y:(\d+)-(\d+)-(\d+)-(\d+)')
Y3_RE = re.compile(r'Y:(\d+)-(\d+)-(\d+)$')
CONFIG_RE = re.compile(r'^([A-Z]{1,3})(?:\s|$)')
FLIGHT_SEL_RE = re.compile(r'Flight Selections:')
DOMESTIC_HEADER_RE = re.compile(r'Domestic Flights', re.IGNORECASE)
INTERNATIONAL_HEADER_RE = re.compile(r'International Flights', re.IGNORECASE)

# ============================================================================
# Helpers
# ============================================================================
def get_correct_year(month: int, day: int) -> int:
    today = date.today()
    candidate = date(today.year, month, day)
    if candidate < today - timedelta(days=30):
        return today.year + 1
    return today.year

def parse_date_headers(lines: List[str]) -> List[str]:
    dates, seen = [], set()
    blob = " ".join(lines[:80])
    for match in DATE_HDR_RE.finditer(blob):
        day = int(match.group(2))
        month = MONTH_MAP.get(match.group(3), 0)
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
                if token_line and not any(h in token_line for h in ["Display Options:", "Date Range:", "Domestic Flights", "International Flights"]):
                    parts = token_line.split()
                    flight_sel_tokens.extend(parts)
                else:
                    if any(h in token_line for h in ["Display Options:", "Date Range:", "Domestic Flights", "International Flights"]):
                        break
                j += 1
            break

    # Extract flight identifier and location(s) from selection tokens
    if "PA" in flight_sel_tokens:
        idx = flight_sel_tokens.index("PA")
        flight_num = None
        location_tokens = []
        i = idx + 1
        # Find flight number (3 digits) or "[All]"
        while i < len(flight_sel_tokens):
            token = flight_sel_tokens[i]
            if token.isdigit() and len(token) == 3:
                flight_num = token
                i += 1
                break
            if token == "[All]":
                flight_num = "All"
                i += 1
                break
            i += 1
        # Now collect location tokens until we hit a bracket containing "config"
        if flight_num:
            in_bracket = False
            bracket_text = ""
            while i < len(flight_sel_tokens):
                token = flight_sel_tokens[i]
                # Handle multi-token brackets (e.g., [All Configs] split into [All and Configs])
                if token.startswith('[') and not token.endswith(']'):
                    in_bracket = True
                    bracket_text = token
                    i += 1
                    continue
                if in_bracket:
                    bracket_text += " " + token
                    if token.endswith(']'):
                        # Finished bracket
                        if 'config' in bracket_text.lower():
                            break  # found [All Configs] or similar
                        else:
                            # It's a location bracket like [All Locations]
                            loc_clean = bracket_text.strip('[]')
                            location_tokens.append(loc_clean)
                            in_bracket = False
                            bracket_text = ""
                    i += 1
                    continue

                # Not in a bracket, single token
                if token.startswith('[') and token.endswith(']'):
                    if 'config' in token.lower():
                        break
                    else:
                        location_tokens.append(token.strip('[]'))
                else:
                    # Regular token (airport code, comma, etc.)
                    location_tokens.append(token)
                i += 1

            # Build location string: remove trailing commas, join with ", "
            loc_parts = []
            for tok in location_tokens:
                # Remove trailing comma from individual tokens like "AUH,"
                tok = tok.rstrip(',')
                if tok:  # ignore empty strings
                    loc_parts.append(tok)
            if loc_parts:
                location = ", ".join(loc_parts)
                metadata["short_code"] = f"[{flight_num}][{location}]"

    # Flight type
    for line in lines:
        if DOMESTIC_HEADER_RE.search(line):
            metadata["type"] = "Domestic"
        elif INTERNATIONAL_HEADER_RE.search(line):
            metadata["type"] = "International"

    # Build name
    if metadata["short_code"] != "Unknown":
        metadata["name"] = f"{metadata['short_code']} - {metadata['type']}"
    else:
        metadata["name"] = metadata["type"]
    return metadata

# ============================================================================
# Parser – robust column alignment (unchanged)
# ============================================================================
def parse_grid(raw_text: str, progress_callback=None) -> Tuple[Dict[str, List[Dict]], List[str], Dict[str, str]]:
    lines = [l.strip() for l in raw_text.splitlines()]
    col_dates = parse_date_headers(lines)
    if not col_dates:
        return {}, [], {}

    metadata = extract_snapshot_metadata(lines)
    n_cols = len(col_dates)
    grid = {d: [] for d in col_dates}
    idx = 0
    total_lines = len(lines)

    def _parse_cell_data(clines):
        revenue_k = 0
        ticketed = 0
        reserved = 0
        capacity = 0
        departed = False
        for cline in clines:
            rev_match = REV_RE.match(cline)
            if rev_match and revenue_k == 0:
                revenue_k = int(rev_match.group(1).replace(",", ""))
                continue
            y4_match = Y4_RE.search(cline)
            if y4_match and ticketed == 0:
                ticketed = int(y4_match.group(1))
                reserved = int(y4_match.group(2))
                capacity = int(y4_match.group(3))
                departed = False
                continue
            y3_match = Y3_RE.search(cline)
            if y3_match and ticketed == 0:
                ticketed = int(y3_match.group(1))
                reserved = int(y3_match.group(2))
                capacity = 0
                departed = True
                continue
            if "XBAG" in cline:
                continue
            cfg_match = CONFIG_RE.match(cline)
            if cfg_match and capacity == 0 and not departed:
                code = cfg_match.group(1)
                if code in CONFIG_CAP:
                    capacity = CONFIG_CAP[code]
        return {
            "ticketed": ticketed if ticketed != 0 else None,
            "reserved": reserved,
            "capacity": capacity,
            "revenue_k": revenue_k,
            "departed": departed,
        }

    while idx < total_lines:
        line = lines[idx]
        route_match = ROUTE_RE.search(line)
        if route_match:
            current_route = route_match.group(1)
            idx += 1
            # Collect block until next route or totals
            block = []
            while idx < total_lines:
                l = lines[idx]
                if ROUTE_RE.search(l) or l.startswith("Totals:") or l.startswith("Grand Totals:"):
                    break
                if l:
                    block.append(l)
                idx += 1
            # Find the flight number (first 3-digit)
            flight_num = None
            for bl in block:
                if FLT_RE.match(bl):
                    flight_num = bl
                    break
            if not flight_num:
                continue
            # Split block into columns using markers: n/a or the flight number
            markers = []
            for i, bl in enumerate(block):
                if bl.lower() == "n/a" or (FLT_RE.match(bl) and bl == flight_num):
                    markers.append(i)
            col_entries = []
            for i in range(len(markers)):
                start = markers[i]
                end = markers[i+1] if i+1 < len(markers) else len(block)
                cell_lines = block[start+1:end]
                if block[start].lower() == "n/a":
                    col_entries.append({
                        "route": current_route, "flt": flight_num,
                        "ticketed": None, "reserved": None, "capacity": None,
                        "revenue_k": 0, "departed": False,
                    })
                else:
                    parsed = _parse_cell_data(cell_lines)
                    col_entries.append({
                        "route": current_route, "flt": flight_num,
                        **parsed
                    })
            # Pad to n_cols
            while len(col_entries) < n_cols:
                col_entries.append({
                    "route": current_route, "flt": flight_num,
                    "ticketed": None, "reserved": None, "capacity": None,
                    "revenue_k": 0, "departed": False,
                })
            col_entries = col_entries[:n_cols]
            for col_pos, entry in enumerate(col_entries):
                grid[col_dates[col_pos]].append(entry)
            if progress_callback:
                progress_callback(idx / total_lines)
            continue
        idx += 1

    return grid, col_dates, metadata

# ============================================================================
# Storage
# ============================================================================
@st.cache_data(ttl=5)
def load_db() -> List[Dict]:
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        records = json.load(f)
    for rec in records:
        if "snapshot_name" not in rec:
            dates = rec.get("col_dates", [])
            date_part = f"{dates[0]}→{dates[-1]}" if dates else "unknown"
            meta_name = rec.get("metadata", {}).get("name", "Snapshot")
            rec["snapshot_name"] = f"{meta_name} {date_part} - {rec.get('pasted_at', 'unknown')}"
    return records

def save_db(records: List[Dict]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    st.cache_data.clear()

# ============================================================================
# UI Pages (unchanged)
# ============================================================================
def apply_custom_css():
    st.markdown("""
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,300;400;500;600;700&display=swap');
        html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
        div[data-testid="stMetricValue"] { font-size: 2rem; font-weight: 600; }
        .stButton > button { border-radius: 8px; font-weight: 500; }
        .stTabs [data-baseweb="tab"] { font-weight: 500; }
    </style>
    """, unsafe_allow_html=True)

def page_paste():
    st.subheader("📋 Import Flight Data")
    st.caption("Copy the entire Flight Manager page with your selected filters")
    raw = st.text_area("Paste data here", height=300)

    if st.button("Process & Save", type="primary"):
        if not raw.strip():
            st.error("No data pasted.")
            return

        progress_bar = st.progress(0, text="Parsing flight data...")
        status_text = st.empty()
        def update_progress(percent):
            progress_bar.progress(min(1.0, percent))
            status_text.text(f"Parsing... {int(percent*100)}%")

        with st.spinner("Parsing..."):
            grid, col_dates, metadata = parse_grid(raw, progress_callback=update_progress)
        progress_bar.empty()
        status_text.empty()

        if not col_dates:
            st.error("Could not find date headers.")
            return
        total = sum(len(v) for v in grid.values())
        if total == 0:
            st.error("No flights found.")
            return

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        date_range = f"{col_dates[0]}→{col_dates[-1]}"
        if metadata.get("short_code") and metadata["short_code"] != "Unknown":
            snapshot_name = f"{metadata['short_code']} {date_range} - {timestamp}"
        else:
            snapshot_name = f"{metadata['name']} {date_range} - {timestamp}"

        record = {
            "pasted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "snapshot_name": snapshot_name,
            "metadata": metadata,
            "col_dates": col_dates,
            "grid": grid,
        }
        db = load_db()
        db.append(record)
        save_db(db)
        st.success(f"✅ Saved as **{snapshot_name}**\n\n{col_dates[0]} → {col_dates[-1]} | {total} flight-day entries")
        st.rerun()

def page_snapshot():
    db = load_db()
    if not db:
        st.info("No data yet. Import your first snapshot.")
        return

    snapshot_options = [f"[{i}] {r['snapshot_name']}" for i, r in enumerate(db)]
    selected_idx = st.selectbox("Select snapshot to view", range(len(db)), format_func=lambda i: snapshot_options[i], index=len(db)-1)
    snap = db[selected_idx]
    grid = snap["grid"]
    dates = snap["col_dates"]

    st.subheader(f"Snapshot: {snap['snapshot_name']}")
    st.caption(f"Imported at {snap['pasted_at']}")

    total_tix = sum(f["ticketed"] for d in dates for f in grid[d] if f.get("ticketed"))
    total_cap = sum(f["capacity"] for d in dates for f in grid[d] if f.get("capacity"))
    load_factor = (total_tix / total_cap * 100) if total_cap else 0
    col1, col2, col3 = st.columns(3)
    col1.metric("Date Range", f"{dates[0]} → {dates[-1]}")
    col2.metric("Tickets Sold", f"{total_tix:,}")
    col3.metric("Load Factor", f"{load_factor:.1f}%")

    flight_keys = []
    for d in dates:
        for f in grid[d]:
            key = (f["route"], f["flt"])
            if key not in flight_keys:
                flight_keys.append(key)
    rows = []
    for route, flt in flight_keys:
        row = {"Flight": f"PA {flt} {route}"}
        for d in dates:
            entry = next((f for f in grid[d] if f["route"]==route and f["flt"]==flt), None)
            if not entry or entry["ticketed"] is None:
                row[d[5:]] = "—"
            elif entry["departed"]:
                row[d[5:]] = "✈️ Departed"
            else:
                cap = entry["capacity"] or 1
                pct = entry["ticketed"] / cap * 100
                color = "#ef4444" if pct < 76 else "#eab308" if pct < 91 else "#22c55e"
                row[d[5:]] = f"<span style='color:{color}; font-weight:500'>{entry['ticketed']}/{cap} ({pct:.0f}%)</span>"
        rows.append(row)
    df = pd.DataFrame(rows)
    st.markdown(df.to_html(escape=False, index=False), unsafe_allow_html=True)

def page_compare():
    db = load_db()
    if len(db) < 2:
        st.info("Need at least two snapshots to compare.")
        return
    options = [f"[{i}] {r['snapshot_name']} ({r['col_dates'][0]}→{r['col_dates'][-1]})" for i, r in enumerate(db)]
    col1, col2 = st.columns(2)
    with col1:
        idx_a = st.selectbox("Earlier snapshot", range(len(db)), format_func=lambda i: options[i], index=max(0, len(db)-2))
    with col2:
        idx_b = st.selectbox("Later snapshot", range(len(db)), format_func=lambda i: options[i], index=len(db)-1)
    if idx_a == idx_b:
        st.warning("Select two different snapshots.")
        return
    prev, curr = db[idx_a], db[idx_b]

    align_mode = st.radio("Alignment method", ["By Date (overlapping calendar days)", "By Day Offset (week‑over‑week)"])

    if align_mode.startswith("By Date"):
        overlap = sorted(set(prev["col_dates"]) & set(curr["col_dates"]))
        if not overlap:
            st.error("No overlapping dates. Switch to 'By Day Offset' for week‑over‑week comparison.")
            return
        st.subheader(f"Comparing: **{prev['snapshot_name']}** → **{curr['snapshot_name']}**")
        prev_totals = [sum(f["ticketed"] for f in prev["grid"].get(d,[]) if f.get("ticketed")) for d in overlap]
        curr_totals = [sum(f["ticketed"] for f in curr["grid"].get(d,[]) if f.get("ticketed")) for d in overlap]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=overlap, y=prev_totals, name=prev['snapshot_name'], line=dict(color="#94a3b8")))
        fig.add_trace(go.Scatter(x=overlap, y=curr_totals, name=curr['snapshot_name'], line=dict(color="#0066cc")))
        fig.update_layout(title="Daily Tickets Sold", template="plotly_white", height=400)
        st.plotly_chart(fig, use_container_width=True)

        flight_keys = set()
        for d in overlap:
            for f in curr["grid"].get(d, []):
                flight_keys.add((f["route"], f["flt"]))
        rows = []
        grand_total = 0
        for route, flt in flight_keys:
            row = {"Flight": f"PA {flt} {route}"}
            ftot = 0
            for d in overlap:
                pe = next((f for f in prev["grid"].get(d,[]) if f["route"]==route and f["flt"]==flt), None)
                ce = next((f for f in curr["grid"].get(d,[]) if f["route"]==route and f["flt"]==flt), None)
                if not ce or ce.get("departed"):
                    row[d[5:]] = "departed"
                    continue
                pt = pe["ticketed"] if pe and pe["ticketed"] else 0
                ct = ce["ticketed"] if ce and ce["ticketed"] else 0
                diff = ct - pt
                ftot += diff
                row[d[5:]] = f"{pt}→{ct} ({diff:+d})"
            grand_total += ftot
            row["Total Δ"] = f"{ftot:+d}"
            rows.append(row)
        rows.append({"Flight": "**GRAND TOTAL**", **{d[5:]: "" for d in overlap}, "Total Δ": f"{grand_total:+d}"})
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
        st.metric("Net Tickets Sold", f"{grand_total:+d}")

    else:  # By Day Offset
        n_days = min(len(prev["col_dates"]), len(curr["col_dates"]))
        if n_days == 0:
            st.error("One of the snapshots has no dates.")
            return
        st.subheader(f"Comparing: **{prev['snapshot_name']}** → **{curr['snapshot_name']}** (Day 1 → Day {n_days})")
        prev_dates = prev["col_dates"][:n_days]
        curr_dates = curr["col_dates"][:n_days]
        day_names = ['MON','TUE','WED','THU','FRI','SAT','SUN']
        labels = []
        for i in range(n_days):
            try:
                d = date.fromisoformat(curr_dates[i])
                dow = day_names[d.weekday()]
                labels.append(f"Day {i+1}\n{dow} {d.strftime('%d%b')}")
            except:
                labels.append(f"Day {i+1}")

        prev_totals = [sum(f["ticketed"] for f in prev["grid"].get(prev_dates[i],[]) if f.get("ticketed")) for i in range(n_days)]
        curr_totals = [sum(f["ticketed"] for f in curr["grid"].get(curr_dates[i],[]) if f.get("ticketed")) for i in range(n_days)]

        fig = go.Figure()
        fig.add_trace(go.Bar(name=prev['snapshot_name'], x=labels, y=prev_totals, marker_color="#94a3b8"))
        fig.add_trace(go.Bar(name=curr['snapshot_name'], x=labels, y=curr_totals, marker_color="#0066cc"))
        fig.update_layout(title="Daily Tickets Sold (Week‑over‑Week)", template="plotly_white", height=400, barmode='group')
        st.plotly_chart(fig, use_container_width=True)

        all_flights = set()
        for i in range(n_days):
            for f in prev["grid"].get(prev_dates[i], []):
                all_flights.add((f["route"], f["flt"]))
            for f in curr["grid"].get(curr_dates[i], []):
                all_flights.add((f["route"], f["flt"]))

        rows = []
        grand_total = 0
        for route, flt in sorted(all_flights):
            row = {"Flight": f"PA {flt} {route}"}
            ftot = 0
            for i in range(n_days):
                pe = next((f for f in prev["grid"].get(prev_dates[i],[]) if f["route"]==route and f["flt"]==flt), None)
                ce = next((f for f in curr["grid"].get(curr_dates[i],[]) if f["route"]==route and f["flt"]==flt), None)
                if not pe and not ce:
                    row[f"Day {i+1}"] = "—"
                    continue
                if ce and ce.get("departed"):
                    row[f"Day {i+1}"] = "✈️ Departed"
                    continue
                pt = pe["ticketed"] if pe and pe["ticketed"] else 0
                ct = ce["ticketed"] if ce and ce["ticketed"] else 0
                diff = ct - pt
                ftot += diff
                row[f"Day {i+1}"] = f"{pt}→{ct}\n({diff:+d})"
            grand_total += ftot
            row["Total Δ"] = f"{ftot:+d}"
            rows.append(row)
        rows.append({"Flight": "**GRAND TOTAL**", **{f"Day {i+1}": "" for i in range(n_days)}, "Total Δ": f"{grand_total:+d}"})
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
        st.metric("Net Tickets Sold", f"{grand_total:+d}")

def page_booking_curve():
    st.subheader("📈 Booking Curve")
    st.caption("See how ticket sales evolved over time for a specific flight and departure date.")
    db = load_db()
    if not db:
        st.info("No data yet. Import snapshots to see booking trends.")
        return

    flights = set()
    for snap in db:
        for d in snap["col_dates"]:
            for f in snap["grid"].get(d, []):
                flights.add((f["route"], f["flt"]))

    if not flights:
        st.warning("No flights found in any snapshot.")
        return

    flight_list = sorted([f"PA {flt} {route}" for route, flt in flights])
    selected_flight = st.selectbox("Select a flight", flight_list)
    parts = selected_flight.split()
    flt = parts[1]
    route = parts[2]

    dep_dates = sorted(set(d for snap in db for d in snap["col_dates"]))
    dep_date = st.selectbox("Departure date", dep_dates)

    timestamps = []
    tickets = []
    capacities = []
    for snap in db:
        snap_time = snap["pasted_at"]
        grid = snap["grid"].get(dep_date, [])
        entry = next((f for f in grid if f["route"] == route and f["flt"] == flt), None)
        if entry and entry["ticketed"] is not None:
            timestamps.append(snap_time)
            tickets.append(entry["ticketed"])
            capacities.append(entry.get("capacity", 0))

    if not timestamps:
        st.warning(f"No data found for {selected_flight} on {dep_date}.")
        return

    df = pd.DataFrame({
        "Snapshot": pd.to_datetime(timestamps),
        "Tickets": tickets,
        "Capacity": capacities
    }).sort_values("Snapshot")

    valid_caps = [c for c in df["Capacity"] if c > 0]
    capacity = max(set(valid_caps), key=valid_caps.count) if valid_caps else None

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["Snapshot"], y=df["Tickets"],
        mode='lines+markers',
        name='Tickets sold',
        line=dict(color="#0066cc", width=2),
        marker=dict(size=6)
    ))

    if capacity:
        fig.add_hline(
            y=capacity,
            line_dash="dash",
            line_color="green",
            annotation_text=f"Capacity: {capacity}",
            annotation_position="bottom right"
        )
        df["Load %"] = (df["Tickets"] / capacity * 100).round(1)
        fig.add_trace(go.Scatter(
            x=df["Snapshot"], y=df["Load %"],
            mode='lines+markers',
            name='Load %',
            yaxis='y2',
            line=dict(color="#f59e0b", width=1.5, dash='dot'),
            marker=dict(size=4)
        ))
        fig.update_layout(
            yaxis2=dict(
                title="Load Factor (%)",
                overlaying='y',
                side='right',
                range=[0, 105]
            )
        )

    fig.update_layout(
        title=f"Booking Curve – {selected_flight} on {dep_date}",
        xaxis_title="Snapshot time",
        yaxis_title="Tickets sold",
        template="plotly_white",
        height=450,
        hovermode="x unified"
    )
    st.plotly_chart(fig, use_container_width=True)

    st.caption("Each point represents one snapshot you imported. "
               "Missing days will appear as gaps in the timeline, but the trend line remains intact.")

def page_history():
    db = load_db()
    if not db:
        st.info("No history.")
        return
    history = []
    for i, rec in enumerate(reversed(db)):
        total_tix = sum(f["ticketed"] for d in rec["col_dates"] for f in rec["grid"].get(d,[]) if f.get("ticketed"))
        history.append({
            "#": len(db)-i,
            "Snapshot Name": rec['snapshot_name'],
            "Imported": rec["pasted_at"],
            "Date Range": f"{rec['col_dates'][0]}→{rec['col_dates'][-1]}",
            "Flights": sum(len(v) for v in rec["grid"].values()),
            "Tickets": f"{total_tix:,}"
        })
    st.dataframe(pd.DataFrame(history), use_container_width=True, hide_index=True)

    st.divider()
    delete_col1, delete_col2 = st.columns(2)
    with delete_col1:
        if st.button("🗑 Delete latest snapshot"):
            if db:
                st.session_state["confirm_delete"] = True
    with delete_col2:
        if st.button("⚠️ Clear entire history"):
            st.session_state["confirm_clear"] = True

    if st.session_state.get("confirm_delete"):
        st.warning("Are you sure you want to delete the latest snapshot?")
        if st.button("✅ Confirm delete", key="confirm_del_btn"):
            db.pop()
            save_db(db)
            st.session_state["confirm_delete"] = False
            st.rerun()
    if st.session_state.get("confirm_clear"):
        st.error("Are you sure you want to delete ALL snapshots? This cannot be undone.")
        if st.button("✅ Confirm clear", key="confirm_clear_btn"):
            save_db([])
            st.session_state["confirm_clear"] = False
            st.rerun()

# ============================================================================
# Main
# ============================================================================
st.set_page_config(page_title="Airblue Flight Tracker", page_icon="✈️", layout="wide")
apply_custom_css()
st.title("✈️ Airblue Flight Tracker")
st.caption("Enterprise-grade daily sales monitoring – snapshots are named by flight number and airport.")

db = load_db()
if db:
    latest_name = db[-1].get('snapshot_name', 'Unknown snapshot')
    st.success(f"✅ Loaded {len(db)} snapshot(s). Latest: **{latest_name}**")

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📋 Import", "📊 Snapshot", "📈 Compare", "📈 Booking Curve", "🗂️ History"
])
with tab1: page_paste()
with tab2: page_snapshot()
with tab3: page_compare()
with tab4: page_booking_curve()
with tab5: page_history()