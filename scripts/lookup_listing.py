"""
Eenmalige diagnostische lookup (GEEN monitor): haalt de ruwe gegevens van
EEN specifieke Marktplaats-advertentie op (via een link.marktplaats.nl
share-link of een gewone marktplaats.nl-URL), zodat uitgezocht kan worden
waarom een monitor een advertentie wel/niet heeft gemeld -- bv. als de
gebruiker een link post met "waarom heb ik deze niet doorgekregen".

Werkwijze:
  1. Volgt redirects (share-links zoals link.marktplaats.nl/m... wijzen
     altijd door naar de volledige, canonieke advertentie-URL).
  2. Haalt de HTML van die canonieke URL op.
  3. Probeert er gestructureerde data uit te halen: eerst JSON-LD
     (application/ld+json, zelfde aanpak als scripts/probe_sources.py
     voor AutoScout24), anders een Next.js __NEXT_DATA__-blok, anders een
     best-effort regex-fallback op zichtbare prijs/titel in de HTML.
  4. Print alles wat gevonden is, zodat het handmatig vergeleken kan
     worden met de filters/catalogus van de relevante monitor
     (scripts/monitor_cars.py, monitor_iphones.py, enz.).

Wordt alleen handmatig gedraaid (workflow_dispatch met input 'url'), post
niets naar Discord en schrijft geen state.

Gebruik: LISTING_URL=<url> python3 scripts/lookup_listing.py
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_LD_JSON_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_NEXT_DATA_PATTERN = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_PRICE_PATTERN = re.compile(r'"price(?:Cents)?"\s*:\s*"?(\d+)"?', re.IGNORECASE)


def fetch(url: str) -> tuple:
    """Haalt een URL op en volgt redirects zelf, zodat we de uiteindelijke
    canonieke URL kunnen tonen (belangrijk: share-links redirecten altijd)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            final_url = resp.geturl()
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, final_url, body
    except urllib.error.HTTPError as exc:
        return exc.code, url, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return None, url, f"[verbindingsfout] {exc}"


def extract_ld_json(html: str) -> list:
    results = []
    for block in _LD_JSON_PATTERN.findall(html):
        try:
            results.append(json.loads(block))
        except json.JSONDecodeError:
            continue
    return results


def extract_next_data(html: str) -> dict | None:
    match = _NEXT_DATA_PATTERN.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def main() -> int:
    url = os.environ.get("LISTING_URL", "").strip()
    if not url:
        print("Geen LISTING_URL meegegeven.", file=sys.stderr)
        return 1

    print(f"Opgevraagde URL: {url}")
    status, final_url, body = fetch(url)
    print(f"HTTP-status: {status}")
    print(f"Uiteindelijke (canonieke) URL na redirects: {final_url}")
    print(f"Content-lengte: {len(body)}")

    if not body:
        print("Geen HTML ontvangen -- kan niets analyseren.", file=sys.stderr)
        return 1

    title_match = _TITLE_PATTERN.search(body)
    if title_match:
        print(f"\n<title>: {title_match.group(1).strip()}")

    ld_blocks = extract_ld_json(body)
    print(f"\nAantal JSON-LD <script>-blokken gevonden: {len(ld_blocks)}")
    for i, block in enumerate(ld_blocks):
        print(f"--- JSON-LD blok {i} ---")
        print(json.dumps(block, indent=2, ensure_ascii=False)[:3000])

    next_data = extract_next_data(body)
    if next_data:
        print("\n__NEXT_DATA__ gevonden, pretty-printed (eerste 6000 tekens):")
        print(json.dumps(next_data, indent=2, ensure_ascii=False)[:6000])
    else:
        print("\nGeen __NEXT_DATA__-blok gevonden.")

    if not ld_blocks and not next_data:
        print("\nGeen gestructureerde data gevonden -- fallback: ruwe prijs-zoekactie.")
        price_match = _PRICE_PATTERN.search(body)
        if price_match:
            ctx_start = max(0, price_match.start() - 200)
            print(f"Context rond eerste price-veld: ...{body[ctx_start:price_match.start() + 300]!r}...")
        else:
            print("Ook geen 'price'-veld gevonden in de ruwe HTML.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
