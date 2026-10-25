#!/usr/bin/env python3
"""textsize-inline.py - copy textsize.js into the pages whose server has no
route for it: sdr-index.html (compiled into sdr-ws, rebuild after) and
rns-map/static/index.html (+ rnsmap-zoomfix.js). Rerun after editing
textsize.js; the block between the markers is replaced each time."""
import re
from pathlib import Path

D = Path(__file__).resolve().parent
BEGIN, END = "<!-- textsize.js begin -->", "<!-- textsize.js end -->"
JS = (D / "textsize.js").read_text()

for page, extra in (("sdr-index.html", ""),
                    ("rns-map/static/index.html", (D / "rnsmap-zoomfix.js").read_text())):
    p = D / page
    s = p.read_text()
    s = re.sub(r"\n?" + re.escape(BEGIN) + ".*?" + re.escape(END), "", s, flags=re.S)
    assert s.count("</body>") == 1, page
    block = BEGIN + "\n<script>\n" + JS + extra + "</script>\n" + END + "\n"
    p.write_text(s.replace("</body>", block + "</body>"))
    print("updated", page)
