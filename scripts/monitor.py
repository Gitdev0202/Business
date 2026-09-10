"""
PS5 Marktplaats monitor.

Haalt nieuwe advertenties op uit de Marktplaats-categorie
"Spelcomputers | Sony PlayStation 5" (id 2954), filtert op vandaag geplaatst
+ minimumprijs, en stuurt alleen nog-niet-eerder-geziene advertenties naar
Discord via een webhook. Gezien-ids worden bijgehouden in state/seen.json
zodat elke run alleen de incrementele (nieuwe) advertenties meldt.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# --- Configuratie ---------------------------------------------------------

L1_CATEGORY_ID = 356   # Spelcomputers en Games
L2_CATEGORY_ID = 2954  # Spelcomputers | Sony PlayStation 5

SEARCH_URL = "https://www.marktplaats.nl/lrp/api/search"
PAGE_SIZE = 100
MAX_PAGES = 5  # ruim genoeg voor een dag vol advertenties in deze categorie

MIN_PRICE_EUR = float(os.environ.get("MIN_PRICE_EUR", "250"))
MIN_PRICE_CENTS = int(MIN_PRICE_EUR * 100)

# priceType-waarden die altijd meetellen, ongeacht prijs:
# FAST_BID   = "Bieden" (open bieden, geen vraagprijs, technisch 0 cent)
# SEE_DESCRIPTION = prijs staat in de omschrijving (onbekend, dus liever
#                    te veel dan te weinig tonen)
ALWAYS_INCLUDE_PRICE_TYPES = {"FAST_BID", "SEE_DESCRIPTION"}

TITLE_PATTERN = re.compile(r"ps ?5|playstation ?5", re.IGNORECASE)

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state", "seen.json")
PRUNE_AFTER_DAYS = 21  # oude entries opruimen zodat het bestand niet oneindig groeit

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# --- Marktplaats -----------------------------------------------------------

def fetch_page(offset: int) -> dict:
    params = (
        f"l1CategoryId={L1_CATEGORY_ID}&l2CategoryId={L2_CATEGORY_ID}"
        f"&limit={PAGE_SIZE}&offset={offset}"
        f"&sortBy=SORT_INDEX&sortOrder=DECREASING"
    )
    url = f"{SEARCH_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Kon Marktplaats niet bereiken na 3 pogingen: {last_error}")


def fetch_all_listings() -> list:
    listings = []
    for page in range(MAX_PAGES):
        data = fetch_page(offset=page * PAGE_SIZE)
        page_listings = data.get("listings", [])
        listings.extend(page_listings)
        if len(page_listings) < PAGE_SIZE:
            break  # laatste pagina bereikt
    return listings


# --- Filtering ---------------------------------------------------------

def is_posted_today(listing: dict) -> bool:
    return listing.get("date") == "Vandaag"


def matches_title(listing: dict) -> bool:
    return bool(TITLE_PATTERN.search(listing.get("title", "")))


def passes_price_filter(listing: dict) -> bool:
    price_info = listing.get("priceInfo", {})
    price_type = price_info.get("priceType")
    price_cents = price_info.get("priceCents", 0)

    if price_type in ALWAYS_INCLUDE_PRICE_TYPES:
        return True
    return price_cents >= MIN_PRICE_CENTS


def is_relevant(listing: dict) -> bool:
    return is_posted_today(listing) and matches_title(listing) and passes_price_filter(listing)


# --- State (dedup) -------------------------------------------------------

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def prune_state(state: dict) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=PRUNE_AFTER_DAYS)
    pruned = {}
    for item_id, entry in state.items():
        try:
            first_seen = datetime.fromisoformat(entry["firstSeen"])
        except (KeyError, ValueError, TypeError):
            continue  # kapotte entry, laten vallen
        if first_seen >= cutoff:
            pruned[item_id] = entry
    return pruned


# --- Discord ---------------------------------------------------------------

def fmt_euro(cents: int) -> str:
    # 1234.5 -> "1,234.50" -> swap separators -> "1.234,50" (NL-notatie)
    us_style = f"{cents / 100:,.2f}"
    nl_style = us_style.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€{nl_style}"


def format_price(listing: dict) -> str:
    price_info = listing.get("priceInfo", {})
    price_type = price_info.get("priceType")
    price_cents = price_info.get("priceCents", 0)

    if price_type == "FAST_BID":
        return "Bieden (geen vraagprijs)"
    if price_type == "SEE_DESCRIPTION":
        return "Prijs in omschrijving"
    if price_type == "MIN_BID":
        return f"Bieden vanaf {fmt_euro(price_cents)}"
    if price_type == "FIXED":
        return f"{fmt_euro(price_cents)} (vraagprijs)"
    return fmt_euro(price_cents)


def listing_url(listing: dict) -> str:
    return f"https://www.marktplaats.nl{listing.get('vipUrl', '')}"


def listing_image(listing: dict) -> str | None:
    images = listing.get("imageUrls") or []
    if not images:
        return None
    url = images[0]
    if url.startswith("//"):
        url = "https:" + url
    return url


def build_embed(listing: dict) -> dict:
    city = listing.get("location", {}).get("cityName", "Onbekende locatie")
    embed = {
        "title": listing.get("title", "PS5 advertentie")[:256],
        "url": listing_url(listing),
        "description": f"{format_price(listing)} — {city}",
        "color": 0x2ECC71,
    }
    image = listing_image(listing)
    if image:
        embed["thumbnail"] = {"url": image}
    return embed


def send_discord_notifications(listings: list) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL ontbreekt (environment variable / secret).")

    chunk_size = 10  # Discord staat max. 10 embeds per bericht toe
    for i in range(0, len(listings), chunk_size):
        chunk = listings[i:i + chunk_size]
        payload = {
            "username": "PS5 Marktplaats Monitor",
            "content": (
                f"🎮 {len(chunk)} nieuwe PS5-advertentie(s) gevonden!"
                if i == 0 else None
            ),
            "embeds": [build_embed(listing) for listing in chunk],
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Discord webhook gaf status {exc.code}: {details}") from exc

        if i + chunk_size < len(listings):
            time.sleep(1)  # simpele rate-limit bescherming


# --- Main --------------------------------------------------------------

def main() -> int:
    state = load_state()

    try:
        listings = fetch_all_listings()
    except RuntimeError as exc:
        print(f"[FOUT] {exc}", file=sys.stderr)
        return 1

    new_matches = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for listing in listings:
        item_id = listing.get("itemId")
        if not item_id or item_id in state:
            continue
        if not is_relevant(listing):
            continue

        new_matches.append(listing)
        state[item_id] = {"firstSeen": now_iso, "title": listing.get("title", "")}

    print(
        f"Opgehaald: {len(listings)} advertenties uit categorie PS5. "
        f"Nieuw en relevant: {len(new_matches)}."
    )

    if new_matches:
        send_discord_notifications(new_matches)
        for listing in new_matches:
            print(f"  -> {listing.get('title')} | {format_price(listing)} | {listing_url(listing)}")

    state = prune_state(state)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
