#!/usr/bin/env python3
"""
rns-web.py — Reticulum/LXMF web bridge for Raspberry Pi Zero 2W

Serves a browser UI showing live RNS announces and LXMF messages.
Runs headless alongside rnsd. No display required.

Ports:
    8082  HTTP  — serves rns-index.html
    8083  WS    — pushes live JSON events, receives commands
    8084  HTTP  — combined iframe page (RNS left, SDR right)
    8085  WS    — terminal (shell command execution)

Dependencies (install in venv):
    rns==1.1.3  LXMF==0.9.3  websockets peewee<4.0.0  msgpack==1.1.2

Usage:
    source ~/rns-web-venv/bin/activate
    python3 rns-web.py
"""

import asyncio
import json
import logging
import os
import pty
import fcntl
import struct
import termios
import sqlite3
import time
import threading
from pathlib import Path

import RNS
import LXMF
import websockets
from websockets import serve as ws_serve

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rns-web")

# ── Configuration ─────────────────────────────────────────────────────────────
BASE_DIR          = Path(__file__).parent
HTML_PATH         = BASE_DIR / "rns-index.html"
DB_PATH           = BASE_DIR / "messages.db"
STORAGE_PATH      = str(BASE_DIR / "lxmf-storage")
DISPLAY_NAME      = "raspi20"
HTTP_PORT         = 8082
WS_PORT           = 8083
COMBINED_PORT     = 8084
TERMINAL_PORT     = 8085
TERM_PAGE_PORT    = 8087
ANNOUNCE_INTERVAL = 900             # seconds — 15 minutes

# ── Globals ───────────────────────────────────────────────────────────────────
_loop: asyncio.AbstractEventLoop = None
_clients: set = set()
_clients_lock = threading.Lock()
_announces: dict = {}               # hash_hex → announce dict
_announces_lock = threading.Lock()
_router: LXMF.LXMRouter = None
_local_dest = None                  # our LXMF delivery destination

# ── Display name sanitisation ─────────────────────────────────────────────────

def sanitise_name(raw: str, fallback: str) -> str:
    """
    Strip non-printable and non-ASCII-safe characters from a display name.
    Returns fallback (short hash) if the result is empty or looks like binary.
    """
    if not raw:
        return fallback
    # Keep only printable Unicode chars (excludes control chars, surrogates etc.)
    cleaned = "".join(c for c in raw if c.isprintable())
    cleaned = cleaned.strip()
    if not cleaned:
        return fallback
    # If more than 25% of chars are non-ASCII, treat as binary garbage
    non_ascii = sum(1 for c in cleaned if ord(c) > 127)
    if len(cleaned) > 0 and non_ascii / len(cleaned) > 0.25:
        return fallback
    return cleaned

# ── Hash deduplication ────────────────────────────────────────────────────────
# MeshChat nodes announce with multiple hashes (delivery + propagation).
# We maintain a name→canonical_hash map so all messages from the same
# display name are stored under one hash regardless of which hash they
# actually arrived from.
_canonical_hash: dict = {}          # display_name → first-seen hash_hex
_canonical_lock = threading.Lock()

def canonical_peer_hash(peer_hash: str, peer_name: str) -> str:
    """
    Return the canonical hash for a named peer.
    The first hash seen for a given display name becomes canonical.
    Subsequent hashes for the same name are redirected to it.
    If peer_name is empty, return peer_hash unchanged.
    """
    if not peer_name:
        return peer_hash
    with _canonical_lock:
        if peer_name not in _canonical_hash:
            _canonical_hash[peer_name] = peer_hash
            log.info("Canonical hash for '%s' → %s", peer_name, peer_hash[:12])
        canon = _canonical_hash[peer_name]
    if canon != peer_hash:
        log.info("Dedup: %s → %s (%s)", peer_hash[:12], canon[:12], peer_name)
    return canon

