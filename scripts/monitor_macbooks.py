"""
MacBook Marktplaats monitor.

Haalt nieuwe advertenties op uit de Marktplaats-categorie "Apple Macbooks"
(id 325), filtert op vandaag geplaatst, prijs >= MIN_PRICE_EUR (of Bieden/
Zie omschrijving), en probeert modellen van vóór 2017 uit te sluiten.

Marktplaats heeft geen bouwjaar-veld voor MacBooks, dus het bouwjaar wordt
via de titel benaderd:
  - Een genoemde chip (M1/M2/M3/M4), "Touch Bar", "Retina" + "Air" samen, of
    een 12-inch MacBook zijn allemaal signalen van 2017 of nieuwer (dit komt
    overeen met de overstap van het oude oplichtende Apple-logo naar het
    huidige donkere logo) -> altijd tonen, ook als er toevallig een verwarrend
    jaartal in de titel staat (bv. "sinds 2017 in bezit" bij een M1-model).
  - Geen van die signalen, maar wel een expliciet jaartal < 2017 -> uitsluiten.
  - Geen enkel signaal en geen jaartal -> tonen (niet te bepalen, dus liever
    te veel dan te weinig).

Sluit daarnaast defecte/"Niet werkend", activatieslot-vergrendelde en
onderdelen-only advertenties uit. Gezien-ids worden bijgehouden in
state/seen_macbooks.json zodat elke run alleen de incrementele (nieuwe)
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

L1_CATEGORY_ID = 322  # Computers en Software
L2_CATEGORY_ID = 325  # Apple Macbooks

SEARCH_URL = "https://www.marktplaats.nl/lrp/api/search"
PAGE_SIZE = 100
MAX_PAGES = 5

# Locatiefilter (optioneel). Leeg laten = heel Nederland.
POSTCODE = os.environ.get("POSTCODE", "").strip()
DISTANCE_KM = os.environ.get("DISTANCE_KM", "").strip()

MIN_PRICE_EUR = float(os.environ.get("MIN_PRICE_EUR", "200"))
MIN_PRICE_CENTS = int(MIN_PRICE_EUR * 100)

ALWAYS_INCLUDE_PRICE_TYPES = {"FAST_BID", "SEE_DESCRIPTION"}

# Veiligheidsnet: soms staan opladers, Magsafe-adapters of hoesjes ook in
# deze categorie. Een echte MacBook-advertentie noemt vrijwel altijd
# "macbook" in de titel.
TITLE_PATTERN = re.compile(r"mac\s*book", re.IGNORECASE)

# --- Bouwjaar-benadering (zie module-docstring) ---

RECENT_CHIP_PATTERN = re.compile(r"\bm[1-4]\s*(pro|max|ultra)?\b", re.IGNORECASE)
TOUCH_BAR_PATTERN = re.compile(r"touch\s*bar", re.IGNORECASE)
TWELVE_INCH_PATTERN = re.compile(r"\b12[\s\-]?inch\b", re.IGNORECASE)
RETINA_PATTERN = re.compile(r"retina", re.IGNORECASE)
AIR_PATTERN = re.compile(r"\bair\b", re.IGNORECASE)
YEAR_PATTERN = re.compile(r"\b(19|20)\d{2}\b")


def has_recent_signal(title: str) -> bool:
    if RECENT_CHIP_PATTERN.search(title):
        return True
    if TOUCH_BAR_PATTERN.search(title):
        return True
    if TWELVE_INCH_PATTERN.search(title):
        return True
    if RETINA_PATTERN.search(title) and AIR_PATTERN.search(title):
        return True
    return False


def has_old_year(title: str) -> bool:
    match = YEAR_PATTERN.search(title)
    if not match:
        return False
    year = int(match.group())
    return year < 2017


def passes_era_filter(title: str) -> bool:
    if has_recent_signal(title):
        return True
    if has_old_year(title):
        return False
    return True  # geen enkel signaal -> liever te veel dan te weinig


# --- Uitsluitingen: defect, activatieslot, alleen onderdelen ---

BROKEN_CONDITION_VALUES = {"Niet werkend"}
BROKEN_KEYWORDS = [
    "voor onderdelen", "defect", "niet werkend", "kapot scherm",
    "start niet op", "gaat niet aan", "valt uit",
]
LOCK_KEYWORDS = [
    "activatieslot", "icloud vergrendeld", "icloud geblokkeerd",
    "find my mac staat aan", "activation lock", "icloud locked",
]
PARTS_ONLY_KEYWORDS = [
    "alleen behuizing", "alleen scherm", "los scherm", "alleen toetsenbord",
    "onderdelen van", "los moederbord", "zonder scherm", "kapotte behuizing",
    "beschermhoes", "hoesje", "sleeve", "laptoptas", "opbergtas",
]

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state", "seen_macbooks.json")
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
    if _contains_any(haystack, LOCK_KEYWORDS):
        return True
    if _contains_any(haystack, PARTS_ONLY_KEYWORDS):
        return True
    return False


def passes_price_filter(listing: dict) -> bool:
    price_info = listing.get("priceInfo", {})
    price_type = price_info.get("priceType")
    price_cents = price_info.get("priceCents", 0)

    if price_type in ALWAYS_INCLUDE_PRICE_TYPES:
        return True
    return price_cents >= MIN_PRICE_CENTS


def guess_model_label(title: str) -> str:
    t = title.lower()
    if "pro" in t:
        return "MacBook Pro"
    if "air" in t:
        return "MacBook Air"
    if TWELVE_INCH_PATTERN.search(title):
        return "MacBook (12-inch)"
    return "MacBook"


def is_relevant(listing: dict) -> bool:
    if not is_posted_today(listing):
        return False
    title = listing.get("title", "")
    if not TITLE_PATTERN.search(title):
        return False
    if not passes_era_filter(title):
        return False
    if is_excluded(listing):
        return False
    if not passes_price_filter(listing):
        return False
    return True


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


def build_embed(listing: dict) -> dict:
    city = listing.get("location", {}).get("cityName", "Onbekende locatie")
    model_label = guess_model_label(listing.get("title", ""))
    embed = {
        "title": listing.get("title", "MacBook advertentie")[:256],
        "url": listing_url(listing),
        "description": f"**{model_label}** — {format_price(listing)} — {city}",
        "color": 0x95A5A6,
    }
    image = listing_image(listing)
    if image:
        embed["thumbnail"] = {"url": image}
    return embed


def send_discord_notifications(listings: list) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL ontbreekt (environment variable / secret).")

    chunk_size = 10
    for i in range(0, len(listings), chunk_size):
        chunk = listings[i:i + chunk_size]
        payload = {
            "username": "MacBook Marktplaats Monitor",
            "content": (
                f"💻 {len(chunk)} nieuwe MacBook-advertentie(s) gevonden!"
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
            time.sleep(1)


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
        f"Opgehaald: {len(listings)} advertenties uit categorie Apple Macbooks. "
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
