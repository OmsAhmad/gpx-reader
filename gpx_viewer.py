#!/usr/bin/env python3
"""GPX Viewer: Parse a GPX file and generate an interactive HTML map with elevation profile.

Usage:
    python gpx_viewer.py activity.gpx          # Generate HTML with pre-loaded data
    python gpx_viewer.py --viewer              # Generate a blank viewer (drag & drop only)
    python gpx_viewer.py activity.gpx -o out.html
"""

import argparse
import json
import math
import os
import sys
from datetime import timezone

try:
    import gpxpy
except ImportError:
    sys.exit("Missing dependency: pip install gpxpy")

try:
    from timezonefinder import TimezoneFinder
    import pytz
    HAS_TZ = True
except ImportError:
    HAS_TZ = False


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_gpx(filepath):
    with open(filepath, "r") as f:
        gpx = gpxpy.parse(f)

    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                points.append({
                    "lat": pt.latitude,
                    "lon": pt.longitude,
                    "ele": pt.elevation if pt.elevation is not None else 0,
                    "time": pt.time,
                })

    if not points:
        for route in gpx.routes:
            for pt in route.points:
                points.append({
                    "lat": pt.latitude,
                    "lon": pt.longitude,
                    "ele": pt.elevation if pt.elevation is not None else 0,
                    "time": pt.time,
                })

    if not points:
        sys.exit("No track or route points found in GPX file.")

    local_tz = None
    if HAS_TZ and points[0]["time"] is not None:
        tf = TimezoneFinder()
        tz_name = tf.timezone_at(lat=points[0]["lat"], lng=points[0]["lon"])
        if tz_name:
            local_tz = pytz.timezone(tz_name)

    cum_dist = 0.0
    data = []
    start_time = points[0]["time"]

    for i, pt in enumerate(points):
        if i > 0:
            cum_dist += haversine(points[i - 1]["lat"], points[i - 1]["lon"], pt["lat"], pt["lon"])

        time_str = ""
        elapsed_str = ""
        if pt["time"] is not None:
            t = pt["time"]
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if local_tz:
                t = t.astimezone(local_tz)
            time_str = t.strftime("%H:%M:%S")

            if start_time is not None:
                st = start_time
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                elapsed = (pt["time"] - start_time).total_seconds()
                h = int(elapsed // 3600)
                m = int((elapsed % 3600) // 60)
                s = int(elapsed % 60)
                elapsed_str = f"{h}:{m:02d}:{s:02d}"

        data.append({
            "lat": pt["lat"],
            "lon": pt["lon"],
            "ele": round(pt["ele"], 1),
            "dist": round(cum_dist, 1),
            "time": time_str,
            "elapsed": elapsed_str,
        })

    tz_label = ""
    if local_tz:
        tz_label = str(local_tz)

    return data, tz_label


def generate_html(data, tz_label, output_path):
    if data:
        initial_data = json.dumps(data)
        initial_tz = json.dumps(tz_label)
    else:
        initial_data = "null"
        initial_tz = '""'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GPX Viewer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a2e; color: #eee; }}
#map {{ width: 100%; height: 55vh; }}
#profile-container {{ position: relative; width: 100%; height: 45vh; background: #16213e; padding: 10px 20px 30px 60px; }}
#profile {{ width: 100%; height: 100%; cursor: crosshair; }}
#tooltip {{ position: absolute; display: none; background: rgba(0,0,0,0.85); color: #fff; padding: 8px 12px; border-radius: 6px; font-size: 13px; pointer-events: none; white-space: nowrap; z-index: 1000; }}
#selection-info {{ position: absolute; top: 10px; right: 20px; background: rgba(0,0,0,0.8); color: #0ff; padding: 8px 14px; border-radius: 6px; font-size: 13px; display: none; z-index: 1000; }}
#drop-overlay {{
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0, 255, 255, 0.08); border: 4px dashed #0ff;
    display: none; z-index: 9999; align-items: center; justify-content: center;
}}
#drop-overlay .drop-text {{ font-size: 28px; color: #0ff; text-shadow: 0 0 20px rgba(0,255,255,0.5); }}
#empty-state {{
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    text-align: center; color: #666; font-size: 18px; pointer-events: none;
}}
#empty-state .icon {{ font-size: 48px; margin-bottom: 12px; opacity: 0.5; }}
</style>
</head>
<body>
<div id="drop-overlay"><span class="drop-text">Drop GPX file here</span></div>
<div id="map"></div>
<div id="profile-container">
    <canvas id="profile"></canvas>
    <div id="tooltip"></div>
    <div id="selection-info"></div>
    <div id="empty-state" style="display:none">
        <div class="icon">&#128506;</div>
        <div>Drag &amp; drop a .gpx file anywhere</div>
    </div>