# ── SQLite helpers ────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            direction   TEXT    NOT NULL,
            peer_hash   TEXT    NOT NULL,
            peer_name   TEXT,
            title       TEXT,
            content     TEXT,
            status      TEXT DEFAULT 'sent',
            msg_hash    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer_hash);
        CREATE INDEX IF NOT EXISTS idx_messages_hash ON messages(msg_hash);
    """)
    conn.commit()
    conn.close()
    log.info("Database ready: %s", DB_PATH)

def db_load_canonical_hashes():
    """
    On startup, rebuild the canonical hash map from existing message history
    so deduplication is consistent across restarts.
    """
    conn = db_connect()
    rows = conn.execute(
        "SELECT peer_hash, peer_name, MIN(ts) as first_ts "
        "FROM messages WHERE peer_name != '' "
        "GROUP BY peer_name ORDER BY first_ts ASC"
    ).fetchall()
    conn.close()
    with _canonical_lock:
        for row in rows:
            name = row["peer_name"]
            if name and name not in _canonical_hash:
                _canonical_hash[name] = row["peer_hash"]
    log.info("Loaded %d canonical hash entries from history", len(_canonical_hash))

def db_save_message(direction, peer_hash, peer_name, title, content,
                    ts=None, msg_hash=None) -> int:
    """Save a message and return its row id."""
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO messages (ts, direction, peer_hash, peer_name, title, content, msg_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts or time.time(), direction, peer_hash,
         peer_name or "", title or "", content or "", msg_hash)
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id

def db_update_status(msg_hash: str, status: str):
    """Update delivery status for an outbound message by its LXMF hash."""
    conn = db_connect()
    conn.execute(
        "UPDATE messages SET status=? WHERE msg_hash=?",
        (status, msg_hash)
    )
    conn.commit()
    conn.close()

def db_get_conversation(peer_hash: str, limit=100) -> list:
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM messages WHERE peer_hash=? ORDER BY ts DESC LIMIT ?",
        (peer_hash, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]

def db_get_conversations() -> list:
    conn = db_connect()
    rows = conn.execute("""
        SELECT peer_hash, peer_name, direction, content, MAX(ts) as ts
        FROM messages
        GROUP BY peer_hash
        ORDER BY ts DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── WebSocket broadcast ───────────────────────────────────────────────────────

def _broadcast_from_thread(msg: dict):
    """Thread-safe broadcast from RNS/LXMF callback threads."""
    if _loop is None:
        return
    payload = json.dumps(msg)
    _loop.call_soon_threadsafe(_schedule_broadcast, payload)

def _schedule_broadcast(payload: str):
    asyncio.ensure_future(_broadcast(payload))

async def _broadcast(payload: str):
    with _clients_lock:
        clients = set(_clients)
    if not clients:
        return
    dead = set()
    for ws in clients:
        try:
            await ws.send(payload)
        except Exception:
            dead.add(ws)
    if dead:
        with _clients_lock:
            _clients.difference_update(dead)

# ── Auto-announce task ────────────────────────────────────────────────────────

def do_announce():
    """Send an LXMF announce. Safe to call from any thread."""
    global _local_dest
    if _local_dest is None:
        return
    try:
        _local_dest.announce()
        log.info("Announced — %s (%s)", DISPLAY_NAME, _local_dest.hash.hex()[:12])
        _broadcast_from_thread({
            "type": "announce_sent",
            "ts":   time.time(),
            "name": DISPLAY_NAME,
            "hash": _local_dest.hash.hex(),
        })
    except Exception as e:
        log.error("Announce error: %s", e)

async def auto_announce_loop():
    """Announce on startup then every ANNOUNCE_INTERVAL seconds."""
    await asyncio.sleep(5)
    do_announce()
    while True:
        await asyncio.sleep(ANNOUNCE_INTERVAL)
        do_announce()

# ── Noise floor poller ────────────────────────────────────────────────────────

import re as _re
import shutil as _shutil

