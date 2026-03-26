#!/usr/bin/env python3
"""
landing-server.py — raspi20
Serves landing.html and notes.html on port 80.
Handles:
  GET  /           -> landing.html
  GET  /notes.html -> notes.html
  GET  /notes      -> plain text quick note
  POST /notes      -> save plain text quick note
  GET  /api/notes  -> JSON array of all Keep-style notes
  POST /api/notes  -> create a new note
  PUT  /api/notes/<id>    -> update a note
  DELETE /api/notes/<id>  -> delete a note
  POST /shutdown   -> system shutdown
"""

import asyncio
import json
import logging
import subprocess
import time
import uuid
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("landing")

HTTP_HOST  = "0.0.0.0"
HTTP_PORT  = 80
HOME       = Path.home()
SERVE_DIR  = HOME   # files served from ~/

NOTES_TXT  = HOME / "notes.txt"    # quick notepad
NOTES_JSON = HOME / "notes.json"   # Keep-style notes


# ── Notes helpers ─────────────────────────────────────────────────────────────

def load_notes() -> list:
    if NOTES_JSON.exists():
        try:
            return json.loads(NOTES_JSON.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

def save_notes(notes: list):
    NOTES_JSON.write_text(
        json.dumps(notes, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def resp(writer, status: str, ctype: str, body: bytes):
    header = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    writer.write(header + body)

def ok_html(writer, path: Path):
    try:
        body = path.read_bytes()
        resp(writer, "200 OK", "text/html; charset=utf-8", body)
    except FileNotFoundError:
        resp(writer, "404 Not Found", "text/plain", b"File not found")

def ok_json(writer, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    resp(writer, "200 OK", "application/json; charset=utf-8", body)

def ok_text(writer, text: str):
    resp(writer, "200 OK", "text/plain; charset=utf-8", text.encode("utf-8"))

def not_found(writer):
    resp(writer, "404 Not Found", "text/plain", b"Not found")

def get_body(raw: str) -> str:
    idx = raw.find("\r\n\r\n")
    return raw[idx + 4:] if idx >= 0 else ""

# ── Request handler ───────────────────────────────────────────────────────────

async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        raw = (await reader.read(8192)).decode("utf-8", errors="replace")
        if not raw:
            return

        first = raw.split("\r\n")[0].split()
        if len(first) < 2:
            return
        method = first[0]
        path   = first[1].split("?")[0]

        log.info("%s %s", method, path)

        # ── Static HTML files ─────────────────────────────────────────────
        if method == "GET" and path in ("/", "/landing.html"):
            ok_html(writer, SERVE_DIR / "landing.html")

        elif method == "GET" and path == "/notes.html":
            ok_html(writer, SERVE_DIR / "notes.html")

        # ── Quick notepad (single text file) ─────────────────────────────
        elif method == "GET" and path == "/notes":
            text = NOTES_TXT.read_text(encoding="utf-8") if NOTES_TXT.exists() else ""
            ok_text(writer, text)

        elif method == "POST" and path == "/notes":
            body = get_body(raw)
            NOTES_TXT.write_text(body, encoding="utf-8")
            ok_text(writer, "ok")

        # ── Keep-style notes API ──────────────────────────────────────────
        elif method == "GET" and path == "/api/notes":
            ok_json(writer, load_notes())

        elif method == "POST" and path == "/api/notes":
            body = get_body(raw).strip()
            try:
                data = json.loads(body)
            except Exception:
                data = {"content": body}
            note = {
                "id":      str(uuid.uuid4()),
                "title":   data.get("title", ""),
                "content": data.get("content", ""),
                "ts":      time.time(),
            }
            notes = load_notes()
            notes.append(note)
            save_notes(notes)
            ok_json(writer, note)

        elif method == "PUT" and path.startswith("/api/notes/"):
            note_id = path[len("/api/notes/"):]
            body = get_body(raw).strip()
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            notes = load_notes()
            updated = None
            for n in notes:
                if n["id"] == note_id:
                    n["title"]   = data.get("title",   n.get("title", ""))
                    n["content"] = data.get("content", n.get("content", ""))
                    n["ts"]      = time.time()
                    updated = n
                    break
            if updated:
                save_notes(notes)
                ok_json(writer, updated)
            else:
                not_found(writer)

        elif method == "DELETE" and path.startswith("/api/notes/"):
            note_id = path[len("/api/notes/"):]
            notes = load_notes()
            before = len(notes)
            notes = [n for n in notes if n["id"] != note_id]
            save_notes(notes)
            if len(notes) < before:
                ok_json(writer, {"deleted": note_id})
            else:
                not_found(writer)

        # ── Shutdown ──────────────────────────────────────────────────────
        elif method == "POST" and path == "/shutdown":
            ok_text(writer, "shutting down")
            await writer.drain()
            log.info("Shutdown requested")
            subprocess.Popen(["sudo", "shutdown", "-h", "now"])

        else:
            not_found(writer)

        await writer.drain()

    except Exception as e:
        log.error("Handler error: %s", e)
    finally:
        writer.close()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    server = await asyncio.start_server(handle, HTTP_HOST, HTTP_PORT)
    log.info("Landing server on http://%s:%d", HTTP_HOST, HTTP_PORT)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