</div>

<script>
let DATA = {initial_data};
let TZ_LABEL = {initial_tz};

const map = L.map('map').setView([20, 0], 2);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; OpenStreetMap contributors'
}}).addTo(map);

let routeLine = L.polyline([], {{ color: '#ff6b6b', weight: 3.5, opacity: 0.9 }}).addTo(map);
let highlightLine = L.polyline([], {{ color: '#0ff', weight: 5, opacity: 0.9 }}).addTo(map);
const marker = L.circleMarker([0, 0], {{ radius: 8, color: '#fff', fillColor: '#ff6b6b', fillOpacity: 1, weight: 2 }});

const canvas = document.getElementById('profile');
const ctx = canvas.getContext('2d');
const container = document.getElementById('profile-container');
const tooltip = document.getElementById('tooltip');
const selInfo = document.getElementById('selection-info');
const emptyState = document.getElementById('empty-state');

let dpr, plotLeft, plotTop, plotWidth, plotHeight;
let minEle, maxEle, maxDist;
let selecting = false, selStart = null, selEnd = null;

function setup() {{
    dpr = window.devicePixelRatio || 1;
    canvas.width = canvas.clientWidth * dpr;
    canvas.height = canvas.clientHeight * dpr;
    ctx.scale(dpr, dpr);

    plotLeft = 0;
    plotTop = 10;
    plotWidth = canvas.clientWidth;
    plotHeight = canvas.clientHeight - 20;

    if (!DATA || DATA.length === 0) return;

    minEle = Math.min(...DATA.map(d => d.ele));
    maxEle = Math.max(...DATA.map(d => d.ele));
    if (maxEle - minEle < 10) {{ minEle -= 5; maxEle += 5; }}
    let pad = (maxEle - minEle) * 0.1;
    minEle -= pad;
    maxEle += pad;
    maxDist = DATA[DATA.length - 1].dist;
}}

function xForDist(dist) {{ return plotLeft + (dist / maxDist) * plotWidth; }}
function yForEle(ele) {{ return plotTop + plotHeight - ((ele - minEle) / (maxEle - minEle)) * plotHeight; }}
function distForX(x) {{ return ((x - plotLeft) / plotWidth) * maxDist; }}

function draw() {{
    const w = canvas.clientWidth, h = canvas.clientHeight;
    ctx.clearRect(0, 0, w, h);

    if (!DATA || DATA.length === 0) return;

    if (selStart !== null && selEnd !== null) {{
        const x1 = Math.min(selStart, selEnd);
        const x2 = Math.max(selStart, selEnd);
        ctx.fillStyle = 'rgba(0, 255, 255, 0.1)';
        ctx.fillRect(x1, plotTop, x2 - x1, plotHeight);
    }}

    ctx.strokeStyle = 'rgba(255,255,255,0.07)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {{
        const y = plotTop + (plotHeight / 4) * i;
        ctx.beginPath(); ctx.moveTo(plotLeft, y); ctx.lineTo(plotLeft + plotWidth, y); ctx.stroke();
    }}

    ctx.beginPath();
    ctx.moveTo(xForDist(DATA[0].dist), yForEle(DATA[0].ele));
    for (let i = 1; i < DATA.length; i++) {{
        ctx.lineTo(xForDist(DATA[i].dist), yForEle(DATA[i].ele));
    }}
    ctx.lineTo(xForDist(DATA[DATA.length - 1].dist), plotTop + plotHeight);
    ctx.lineTo(xForDist(DATA[0].dist), plotTop + plotHeight);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, plotTop, 0, plotTop + plotHeight);
    grad.addColorStop(0, 'rgba(255, 107, 107, 0.6)');
    grad.addColorStop(1, 'rgba(255, 107, 107, 0.05)');
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    ctx.moveTo(xForDist(DATA[0].dist), yForEle(DATA[0].ele));
    for (let i = 1; i < DATA.length; i++) {{
        ctx.lineTo(xForDist(DATA[i].dist), yForEle(DATA[i].ele));
    }}
    ctx.strokeStyle = '#ff6b6b';
    ctx.lineWidth = 2;
    ctx.stroke();
}}

