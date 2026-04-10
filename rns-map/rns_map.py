#!/usr/bin/env python3
"""
rns_map.py  -  Reticulum live network map backend.

Uses four aspect-filtered announce handlers so each type is identified
correctly. Stores nodes and per-minute activity buckets in SQLite.

HTTP + WS served on PORT 8085.
  GET  /          -> static/index.html
  GET  /ws        -> WebSocket upgrade
  GET  /activity  -> JSON array of activity buckets (last 24h)
  POST /reset     -> wipe nodes table and broadcast reset event
"""

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path

import RNS
from aiohttp import web
import aiohttp

# -- Configuration -------------------------------------------------------------

BASE_DIR   = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH    = BASE_DIR / "nodes.db"
RNS_CONFIG = BASE_DIR / "reticulum-config"
PORT       = 8086

BUCKET_SECS  = 60           # 1-minute buckets
MAX_BUCKETS  = 24 * 60      # 24 hours of history

# -- Shared state --------------------------------------------------------------

_loop           = None
_ws_clients     = set()
_nodes          = {}
_db_lock        = threading.Lock()
_announce_count = 0

# -- Database ------------------------------------------------------------------

def db_init():
    with sqlite3.connect(str(DB_PATH)) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                hash       TEXT PRIMARY KEY,
                name       TEXT,
                app_type   TEXT,
                hops       INTEGER,
                first_seen REAL,
                last_seen  REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                bucket_ts  INTEGER,
                app_type   TEXT,
                count      INTEGER DEFAULT 0,
                PRIMARY KEY (bucket_ts, app_type)
            )
        """)
        c.commit()

def db_load():
    with sqlite3.connect(str(DB_PATH)) as c:
        for row in c.execute(
            "SELECT hash, name, app_type, hops, first_seen, last_seen FROM nodes"
        ):
            _nodes[row[0]] = {
                "hash":       row[0],
                "name":       row[1],
                "app_type":   row[2],
                "hops":       row[3],
                "first_seen": row[4],
                "last_seen":  row[5],
            }

def db_upsert_node(node):
    with _db_lock:
        with sqlite3.connect(str(DB_PATH)) as c:
            c.execute("""
                INSERT OR REPLACE INTO nodes
                (hash, name, app_type, hops, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                node["hash"], node["name"], node["app_type"],
                node["hops"], node["first_seen"], node["last_seen"],
            ))
            c.commit()