def _parse_rnstatus_noise() -> tuple:
    """
    Run rnstatus and parse noise floor + interference values.
    Returns (noise_db: int|None, intrfrnc_db: int|None)
    Handles both formats:
      'Noise Fl. : -104 dBm, no interference'
      'Noise Fl. : -104 dBm, Intrfrnc.: -84 dBm 53s ago'
      'Noise Fl.: -104 dBm'   (older format)
    """
    rnstatus = _shutil.which("rnstatus") or str(Path(STORAGE_PATH).parent / "rns-web-venv" / "bin" / "rnstatus")
    try:
        import subprocess
        result = subprocess.run(
            [rnstatus],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout + result.stderr
    except Exception as e:
        log.debug("rnstatus error: %s", e)
        return None, None

    noise_db   = None
    intrfrnc_db = None

    for line in output.splitlines():
        # Noise floor
        m = _re.search(r'Noise\s+Fl[.\s]+:\s*(-?\d+)\s*dBm', line, _re.IGNORECASE)
        if m:
            noise_db = int(m.group(1))
            # Interference on same line
            if 'no interference' in line.lower():
                intrfrnc_db = None
            else:
                mi = _re.search(r'Intrfrnc[.\s]+:\s*(-?\d+)\s*dBm', line, _re.IGNORECASE)
                if mi:
                    intrfrnc_db = int(mi.group(1))
        # Interference on its own line
        if noise_db is not None and intrfrnc_db is None:
            mi = _re.search(r'Intrfrnc[.\s]+:\s*(-?\d+)\s*dBm', line, _re.IGNORECASE)
            if mi:
                intrfrnc_db = int(mi.group(1))

    return noise_db, intrfrnc_db

async def noise_poll_loop():
    """Poll rnstatus every second and broadcast noise_sample to all WS clients."""
    await asyncio.sleep(8)   # let rnsd settle first
    while True:
        try:
            noise_db, intrfrnc_db = await asyncio.get_event_loop().run_in_executor(
                None, _parse_rnstatus_noise
            )
            if noise_db is not None:
                _broadcast_from_thread({
                    "type":       "noise_sample",
                    "noise_db":   noise_db,
                    "intrfrnc_db": intrfrnc_db,
                    "ts":          time.time(),
                })
        except Exception as e:
            log.debug("noise_poll_loop error: %s", e)
        await asyncio.sleep(1)

# ── RNS Announce handler ──────────────────────────────────────────────────────

class AnnounceHandler:
    """Receives all announces from rnsd and forwards to browser."""
    aspect_filter = None

    def received_announce(self, destination_hash, announced_identity, app_data):
        hash_hex  = destination_hash.hex()
        disp_name = ""

        try:
            if app_data:
                try:
                    import msgpack
                    decoded = msgpack.unpackb(app_data)
                    if isinstance(decoded, list) and len(decoded) > 0:
                        disp_name = decoded[0].decode("utf-8", errors="replace")
                    elif isinstance(decoded, bytes):
                        disp_name = decoded.decode("utf-8", errors="replace")
                    else:
                        disp_name = str(decoded)
                except Exception:
                    disp_name = app_data.decode("utf-8", errors="replace")
        except Exception:
            pass

        # Sanitise display name — reject binary garbage
        disp_name = sanitise_name(disp_name, hash_hex[:12] + "…")

        # Register canonical hash for this display name
        canon = canonical_peer_hash(hash_hex, disp_name)

        # hops_to returns RNS.Transport.PATHFINDER_M (128) for unknown paths
        raw_hops = RNS.Transport.hops_to(destination_hash)
        hops = raw_hops if (raw_hops is not None and 0 <= raw_hops <= 32) else -1

        entry = {
            "hash": canon,
            "name": disp_name,
            "hops": hops,
            "ts":   time.time(),
        }

        with _announces_lock:
            # Store under canonical hash — merges duplicate entries
            _announces[canon] = entry

        log.info("Announce: %s  %s  hops=%s",
                 hash_hex[:12], entry["name"], entry["hops"])

        _broadcast_from_thread({
            "type":     "announce",
            "announce": entry,
        })

# ── LXMF delivery callback ────────────────────────────────────────────────────

def lxmf_delivery(message: LXMF.LXMessage):
    """Called by LXMRouter in its own thread when a message arrives."""
    try:
        raw_hash  = message.source_hash.hex()
        peer_name = ""

        # Look up display name from announce table using raw hash first
        with _announces_lock:
            entry = _announces.get(raw_hash) or _announces.get(
                canonical_peer_hash(raw_hash, "")
            )
            if entry:
                peer_name = entry.get("name", "")

        # If not in announces yet, check canonical map by iterating names
        if not peer_name:
            with _canonical_lock:
                for name, canon in _canonical_hash.items():
                    if canon == raw_hash or raw_hash == canon:
                        peer_name = name
                        break

        # Resolve to canonical hash for storage
        canon_hash = canonical_peer_hash(raw_hash, peer_name)

        title   = message.title.decode("utf-8", errors="replace") if message.title else ""
        content = message.content.decode("utf-8", errors="replace") if message.content else ""
        ts      = message.timestamp or time.time()

        log.info("LXMF in from %s (canon %s)  '%s'",
                 raw_hash[:12], canon_hash[:12], content[:40])

        db_save_message("in", canon_hash, peer_name, title, content, ts)

        _broadcast_from_thread({
            "type": "message",
            "msg": {
                "direction": "in",
                "peer_hash": canon_hash,
                "peer_name": peer_name,
                "title":     title,
                "content":   content,
                "ts":        ts,
                "status":    "received",
            }
        })
    except Exception as e:
        log.error("lxmf_delivery error: %s", e)

# ── LXMF receipt callback ─────────────────────────────────────────────────────

def lxmf_receipt(receipt):
    """Called when a delivery receipt arrives for an outbound message."""
    try:
        msg_hash = receipt.hash.hex() if hasattr(receipt, "hash") else None
        if msg_hash is None:
            return
        log.info("Delivery receipt: %s → delivered", msg_hash[:12])
        db_update_status(msg_hash, "delivered")
        _broadcast_from_thread({
            "type":     "receipt",
            "msg_hash": msg_hash,
            "status":   "delivered",
        })
    except Exception as e:
        log.error("lxmf_receipt error: %s", e)

# ── Send LXMF message ─────────────────────────────────────────────────────────

def send_lxmf(dest_hash_hex: str, content: str, title: str = "") -> bool:
    """Send an LXMF message. Returns True if queued successfully."""
    global _router, _local_dest
    try:
        dest_hash     = bytes.fromhex(dest_hash_hex)
        dest_identity = RNS.Identity.recall(dest_hash)
        if dest_identity is None:
            log.warning("No identity for %s — cannot send", dest_hash_hex[:12])
            return False

        dest = RNS.Destination(
            dest_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery",
        )

        lxm = LXMF.LXMessage(
            dest,
            _local_dest,
            content.encode("utf-8"),
            title.encode("utf-8") if title else b"",
            desired_method=LXMF.LXMessage.DIRECT,
        )
        lxm.register_delivery_callback(lxmf_receipt)
        _router.handle_outbound(lxm)

        msg_hash = lxm.hash.hex() if hasattr(lxm, "hash") and lxm.hash else None

        with _announces_lock:
            entry = _announces.get(dest_hash_hex, {})
        peer_name = entry.get("name", "")

        db_save_message("out", dest_hash_hex, peer_name, title, content,
                        msg_hash=msg_hash)

        _broadcast_from_thread({
            "type": "message",
            "msg": {
                "direction": "out",
                "peer_hash": dest_hash_hex,
                "peer_name": peer_name,
                "title":     title,
                "content":   content,
                "ts":        time.time(),
                "status":    "sent",
                "msg_hash":  msg_hash,
            }
        })

        log.info("LXMF out → %s  '%s'", dest_hash_hex[:12], content[:40])
        return True

    except Exception as e:
        log.error("send_lxmf error: %s", e)
        return False

# ── WebSocket command handler ─────────────────────────────────────────────────

async def handle_ws_command(ws, text: str):
    try:
        cmd = json.loads(text)
    except json.JSONDecodeError:
        return

    action = cmd.get("cmd")

    if action == "get_announces":
        with _announces_lock:
            announces = list(_announces.values())
        announces.sort(key=lambda a: a["ts"], reverse=True)
        await ws.send(json.dumps({"type": "announces", "announces": announces}))

    elif action == "get_conversations":
        convs = db_get_conversations()
        await ws.send(json.dumps({"type": "conversations", "conversations": convs}))

    elif action == "get_messages":
        peer_hash = cmd.get("peer_hash", "")
        msgs = db_get_conversation(peer_hash)
        await ws.send(json.dumps({
            "type": "conversation", "peer_hash": peer_hash, "messages": msgs
        }))

    elif action == "send":
        dest  = cmd.get("dest", "")
        body  = cmd.get("content", "").strip()
        title = cmd.get("title", "")
        if dest and body:
            ok = send_lxmf(dest, body, title)
            await ws.send(json.dumps({"type": "send_result", "ok": ok, "dest": dest}))

    elif action == "announce_now":
        await asyncio.get_event_loop().run_in_executor(None, do_announce)

    elif action == "get_identity":
        if _local_dest:
            await ws.send(json.dumps({
                "type": "identity",
                "hash": _local_dest.hash.hex(),
                "name": DISPLAY_NAME,
            }))

# ── WebSocket connection handler ──────────────────────────────────────────────

async def ws_handler(ws):
    with _clients_lock:
        _clients.add(ws)
    log.info("WS client connected: %s", ws.remote_address)

    with _announces_lock:
        announces = list(_announces.values())
    announces.sort(key=lambda a: a["ts"], reverse=True)
    await ws.send(json.dumps({"type": "announces", "announces": announces}))

    convs = db_get_conversations()
    await ws.send(json.dumps({"type": "conversations", "conversations": convs}))

    if _local_dest:
        await ws.send(json.dumps({
            "type": "identity",
            "hash": _local_dest.hash.hex(),
            "name": DISPLAY_NAME,
        }))

    try:
        async for text in ws:
            await handle_ws_command(ws, text)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        with _clients_lock:
            _clients.discard(ws)
        log.info("WS client disconnected: %s", ws.remote_address)

# ── HTTP server ───────────────────────────────────────────────────────────────

STATIC_FILES = {
    "/xterm.js":       ("application/javascript", Path("/home/user/xterm.min.js")),
    "/xterm.css":      ("text/css",               Path("/home/user/xterm.min.css")),
    "/addon-fit.js":   ("application/javascript", Path("/home/user/addon-fit.min.js")),
}

async def http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        raw = await reader.read(4096)
        # Parse request path
        path = "/"
        try:
            first_line = raw.split(b"\r\n")[0].decode()
            path = first_line.split(" ")[1]
        except Exception:
            pass

        if path in STATIC_FILES:
            mime, fpath = STATIC_FILES[path]
            try:
                body = fpath.read_bytes()
            except FileNotFoundError:
                body = b"/* not found */"
            header = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Type: {mime}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode()
        else:
            try:
                body = HTML_PATH.read_bytes()
            except FileNotFoundError:
                body = b"<h1>rns-index.html not found</h1>"
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode()
        writer.write(header + body)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()

# ── Combined page HTTP handler ────────────────────────────────────────────────

COMBINED_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RNS + SDR</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { background: #0a0a0a; width: 100%; height: 100%; overflow: hidden; font-family: monospace; }
#container { display: flex; width: 100%; height: 100vh; flex-direction: row; }
#left-col { width: 55%; min-width: 520px; flex-shrink: 0; display: flex; flex-direction: column; height: 100%; }
#sdr-frame { border: none; width: 100%; flex-shrink: 0; height: 40%; }
#left-divider { height: 4px; background: #222233; cursor: row-resize; flex-shrink: 0; }
#map-frame { border: none; width: 100%; flex: 1; min-height: 0; }
#divider { width: 4px; background: #222233; cursor: col-resize; flex-shrink: 0; }
#right-col { flex: 1; min-width: 0; display: flex; flex-direction: column; height: 100%; }
#rns-frame { border: none; flex: 1; min-height: 0; width: 100%; }
#noise-panel { height: 132px; flex-shrink: 0; background: #0a0a0a; border-top: 2px solid #222233; display: flex; flex-direction: column; }
#noise-titlebar { display: flex; align-items: center; justify-content: space-between; padding: 2px 10px; background: #0d0d0d; border-bottom: 1px solid #1a1a1a; flex-shrink: 0; }
#noise-titlebar span { font-size: 10px; color: #555; text-transform: uppercase; letter-spacing: 0.06em; }
#noise-reading { font-size: 11px; color: #00cc66 !important; }
#noise-intrfrnc { font-size: 10px; color: #cc4444 !important; }
#noise-chart-container { flex: 1; min-height: 0; padding: 2px 4px; }
#noiseChart { width: 100% !important; height: 100% !important; display: block; }
</style>
</head>
<body>
<div id="container">
  <div id="left-col">
    <iframe id="sdr-frame" src="" title="SDR"></iframe>
    <div id="left-divider"></div>
    <iframe id="map-frame" src="" title="Node Map"></iframe>
  </div>
  <div id="divider"></div>
  <div id="right-col">
    <iframe id="rns-frame" src="" title="RNS Live"></iframe>
    <div id="noise-panel">
      <div id="noise-titlebar">
        <span>RNode Noise Floor - 5 min</span>
        <span id="noise-reading">-- dBm</span>
        <span id="noise-intrfrnc"></span>
      </div>
      <div id="noise-chart-container">
        <canvas id="noiseChart"></canvas>
      </div>
    </div>
  </div>
</div>
<script>
'use strict';
var wsHost = window.location.hostname;
var baseUrl = 'http://' + wsHost;

// Set iframe sources dynamically
document.getElementById('sdr-frame').src = baseUrl + ':8080';
document.getElementById('map-frame').src = baseUrl + ':8086';
document.getElementById('rns-frame').src = baseUrl + ':8082';

// Noise chart - init after layout via requestAnimationFrame
var MAX_POINTS = 300;
var labels    = [];
var noiseData = [];
var intrfData = [];
for (var i = 0; i < MAX_POINTS; i++) { labels.push(''); noiseData.push(null); intrfData.push(null); }

var chart = null;
requestAnimationFrame(function() {
    chart = new Chart(document.getElementById('noiseChart').getContext('2d'), {
        type: 'line',
        data: { labels: labels, datasets: [
            { label:'Noise Fl.', data:noiseData, borderColor:'#00cc66',
              borderWidth:1.5, pointRadius:0, tension:0.2, fill:false, spanGaps:true },
            { label:'Intrfrnc.', data:intrfData, borderColor:'rgba(0,0,0,0)',
              pointBackgroundColor:'#cc4444', pointRadius:4, showLine:false, spanGaps:false },
        ]},
        options: {
            animation:false, responsive:true, maintainAspectRatio:false,
            plugins:{ legend:{display:false}, tooltip:{enabled:false} },
            scales:{
                x:{ display:false },
                y:{ min:-120, max:-80,
                    ticks:{ color:'#555', font:{size:9,family:'monospace'},
                            stepSize:10, callback:function(v){ return v+' dBm'; } },
                    grid:{ color:'#1a1a1a' }, border:{ color:'#222233' } },
            },
        },
    });
});

var noiseWs;
function connectNoise() {
    noiseWs = new WebSocket('ws://' + wsHost + ':8083');
    noiseWs.onclose = function() { setTimeout(connectNoise, 3000); };
    noiseWs.onmessage = function(e) {
        var msg; try { msg = JSON.parse(e.data); } catch(ex) { return; }
        if (msg.type !== 'noise_sample') return;
        noiseData.shift(); noiseData.push(msg.noise_db);
        intrfData.shift(); intrfData.push(msg.intrfrnc_db);
        labels.shift(); labels.push('');
        if (chart) chart.update('none');
        document.getElementById('noise-reading').textContent =
            msg.noise_db !== null ? msg.noise_db + ' dBm' : '-- dBm';
        var el = document.getElementById('noise-intrfrnc');
        el.textContent = msg.intrfrnc_db !== null ? 'Intrfrnc: '+msg.intrfrnc_db+' dBm' : '';
    };
}
connectNoise();

// Vertical divider
var divider=document.getElementById('divider');
var leftCol=document.getElementById('left-col');
var rightCol=document.getElementById('right-col');
var sdrFrame=document.getElementById('sdr-frame');
var rnsFrame=document.getElementById('rns-frame');
var mapFrame=document.getElementById('map-frame');
var container=document.getElementById('container');
var dragging=false;
divider.addEventListener('mousedown',function(e){ dragging=true; sdrFrame.style.pointerEvents='none'; rnsFrame.style.pointerEvents='none'; mapFrame.style.pointerEvents='none'; e.preventDefault(); });
document.addEventListener('mousemove',function(e){ if(!dragging)return; var rect=container.getBoundingClientRect(); var pct=Math.min(80,Math.max(30,((e.clientX-rect.left)/rect.width)*100)); leftCol.style.width=pct+'%'; rightCol.style.flex='none'; rightCol.style.width=(100-pct)+'%'; });
document.addEventListener('mouseup',function(){ if(!dragging)return; dragging=false; sdrFrame.style.pointerEvents=''; rnsFrame.style.pointerEvents=''; mapFrame.style.pointerEvents=''; });

// Horizontal divider (SDR/map)
var leftDivider=document.getElementById('left-divider');
var leftDragging=false;
leftDivider.addEventListener('mousedown',function(e){ leftDragging=true; sdrFrame.style.pointerEvents='none'; mapFrame.style.pointerEvents='none'; e.preventDefault(); });
document.addEventListener('mousemove',function(e){ if(!leftDragging)return; var rect=leftCol.getBoundingClientRect(); var pct=Math.min(70,Math.max(15,((e.clientY-rect.top)/rect.height)*100)); sdrFrame.style.height=pct+'%'; mapFrame.style.flex='none'; mapFrame.style.height=(100-pct)+'%'; });
document.addEventListener('mouseup',function(){ if(!leftDragging)return; leftDragging=false; sdrFrame.style.pointerEvents=''; mapFrame.style.pointerEvents=''; });
</script>
</body>
</html>
"""

async def combined_http_handler(reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter):
    try:
        await reader.read(4096)
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(COMBINED_HTML)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        writer.write(header + COMBINED_HTML)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()

# ── Reticulum + LXMF initialisation (must run in main thread) ────────────────

def init_rns_lxmf():
    global _router, _local_dest

    log.info("Starting Reticulum...")
    RNS.Reticulum()
    log.info("Reticulum ready")

    RNS.Transport.register_announce_handler(AnnounceHandler())
    log.info("Announce handler registered")

    os.makedirs(STORAGE_PATH, exist_ok=True)
    _router = LXMF.LXMRouter(
        storagepath=STORAGE_PATH,
        autopeer=True,
    )

    identity_path = Path(STORAGE_PATH) / "identity"
    if identity_path.exists():
        identity = RNS.Identity.from_file(str(identity_path))
        log.info("Loaded existing identity")
    else:
        identity = RNS.Identity()
        identity.to_file(str(identity_path))
        log.info("Created new identity")

    _local_dest = _router.register_delivery_identity(
        identity,
        display_name=DISPLAY_NAME,
    )
    _router.register_delivery_callback(lxmf_delivery)

    log.info("LXMF ready — our address: %s", _local_dest.hash.hex())
    log.info("Display name: %s  Announce interval: %ds",
             DISPLAY_NAME, ANNOUNCE_INTERVAL)

# ── Terminal WebSocket handler ────────────────────────────────────────────────

async def terminal_ws_handler(ws):
    """
    Full PTY terminal over WebSocket for xterm.js.
    Spawns a bash login shell on a real pseudo-terminal so that
    ncurses apps (cmatrix, htop, vim, …) and sudo work correctly.
    Protocol:
      browser → server : raw bytes (keystrokes) OR JSON resize message
      server → browser : raw bytes (PTY output)
    Security note: only expose on the hotspot interface (trusted clients).
    """
    log.info("Terminal WS connected: %s", ws.remote_address)
    loop = asyncio.get_event_loop()

    # Allocate PTY pair
    master_fd, slave_fd = pty.openpty()

    # Default window size — browser will send a resize immediately
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", 24, 80, 0, 0))

    env = {
        "TERM":      "xterm-256color",
        "HOME":      "/home/user",
        "USER":      "user",
        "LOGNAME":   "user",
        "SHELL":     "/bin/bash",
        "PATH":      "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG":      "en_GB.UTF-8",
    }

    def _setup_child():
        os.setsid()
        # Set slave PTY as controlling terminal for this process
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "--login",
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        env=env,
        cwd="/home/user",
        preexec_fn=_setup_child,
    )
    os.close(slave_fd)   # parent doesn't need the slave end

    # Make master non-blocking so the reader callback doesn't block the loop
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    pty_queue: asyncio.Queue = asyncio.Queue()

    def _on_pty_readable():
        try:
            data = os.read(master_fd, 4096)
            if data:
                loop.call_soon_threadsafe(pty_queue.put_nowait, data)
        except OSError:
            loop.call_soon_threadsafe(pty_queue.put_nowait, None)

    loop.add_reader(master_fd, _on_pty_readable)

    stop = asyncio.Event()

    async def _pty_to_ws():
        """Forward PTY output to the browser."""
        while not stop.is_set():
            try:
                data = await asyncio.wait_for(pty_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if data is None:
                break
            try:
                await ws.send(data)
            except Exception:
                break
        stop.set()

    async def _ws_to_pty():
        """Forward browser keystrokes / resize messages to the PTY."""
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    try:
                        os.write(master_fd, msg)
                    except OSError:
                        break
                elif isinstance(msg, str):
                    # JSON control message (resize only for now)
                    try:
                        ctrl = json.loads(msg)
                        if ctrl.get("type") == "resize":
                            cols = max(1, int(ctrl.get("cols", 80)))
                            rows = max(1, int(ctrl.get("rows", 24)))
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                        struct.pack("HHHH", rows, cols, 0, 0))
                    except (json.JSONDecodeError, ValueError):
                        pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            stop.set()   # signal _pty_to_ws to exit

    try:
        await asyncio.gather(_pty_to_ws(), _ws_to_pty())
    finally:
        loop.remove_reader(master_fd)
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        await proc.wait()
        log.info("Terminal WS closed: %s", ws.remote_address)

# ── Terminal page (port 8087) ─────────────────────────────────────────────────

TERM_PAGE_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>raspi20 - Terminal</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { background: #000; width: 100%; height: 100%; overflow: hidden; font-family: monospace; }
#titlebar {
    height: 32px;
    background: #0d0d0d;
    border-bottom: 1px solid #1a1a1a;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 12px;
    flex-shrink: 0;
}
#titlebar span { font-size: 10px; color: #555; text-transform: uppercase; letter-spacing: 0.06em; }
#conn-status { font-size: 10px; color: #444; }
#term-container {
    position: absolute;
    top: 32px; left: 0; right: 0; bottom: 0;
    padding: 4px 6px;
}
</style>
</head>
<body>
<div id="titlebar">
    <span>Terminal - raspi20</span>
    <span id="conn-status">connecting...</span>
</div>
<div id="term-container"></div>
<script>
'use strict';
var wsHost   = window.location.hostname;
var baseUrl  = 'http://' + wsHost;

// Inject xterm static files dynamically
var _css = document.createElement('link');
_css.rel = 'stylesheet';
_css.href = baseUrl + ':8082/xterm.css';
document.head.appendChild(_css);

function _loadScript(src, cb) {
    var s = document.createElement('script');
    s.src = src;
    s.onload = cb;
    document.head.appendChild(s);
}
_loadScript(baseUrl + ':8082/xterm.js', function() {
    _loadScript(baseUrl + ':8082/addon-fit.js', initTerminal);
});

function initTerminal() {

var statusEl = document.getElementById('conn-status');

var term = new Terminal({
    cursorBlink:  true,
    fontSize:     13,
    fontFamily:   '"DejaVu Sans Mono", "Courier New", monospace',
    theme: { background:'#000000', foreground:'#00ff00', cursor:'#00ff00',
             selectionBackground:'#004400' },
    scrollback:   1000,
});
var fitAddon = new FitAddon.FitAddon();
term.loadAddon(fitAddon);
term.open(document.getElementById('term-container'));
setTimeout(function() { fitAddon.fit(); sendResize(); }, 100);
new ResizeObserver(function() { fitAddon.fit(); sendResize(); })
    .observe(document.getElementById('term-container'));
window.addEventListener('resize', function() { fitAddon.fit(); sendResize(); });

var termWs;
function sendResize() {
    if (termWs && termWs.readyState === WebSocket.OPEN)
        termWs.send(JSON.stringify({type:'resize', cols:term.cols, rows:term.rows}));
}
function connect() {
    termWs = new WebSocket('ws://' + wsHost + ':8085');
    termWs.binaryType = 'arraybuffer';
    termWs.onopen = function() {
        statusEl.textContent = 'connected';
        statusEl.style.color = '#006600';
        sendResize();
    };
    termWs.onclose = function() {
        statusEl.textContent = 'reconnecting...';
        statusEl.style.color = '#664400';
        term.write('\\r\\n\\x1b[33m[disconnected - reconnecting in 3s]\\x1b[0m\\r\\n');
        setTimeout(connect, 3000);
    };
    termWs.onerror = function() {
        statusEl.textContent = 'error';
        statusEl.style.color = '#aa3333';
    };
    termWs.onmessage = function(e) {
        if (e.data instanceof ArrayBuffer) term.write(new Uint8Array(e.data));
    };
}
term.onData(function(d) {
    if (termWs && termWs.readyState === WebSocket.OPEN)
        termWs.send(new TextEncoder().encode(d));
});
connect();
</script>
</body>
</html>
"""

async def term_page_handler(reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
    try:
        await reader.read(4096)
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(TERM_PAGE_HTML)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        writer.write(header + TERM_PAGE_HTML)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global _loop
    _loop = asyncio.get_event_loop()

    db_init()
    db_load_canonical_hashes()      # rebuild dedup map from history on startup

    log.info("╔══════════════════════════════════════════════╗")
    log.info("║  RNS Web Bridge                              ║")
    log.info("╠══════════════════════════════════════════════╣")
    log.info("║  UI:        http://10.42.0.1:%d             ║", HTTP_PORT)
    log.info("║  WebSocket: ws://10.42.0.1:%d               ║", WS_PORT)
    log.info("║  Combined:  http://10.42.0.1:%d             ║", COMBINED_PORT)
    log.info("║  Terminal:  ws://10.42.0.1:%d               ║", TERMINAL_PORT)
    log.info("║  Term page: http://10.42.0.1:%d             ║", TERM_PAGE_PORT)
    log.info("╚══════════════════════════════════════════════╝")

    http_server     = await asyncio.start_server(http_handler,          "0.0.0.0", HTTP_PORT)
    ws_server       = await ws_serve(ws_handler,                        "0.0.0.0", WS_PORT)
    combined_server   = await asyncio.start_server(combined_http_handler, "0.0.0.0", COMBINED_PORT)
    terminal_server   = await ws_serve(terminal_ws_handler,               "0.0.0.0", TERMINAL_PORT)
    term_page_server  = await asyncio.start_server(term_page_handler,     "0.0.0.0", TERM_PAGE_PORT)

    async with http_server, ws_server, combined_server, terminal_server, term_page_server:
        await asyncio.gather(
            http_server.serve_forever(),
            ws_server.wait_closed(),
            combined_server.serve_forever(),
            terminal_server.wait_closed(),
            term_page_server.serve_forever(),
            auto_announce_loop(),
            noise_poll_loop(),
        )

if __name__ == "__main__":
    init_rns_lxmf()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
