"""
Eenmalige diagnostische probe (GEEN monitor): checkt of AutoScout24,
Gaspedaal.nl en AutoTrack.nl scrapebaar zijn met een simpele HTTP-request
(zoals de Marktplaats-monitors dat doen), of dat ze JavaScript-rendering /
een bot-check vereisen. Print per bron:
  - HTTP-statuscode en robots.txt-status
  - content-lengte
  - of er herkenbare advertentiedata (prijzen/JSON) in de RUWE HTML zit
  - een korte inhoudsindicatie (cookiewall/captcha/lege JS-shell/data aanwezig)

Wordt alleen handmatig gedraaid (workflow_dispatch), post niets naar
Discord en schrijft geen state.
"""

import json
import re
import urllib.request
import urllib.error

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SOURCES = {
    "AutoScout24": {
        "robots": "https://www.autoscout24.nl/robots.txt",
        "page": "https://www.autoscout24.nl/lst/volkswagen/up",
    },
    "Gaspedaal.nl": {
        "robots": "https://www.gaspedaal.nl/robots.txt",
        "page": "https://www.gaspedaal.nl/volkswagen/up",
    },
    "AutoTrack.nl": {
        "robots": "https://www.autotrack.nl/robots.txt",
        "page": "https://www.autotrack.nl/volkswagen/up",
    },
}

# Signalen dat er echt server-side advertentiedata in de HTML zit.
DATA_SIGNALS = re.compile(r"€\s?\d{2,3}[.,]?\d{3}|\"price\"\s*:|application/ld\+json", re.IGNORECASE)
# Signalen van een bot-check/cookiewall i.p.v. echte content.
BLOCK_SIGNALS = re.compile(
    r"captcha|access denied|cloudflare|are you a robot|enable javascript|"
    r"just a moment|cookiewall|consent",
    re.IGNORECASE,
)


def fetch(url: str) -> tuple:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except urllib.error.URLError as exc:
        return None, f"[verbindingsfout] {exc}"


def main() -> int:
    for name, urls in SOURCES.items():
        print(f"\n=== {name} ===")

        status, body = fetch(urls["robots"])
        print(f"robots.txt: status={status}, lengte={len(body)}")
        if body:
            disallow_all = re.search(r"User-agent:\s*\*\s*\n\s*Disallow:\s*/\s*$", body, re.MULTILINE)
            print(f"  Disallow: / voor alle user-agents? {'JA' if disallow_all else 'nee (of niet gevonden)'}")

        status, body = fetch(urls["page"])
        has_data = bool(DATA_SIGNALS.search(body)) if body else False
        has_block = bool(BLOCK_SIGNALS.search(body)) if body else False
        print(f"pagina: status={status}, content-lengte={len(body)}")
        print(f"  herkenbare advertentiedata in ruwe HTML? {has_data}")
        print(f"  bot-check/cookiewall-signalen aangetroffen? {has_block}")
        if not has_data and not has_block and body:
            print("  -> geen duidelijk signaal; waarschijnlijk JS-rendering nodig (lege shell)")

        if body:
            block_match = BLOCK_SIGNALS.search(body)
            if block_match:
                ctx_start = max(0, block_match.start() - 80)
                print(f"  context rond block-signaal: ...{body[ctx_start:block_match.start() + 120]!r}...")

            ld_blocks = re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                body, re.DOTALL | re.IGNORECASE,
            )
            print(f"  aantal JSON-LD <script>-blokken gevonden: {len(ld_blocks)}")

            # Probeer één volledig listing-item te parsen en pretty-printen
            # i.p.v. een ruwe tekst-truncatie, zodat écht alle velden
            # (bouwjaar, verkoper, etc.) zichtbaar worden.
            printed_item = False
            for block in ld_blocks:
                try:
                    data = json.loads(block)
                except json.JSONDecodeError:
                    continue
                graph = data.get("@graph", [data]) if isinstance(data, dict) else data
                for node in graph:
                    item_list = (node.get("mainEntity") or {}).get("itemListElement") if isinstance(node, dict) else None
                    if item_list:
                        first_item = item_list[0].get("item")
                        print("  --- volledig eerste listing-item (pretty-printed) ---")
                        print("  " + json.dumps(first_item, indent=2, ensure_ascii=False).replace("\n", "\n  "))
                        printed_item = True
                        break
                if printed_item:
                    break
            if not printed_item:
                for i, block in enumerate(ld_blocks[:1]):
                    print(f"  --- JSON-LD blok {i} (eerste 1000 tekens, geen itemList gevonden) ---")
                    print(f"  {block.strip()[:1000]}")

            price_match = re.search(r"\"price\"\s*:\s*\"?\d+", body)
            if price_match:
                ctx_start = max(0, price_match.start() - 300)
                print(f"  --- context rond eerste 'price'-veld (600 tekens) ---")
                print(f"  {body[ctx_start:ctx_start + 600]!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
