"""
iPhone Marktplaats monitor.

Haalt nieuwe advertenties op uit de Marktplaats-categorie
"Mobiele telefoons | Apple iPhone" (id 1953), filtert op vandaag geplaatst,
model (iPhone 15/15 Plus/15 Pro/15 Pro Max/16/16 Plus/16 Pro/16 Pro Max) met
een eigen minimumprijs per model, sluit voor onderdelen/iCloud-vergrendeld/
lege-doos advertenties uit, en stuurt alleen nog-niet-eerder-geziene
advertenties naar Discord via een webhook. Gezien-ids worden bijgehouden in
state/seen_iphones.json zodat elke run alleen de incrementele (nieuwe)
advertenties meldt.
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

L1_CATEGORY_ID = 820   # Telecommunicatie
L2_CATEGORY_ID = 1953  # Mobiele telefoons | Apple iPhone

SEARCH_URL = "https://www.marktplaats.nl/lrp/api/search"
PAGE_SIZE = 100
MAX_PAGES = 5

# Locatiefilter (optioneel). Leeg laten = heel Nederland.
POSTCODE = os.environ.get("POSTCODE", "").strip()
DISTANCE_KM = os.environ.get("DISTANCE_KM", "").strip()

# priceType-waarden die altijd meetellen, ongeacht prijs (zie monitor.py
# voor uitleg over FAST_BID / SEE_DESCRIPTION).
ALWAYS_INCLUDE_PRICE_TYPES = {"FAST_BID", "SEE_DESCRIPTION"}

# Model-herkenning + minimumprijs per model (in centen). Volgorde is
# belangrijk: meest specifieke variant staat vóór de kortere, zodat
# bijvoorbeeld "iPhone 16 Pro Max" niet per ongeluk als "iPhone 16" wordt
# geclassificeerd.
MODEL_CONFIG = [
    ("iPhone 16 Pro Max", re.compile(r"iphone\s*16[\s\-]*pro[\s\-]*max", re.IGNORECASE), 55000),
    ("iPhone 16 Pro", re.compile(r"iphone\s*16[\s\-]*pro(?!\s*max)", re.IGNORECASE), 50000),
    ("iPhone 16 Plus", re.compile(r"iphone\s*16[\s\-]*plus", re.IGNORECASE), 45000),
    ("iPhone 16", re.compile(r"iphone\s*16(?!\s*pro)(?!\s*plus)", re.IGNORECASE), 35000),
    ("iPhone 15 Pro Max", re.compile(r"iphone\s*15[\s\-]*pro[\s\-]*max", re.IGNORECASE), 40000),
    ("iPhone 15 Pro", re.compile(r"iphone\s*15[\s\-]*pro(?!\s*max)", re.IGNORECASE), 35000),
    ("iPhone 15 Plus", re.compile(r"iphone\s*15[\s\-]*plus", re.IGNORECASE), 35000),
    ("iPhone 15", re.compile(r"iphone\s*15(?!\s*pro)(?!\s*plus)", re.IGNORECASE), 25000),
]

# Uitsluitingen: voor onderdelen/defect, iCloud-vergrendeld, lege doos.
BROKEN_CONDITION_VALUES = {"Niet werkend"}
BROKEN_KEYWORDS = [
    "voor onderdelen", "kapot scherm", "niet werkend", "defect",
    "voor reparatie", "accu defect", "icloud onbekend",
]
ICLOUD_LOCK_KEYWORDS = [
    "icloud vergrendeld", "icloud geblokkeerd", "activatieslot",
    "icloud locked", "vergeten icloud", "icloud account onbekend",
    "find my iphone staat aan", "geen toegang tot icloud", "activation lock",
]
EMPTY_BOX_KEYWORDS = [
    "lege doos", "alleen de doos", "alleen doosje", "leeg doosje",
    "enkel de doos", "alleen verpakking", "zonder telefoon", "zonder toestel",
]

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state", "seen_iphones.json")
PRUNE_AFTER_DAYS = 21

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
    if POSTCODE and DISTANCE_KM:
        distance_meters = int(float(DISTANCE_KM) * 1000)
        params += f"&postcode={POSTCODE}&distanceMeters={distance_meters}"
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
            break
    return listings


# --- Filtering ---------------------------------------------------------

def is_posted_today(listing: dict) -> bool:
    return listing.get("date") == "Vandaag"


def classify_model(title: str):
    """Geeft (modelnaam, minimumprijs_centen) terug, of (None, None) als
    de titel geen van de gevolgde modellen bevat."""
    for model_name, pattern, min_price_cents in MODEL_CONFIG:
        if pattern.search(title):
            return model_name, min_price_cents
    return None, None


def get_condition(listing: dict):
    for attr in listing.get("extendedAttributes", []):
        if attr.get("key") == "condition":
            return attr.get("value")
    return None


def _contains_any(text: str, keywords: list) -> bool:
    text = text.lower()
    return any(kw in text for kw in keywords)


def is_excluded(listing: dict) -> bool:
    if get_condition(listing) in BROKEN_CONDITION_VALUES:
        return True
    haystack = f"{listing.get('title', '')} {listing.get('description', '')}"
    if _contains_any(haystack, BROKEN_KEYWORDS):
        return True
    if _contains_any(haystack, ICLOUD_LOCK_KEYWORDS):
        return True
    if _contains_any(listing.get("title", ""), EMPTY_BOX_KEYWORDS):
        return True
    return False


def passes_price_filter(listing: dict, min_price_cents: int) -> bool:
    price_info = listing.get("priceInfo", {})
    price_type = price_info.get("priceType")
    price_cents = price_info.get("priceCents", 0)

    if price_type in ALWAYS_INCLUDE_PRICE_TYPES:
        return True
    return price_cents >= min_price_cents


def evaluate_listing(listing: dict):
    """Geeft modelnaam terug als de advertentie relevant is, anders None."""
    if not is_posted_today(listing):
        return None

    model_name, min_price_cents = classify_model(listing.get("title", ""))
    if model_name is None:
        return None

    if is_excluded(listing):
        return None

    if not passes_price_filter(listing, min_price_cents):
        return None

    return model_name


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
            continue
        if first_seen >= cutoff:
            pruned[item_id] = entry
    return pruned


# --- Discord ---------------------------------------------------------------

def fmt_euro(cents: int) -> str:
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


def listing_image(listing: dict):
    images = listing.get("imageUrls") or []
    if not images:
        return None
    url = images[0]
    if url.startswith("//"):
        url = "https:" + url
    return url


def build_embed(listing: dict, model_name: str) -> dict:
    city = listing.get("location", {}).get("cityName", "Onbekende locatie")
    embed = {
        "title": listing.get("title", "iPhone advertentie")[:256],
        "url": listing_url(listing),
        "description": f"**{model_name}** — {format_price(listing)} — {city}",
        "color": 0x3498DB,
    }
    image = listing_image(listing)
    if image:
        embed["thumbnail"] = {"url": image}
    return embed


def send_discord_notifications(matches: list) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL ontbreekt (environment variable / secret).")

    chunk_size = 10
    for i in range(0, len(matches), chunk_size):
        chunk = matches[i:i + chunk_size]
        payload = {
            "username": "iPhone Marktplaats Monitor",
            "content": (
                f"📱 {len(chunk)} nieuwe iPhone-advertentie(s) gevonden!"
                if i == 0 else None
            ),
            "embeds": [build_embed(listing, model_name) for listing, model_name in chunk],
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

        if i + chunk_size < len(matches):
            time.sleep(1)


# --- Main --------------------------------------------------------------

def main() -> int:
    state = load_state()

    try:
        listings = fetch_all_listings()
    except RuntimeError as exc:
        print(f"[FOUT] {exc}", file=sys.stderr)
        return 1

    new_matches = []  # list of (listing, model_name)
    now_iso = datetime.now(timezone.utc).isoformat()

    for listing in listings:
        item_id = listing.get("itemId")
        if not item_id or item_id in state:
            continue

        model_name = evaluate_listing(listing)
        if model_name is None:
            continue

        new_matches.append((listing, model_name))
        state[item_id] = {
            "firstSeen": now_iso,
            "title": listing.get("title", ""),
            "model": model_name,
        }

    print(
        f"Opgehaald: {len(listings)} advertenties uit categorie Apple iPhone. "
        f"Nieuw en relevant: {len(new_matches)}."
    )

    if new_matches:
        send_discord_notifications(new_matches)
        for listing, model_name in new_matches:
            print(f"  -> [{model_name}] {listing.get('title')} | {format_price(listing)} | {listing_url(listing)}")

    state = prune_state(state)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