def db_record_activity(app_type: str):
    bucket = int(time.time() // BUCKET_SECS) * BUCKET_SECS
    with _db_lock:
        with sqlite3.connect(str(DB_PATH)) as c:
            c.execute("""
                INSERT INTO activity (bucket_ts, app_type, count)
                VALUES (?, ?, 1)
                ON CONFLICT(bucket_ts, app_type) DO UPDATE SET count = count + 1
            """, (bucket, app_type))
            # Prune old buckets
            cutoff = bucket - MAX_BUCKETS * BUCKET_SECS
            c.execute("DELETE FROM activity WHERE bucket_ts < ?", (cutoff,))
            c.commit()

def db_get_activity():
    """Return activity buckets as list of dicts for last 24h."""
    cutoff = int(time.time()) - MAX_BUCKETS * BUCKET_SECS
    with sqlite3.connect(str(DB_PATH)) as c:
        rows = c.execute("""
            SELECT bucket_ts, app_type, count FROM activity
            WHERE bucket_ts >= ?
            ORDER BY bucket_ts ASC
        """, (cutoff,)).fetchall()
    # Pivot into {bucket_ts: {app_type: count, ...}, ...}
    pivot = {}
    for ts, app_type, count in rows:
        if ts not in pivot:
            pivot[ts] = {"ts": ts, "lxmf": 0, "nomadnet": 0,
                         "propagation": 0, "audio": 0}
        if app_type in pivot[ts]:
            pivot[ts][app_type] = count
    return sorted(pivot.values(), key=lambda x: x["ts"])

def db_reset_nodes():
    with _db_lock:
        with sqlite3.connect(str(DB_PATH)) as c:
            c.execute("DELETE FROM nodes")
            c.commit()

# -- Name parsing --------------------------------------------------------------

def _parse_name(app_data, fallback: str) -> str:
    if not app_data:
        return fallback
    # Try msgpack first (LXMF delivery format: [name_bytes, stamp_cost_or_nil])
    try:
        import msgpack
        unpacked = msgpack.unpackb(app_data, raw=True)
        if isinstance(unpacked, (list, tuple)) and len(unpacked) >= 1:
            first = unpacked[0]
            if isinstance(first, bytes) and len(first) >= 1:
                name = first.decode("utf-8").strip()
                if name:
                    return name
    except Exception:
        pass
    # Try plain UTF-8 (propagation nodes, nomadnet plain name)
    try:
        decoded = app_data.decode("utf-8").strip()
        if not decoded:
            return fallback
        if decoded.startswith("{") and "server_name" in decoded:
            return json.loads(decoded).get("server_name", fallback)
        if decoded.isprintable() and len(decoded) >= 1:
            return decoded
    except Exception:
        pass
    return fallback

# -- Hop count -----------------------------------------------------------------

def _get_hops(destination_hash: bytes, announce_packet_hash=None) -> int:
    """
    Query rns-web journal for hop count logged in format:
    "Announce: <hash12>  <name>  hops=N"
    Falls back to 1 if not found.
    """
    import subprocess, re
    hash12 = destination_hash.hex()[:12]
    try:
        result = subprocess.run(
            ["journalctl", "-u", "rns-web", "-n", "500", "--no-pager",
             "--output=cat"],
            capture_output=True, text=True, timeout=2
        )
        pattern = re.compile(
            r"Announce: " + re.escape(hash12) + r"[ \t]+\S+[ \t]+hops=([0-9]+)"
        )
        matches = pattern.findall(result.stdout)
        if matches:
            return min(max(int(matches[-1]), 0), 6)
    except Exception as e:
        print("[rns-map] _get_hops journal error: {}".format(e), flush=True)
    return 1

# -- Core processor ------------------------------------------------------------

def _process(app_type: str, destination_hash: bytes, announced_identity,
             app_data, kwargs):
    global _announce_count
    try:
        hash_hex  = destination_hash.hex()
        now       = time.time()
        name      = _parse_name(app_data, hash_hex[:12])
        hops      = min(max(_get_hops(destination_hash,
                            kwargs.get("announce_packet_hash")), 0), 6)
        _announce_count += 1

        node = {
            "hash":       hash_hex,
            "name":       name,
            "app_type":   app_type,
            "hops":       hops,
            "first_seen": _nodes.get(hash_hex, {}).get("first_seen", now),
            "last_seen":  now,
        }
        _nodes[hash_hex] = node

        threading.Thread(target=db_upsert_node,   args=(dict(node),), daemon=True).start()
        threading.Thread(target=db_record_activity, args=(app_type,),  daemon=True).start()

        event = {"type": "announce", "node": dict(node), "ts": now}
        loop_ok = _loop is not None and _loop.is_running()
        print("[rns-map] #{} {} \"{}\" hops={} ws={}".format(
            _announce_count, app_type, name, hops, len(_ws_clients)), flush=True)

        if loop_ok:
            fut = asyncio.run_coroutine_threadsafe(_broadcast(event), _loop)
            try:
                fut.result(timeout=2)
            except Exception as e:
                print("[rns-map] broadcast error: {}".format(e), flush=True)

    except Exception as e:
        print("[rns-map] _process error: {}".format(e), flush=True)

# -- Announce handlers ---------------------------------------------------------

class _LXMFDeliveryHandler:
    aspect_filter = "lxmf.delivery"
    def received_announce(self, destination_hash, announced_identity, app_data, **kwargs):
        _process("lxmf", destination_hash, announced_identity, app_data, kwargs)

class _LXMFPropHandler:
    aspect_filter = "lxmf.propagation"
    def received_announce(self, destination_hash, announced_identity, app_data, **kwargs):
        _process("propagation", destination_hash, announced_identity, app_data, kwargs)

class _NomadHandler:
    aspect_filter = "nomadnetwork.node"
    def received_announce(self, destination_hash, announced_identity, app_data, **kwargs):
        _process("nomadnet", destination_hash, announced_identity, app_data, kwargs)

class _AudioHandler:
    aspect_filter = "call.audio"
    def received_announce(self, destination_hash, announced_identity, app_data, **kwargs):
        _process("audio", destination_hash, announced_identity, app_data, kwargs)

# -- WebSocket broadcast -------------------------------------------------------

async def _broadcast(event: dict):
    global _ws_clients
    if not _ws_clients:
        return
    msg  = json.dumps(event)
    dead = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_str(msg)
        except Exception as e:
            print("[rns-map] ws send error: {}".format(e), flush=True)
            dead.add(ws)
    _ws_clients -= dead

# -- HTTP handlers -------------------------------------------------------------

async def handle_ws(request):
    global _ws_clients
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _ws_clients.add(ws)
    print("[rns-map] WS connected, total={}".format(len(_ws_clients)), flush=True)
    try:
        await ws.send_str(json.dumps({
            "type":  "state",
            "nodes": list(_nodes.values()),
        }))
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        _ws_clients.discard(ws)
        print("[rns-map] WS disconnected, total={}".format(len(_ws_clients)), flush=True)
    return ws

async def handle_index(request):
    return web.FileResponse(STATIC_DIR / "index.html")

async def handle_activity(request):
    """Return persisted activity buckets as JSON."""
    data = await asyncio.get_event_loop().run_in_executor(None, db_get_activity)
    return web.Response(
        text=json.dumps(data),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )

async def handle_reset(request):
    """Wipe nodes table, clear in-memory node dict, broadcast reset."""
    global _nodes
    _nodes = {}
    await asyncio.get_event_loop().run_in_executor(None, db_reset_nodes)
    await _broadcast({"type": "reset"})
    print("[rns-map] Node DB reset by user request", flush=True)
    return web.Response(text='{"ok":true}', content_type="application/json")

# -- Main ----------------------------------------------------------------------

async def main():
    global _loop
    _loop = asyncio.get_running_loop()

    db_init()
    db_load()
    print("[rns-map] Loaded {} nodes from DB".format(len(_nodes)), flush=True)

    RNS.Reticulum()   # shared instance - rnsd owns the RNode
    for handler in (_LXMFDeliveryHandler(), _LXMFPropHandler(),
                    _NomadHandler(), _AudioHandler()):
        RNS.Transport.register_announce_handler(handler)
    print("[rns-map] Announce handlers registered", flush=True)

    app = web.Application()
    app.router.add_get ("/",         handle_index)
    app.router.add_get ("/ws",       handle_ws)
    app.router.add_get ("/activity", handle_activity)
    app.router.add_post("/reset",    handle_reset)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print("[rns-map] Serving on http://0.0.0.0:{}".format(PORT), flush=True)

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
