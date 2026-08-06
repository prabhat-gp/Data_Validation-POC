"""
figma_palette.py
----------------
Reads the colour palette out of a Figma file via the REST API and prints it as
CSS custom properties ready to paste into globals.css.

SETUP (one time)
  1. Figma -> your avatar -> Settings -> Security -> Personal access tokens
     -> "Generate new token". Scope "File content: read" is enough.
  2. Put it in backend/.env  (NOT the repo-root .env -- that one is committed
     to git, so a token placed there would be published):

         FIGMA_TOKEN=figd_xxxxxxxxxxxxxxxxxxxxx

  3. python figma_palette.py                      # uses FIGMA_FILE_KEY below
     python figma_palette.py <file_key>           # or pass one explicitly

The file key is the segment after /design/ in the Figma URL:
  figma.com/design/<FILE_KEY>/Some-Name?node-id=...
"""

import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_HERE, ".env"))
    load_dotenv(os.path.join(os.path.dirname(_HERE), ".env"))
except ImportError:
    pass

# from the URL you shared
FIGMA_FILE_KEY = os.getenv("FIGMA_FILE_KEY", "zSkXW7BCs7dZcFSZYsZejQ")
TOKEN = os.getenv("FIGMA_TOKEN")
API = "https://api.figma.com/v1"


def get(path):
    req = urllib.request.Request(f"{API}{path}", headers={"X-Figma-Token": TOKEN})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        print(f"  HTTP {e.code} on {path}\n  {body}")
        if e.code == 403:
            print("  -> token is missing 'File content: read', or you lack access to this file")
        if e.code == 404:
            print("  -> wrong file key, or the file is not visible to this token")
        sys.exit(1)


def to_hex(c):
    r, g, b = (round(c.get(k, 0) * 255) for k in ("r", "g", "b"))
    return f"#{r:02x}{g:02x}{b:02x}"


def walk(node, out):
    """Collect every solid fill and stroke colour in the document."""
    for key in ("fills", "strokes"):
        for p in node.get(key) or []:
            if p.get("type") == "SOLID" and p.get("visible", True) and p.get("color"):
                if p.get("opacity", 1) >= 0.9:      # ignore washes/overlays
                    out.append(to_hex(p["color"]))
    for child in node.get("children") or []:
        walk(child, out)


def main():
    if not TOKEN:
        print(__doc__)
        print("  FIGMA_TOKEN is not set -- see SETUP above.")
        sys.exit(1)

    key = sys.argv[1] if len(sys.argv) > 1 else FIGMA_FILE_KEY
    print(f"Reading Figma file {key} ...\n")

    # 1) published colour styles -- these are the INTENTIONAL palette
    styles = get(f"/files/{key}/styles")
    fill_styles = [s for s in styles["meta"]["styles"] if s["style_type"] == "FILL"]
    if fill_styles:
        ids = ",".join(s["node_id"] for s in fill_styles[:100])
        nodes = get(f"/files/{key}/nodes?ids={ids}")["nodes"]
        print("PUBLISHED COLOUR STYLES (the real palette)")
        print("-" * 58)
        for s in fill_styles:
            n = nodes.get(s["node_id"], {}).get("document", {})
            fills = [f for f in (n.get("fills") or []) if f.get("type") == "SOLID"]
            if fills:
                print(f"  {to_hex(fills[0]['color'])}   {s['name']}")
    else:
        print("No published FILL styles in this file.")

    # 2) fall back to what is actually used on the canvas
    print("\nMOST-USED COLOURS ON THE CANVAS")
    print("-" * 58)
    doc = get(f"/files/{key}")["document"]
    found = []
    walk(doc, found)
    for hexv, n in Counter(found).most_common(20):
        print(f"  {hexv}   used {n}x")

    print("\nPaste the ones you want into frontend/app/globals.css as --accent, "
          "--ink, --line, etc.")


if __name__ == "__main__":
    main()