function findClosestIndex(dist) {{
    let lo = 0, hi = DATA.length - 1;
    while (lo < hi) {{
        const mid = (lo + hi) >> 1;
        if (DATA[mid].dist < dist) lo = mid + 1;
        else hi = mid;
    }}
    if (lo > 0 && Math.abs(DATA[lo - 1].dist - dist) < Math.abs(DATA[lo].dist - dist)) lo--;
    return lo;
}}

function showMarker(idx) {{
    const d = DATA[idx];
    marker.setLatLng([d.lat, d.lon]);
    if (!map.hasLayer(marker)) marker.addTo(map);
}}

function getCanvasX(e) {{
    const rect = canvas.getBoundingClientRect();
    return e.clientX - rect.left;
}}

canvas.addEventListener('mousemove', (e) => {{
    if (!DATA || DATA.length === 0) return;
    const x = getCanvasX(e);
    const dist = distForX(x);
    if (dist < 0 || dist > maxDist) {{ tooltip.style.display = 'none'; return; }}

    const idx = findClosestIndex(dist);
    const d = DATA[idx];
    showMarker(idx);

    let html = `<b>${{(d.dist/1000 * 0.621371).toFixed(2)}} mi</b> &mdash; ${{(d.ele * 3.28084).toFixed(0)}} ft`;
    if (d.time) html += `<br>Time: ${{d.time}}${{TZ_LABEL ? ' (' + TZ_LABEL + ')' : ''}}`;
    if (d.elapsed) html += `<br>Elapsed: ${{d.elapsed}}`;
    tooltip.innerHTML = html;
    tooltip.style.display = 'block';

    const rect = container.getBoundingClientRect();
    let tx = e.clientX - rect.left + 15;
    let ty = e.clientY - rect.top - 40;
    if (tx + tooltip.offsetWidth > rect.width - 10) tx = e.clientX - rect.left - tooltip.offsetWidth - 15;
    tooltip.style.left = tx + 'px';
    tooltip.style.top = ty + 'px';

    if (selecting) {{
        selEnd = x;
        draw();
        drawCrosshair(x, idx);
        updateSelection();
    }} else {{
        draw();
        drawCrosshair(x, idx);
    }}
}});

function drawCrosshair(x, idx) {{
    const d = DATA[idx];
    const y = yForEle(d.ele);
    ctx.strokeStyle = 'rgba(255,255,255,0.3)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(x, plotTop); ctx.lineTo(x, plotTop + plotHeight); ctx.stroke();
    ctx.setLineDash([]);

    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.fillStyle = '#fff';
    ctx.fill();
    ctx.strokeStyle = '#ff6b6b';
    ctx.lineWidth = 2;
    ctx.stroke();
}}

canvas.addEventListener('mousedown', (e) => {{
    if (!DATA || DATA.length === 0) return;
    selecting = true;
    selStart = getCanvasX(e);
    selEnd = selStart;
    highlightLine.setLatLngs([]);
    selInfo.style.display = 'none';
}});

canvas.addEventListener('mouseup', () => {{
    if (selecting && selStart !== null && selEnd !== null && Math.abs(selEnd - selStart) > 5) {{
        updateSelection();
    }} else {{
        selStart = null;
        selEnd = null;
        highlightLine.setLatLngs([]);
        selInfo.style.display = 'none';
        draw();
    }}
    selecting = false;
}});

canvas.addEventListener('mouseleave', () => {{
    if (!selecting) {{
        tooltip.style.display = 'none';
        marker.remove();
        selStart = null; selEnd = null;
        highlightLine.setLatLngs([]);
        selInfo.style.display = 'none';
        draw();
    }}
}});

function updateSelection() {{
    if (selStart === null || selEnd === null) return;
    const x1 = Math.min(selStart, selEnd);
    const x2 = Math.max(selStart, selEnd);
    const d1 = distForX(x1);
    const d2 = distForX(x2);
    const i1 = findClosestIndex(d1);
    const i2 = findClosestIndex(d2);

    if (i1 === i2) return;

    const segDist = DATA[i2].dist - DATA[i1].dist;
    const eleChange = DATA[i2].ele - DATA[i1].ele;
    let eleGain = 0, eleLoss = 0;
    for (let i = i1 + 1; i <= i2; i++) {{
        const diff = DATA[i].ele - DATA[i - 1].ele;
        if (diff > 0) eleGain += diff;
        else eleLoss += diff;
    }}

    selInfo.innerHTML = `Distance: <b>${{(segDist/1000 * 0.621371).toFixed(2)}} mi</b> | ` +
        `Elevation: <b>${{eleChange >= 0 ? '+' : ''}}${{(eleChange * 3.28084).toFixed(0)}} ft</b> | ` +
        `Gain: <b>+${{(eleGain * 3.28084).toFixed(0)}} ft</b> | Loss: <b>${{(eleLoss * 3.28084).toFixed(0)}} ft</b>`;
    selInfo.style.display = 'block';

    const segLatLngs = DATA.slice(i1, i2 + 1).map(d => [d.lat, d.lon]);
    highlightLine.setLatLngs(segLatLngs);
}}

// --- Drag and Drop GPX Support ---

function haversineJS(lat1, lon1, lat2, lon2) {{
    const R = 6371000;
    const phi1 = lat1 * Math.PI / 180, phi2 = lat2 * Math.PI / 180;
    const dphi = (lat2 - lat1) * Math.PI / 180;
    const dlam = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dphi/2)**2 + Math.cos(phi1)*Math.cos(phi2)*Math.sin(dlam/2)**2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}}

function parseGPXClient(xmlStr) {{
    const parser = new DOMParser();
    const doc = parser.parseFromString(xmlStr, 'application/xml');

    let trkpts = doc.querySelectorAll('trkpt');
    if (trkpts.length === 0) trkpts = doc.querySelectorAll('rtept');
    if (trkpts.length === 0) return null;

    const points = [];
    for (const pt of trkpts) {{
        const lat = parseFloat(pt.getAttribute('lat'));
        const lon = parseFloat(pt.getAttribute('lon'));
        const eleEl = pt.querySelector('ele');
        const timeEl = pt.querySelector('time');
        points.push({{
            lat, lon,
            ele: eleEl ? parseFloat(eleEl.textContent) : 0,
            time: timeEl ? new Date(timeEl.textContent) : null,
        }});
    }}

    // Determine timezone from coordinates using Intl
    let tzLabel = '';
    try {{
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        tzLabel = tz;
    }} catch(e) {{}}

    const startTime = points[0].time;
    let cumDist = 0;
    const data = [];

    for (let i = 0; i < points.length; i++) {{
        const pt = points[i];
        if (i > 0) {{
            cumDist += haversineJS(points[i-1].lat, points[i-1].lon, pt.lat, pt.lon);
        }}

        let timeStr = '', elapsedStr = '';
        if (pt.time) {{
            timeStr = pt.time.toLocaleTimeString('en-GB', {{ hour: '2-digit', minute: '2-digit', second: '2-digit' }});
            if (startTime) {{
                const elapsed = (pt.time - startTime) / 1000;
                const h = Math.floor(elapsed / 3600);
                const m = Math.floor((elapsed % 3600) / 60);
                const s = Math.floor(elapsed % 60);
                elapsedStr = `${{h}}:${{String(m).padStart(2,'0')}}:${{String(s).padStart(2,'0')}}`;
            }}
        }}

        data.push({{
            lat: pt.lat, lon: pt.lon,
            ele: Math.round(pt.ele * 10) / 10,
            dist: Math.round(cumDist * 10) / 10,
            time: timeStr,
            elapsed: elapsedStr,
        }});
    }}

    return {{ data, tzLabel }};
}}

function loadGPXData(data, tzLabel) {{
    DATA = data;
    TZ_LABEL = tzLabel || '';
    emptyState.style.display = 'none';

    const latlngs = DATA.map(d => [d.lat, d.lon]);
    routeLine.setLatLngs(latlngs);
    highlightLine.setLatLngs([]);
    marker.remove();
    selStart = null; selEnd = null;
    selInfo.style.display = 'none';
    tooltip.style.display = 'none';

    map.fitBounds(L.latLngBounds(latlngs).pad(0.05));
    setup();
    draw();
}}

// Drag and drop handlers
const dropOverlay = document.getElementById('drop-overlay');
let dragCounter = 0;

document.addEventListener('dragenter', (e) => {{
    e.preventDefault();
    dragCounter++;
    dropOverlay.style.display = 'flex';
}});

document.addEventListener('dragleave', (e) => {{
    e.preventDefault();
    dragCounter--;
    if (dragCounter <= 0) {{
        dragCounter = 0;
        dropOverlay.style.display = 'none';
    }}
}});

document.addEventListener('dragover', (e) => {{
    e.preventDefault();
}});

document.addEventListener('drop', (e) => {{
    e.preventDefault();
    dragCounter = 0;
    dropOverlay.style.display = 'none';

    const files = e.dataTransfer.files;
    if (files.length === 0) return;

    const file = files[0];
    if (!file.name.toLowerCase().endsWith('.gpx')) {{
        alert('Please drop a .gpx file');
        return;
    }}

    const reader = new FileReader();
    reader.onload = (ev) => {{
        const result = parseGPXClient(ev.target.result);
        if (!result || !result.data || result.data.length === 0) {{
            alert('Could not parse GPX file or no points found.');
            return;
        }}
        loadGPXData(result.data, result.tzLabel);
    }};
    reader.readAsText(file);
}});

// Initialize
if (DATA && DATA.length > 0) {{
    loadGPXData(DATA, TZ_LABEL);
}} else {{
    emptyState.style.display = 'block';
}}

window.addEventListener('resize', () => {{ setup(); draw(); }});
</script>
</body>
</html>"""

    with open(output_path, "w") as f:
        f.write(html)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Generate an interactive GPX viewer")
    parser.add_argument("gpx_file", nargs="?", help="Path to the GPX file (omit for blank viewer)")
    parser.add_argument("-o", "--output", help="Output HTML file path")
    parser.add_argument("--viewer", action="store_true", help="Generate a blank viewer (drag & drop only)")
    args = parser.parse_args()

    if args.viewer or args.gpx_file is None:
        output = args.output or "gpx_viewer.html"
        out = generate_html([], "", output)
        print(f"Generated blank viewer: {out}")
        print(f"Open in browser and drag & drop a .gpx file: file://{os.path.abspath(out)}")
        return

    if not os.path.exists(args.gpx_file):
        sys.exit(f"File not found: {args.gpx_file}")

    output = args.output or os.path.splitext(args.gpx_file)[0] + ".html"

    print(f"Parsing {args.gpx_file}...")
    data, tz_label = parse_gpx(args.gpx_file)
    print(f"  {len(data)} points, {data[-1]['dist']/1000:.2f} km total distance")
    if tz_label:
        print(f"  Local timezone: {tz_label}")

    out = generate_html(data, tz_label, output)
    print(f"Generated: {out}")
    print(f"Open in browser: file://{os.path.abspath(out)}")
    print("  (You can also drag & drop another .gpx file onto the viewer to replace it)")


if __name__ == "__main__":
    main()
