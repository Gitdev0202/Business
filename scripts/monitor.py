"""
PS5 Marktplaats monitor.

Haalt nieuwe advertenties op uit de Marktplaats-categorie
"Spelcomputers | Sony PlayStation 5" (id 2954) EN de Games-categorie
(id 2952, waar verkopers hun console geregeld per ongeluk in plaatsen),
filtert op vandaag geplaatst + minimumprijs, weert losse spellen en
accessoires (controllers, playseats, racestuur e.d.), en stuurt alleen
nog-niet-eerder-geziene advertenties naar Discord via een webhook.
Gereserveerde advertenties worden WEL getoond (de reservering kan nog
afvallen), maar met een duidelijk "Gereserveerd"-label erbij.
Gezien-ids worden bijgehouden in state/seen.json zodat elke run alleen de
incrementele (nieuwe) advertenties meldt.
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
# Naast de echte consolecategorie (2954) doorzoeken we ook de Games-
# categorie (2952): verkopers zetten hun PS5-console daar geregeld per
# ongeluk in (bleek uit live onderzoek: tientallen echte consoles per dag
# die anders gemist zouden worden). De extra filters verderop (hardware-
# signaal, bekende spel-titels, accessoire-taal) zorgen dat losse spellen
# en accessoires uit die categorie niet per ongeluk meetellen.
L2_CONSOLE_CATEGORY_ID = 2954
L2_CATEGORY_IDS = [L2_CONSOLE_CATEGORY_ID, 2952]

SEARCH_URL = "https://www.marktplaats.nl/lrp/api/search"
PAGE_SIZE = 100
MAX_PAGES_PER_CATEGORY = 5  # ruim genoeg voor een dag vol advertenties

MIN_PRICE_EUR = float(os.environ.get("MIN_PRICE_EUR", "250"))
MIN_PRICE_CENTS = int(MIN_PRICE_EUR * 100)

# Locatiefilter (optioneel). Leeg laten = heel Nederland.
POSTCODE = os.environ.get("POSTCODE", "").strip()
DISTANCE_KM = os.environ.get("DISTANCE_KM", "").strip()

# priceType-waarden die altijd meetellen, ongeacht prijs:
# FAST_BID   = "Bieden" (open bieden, geen vraagprijs, technisch 0 cent)
# SEE_DESCRIPTION = prijs staat in de omschrijving (onbekend, dus liever
#                    te veel dan te weinig tonen)
ALWAYS_INCLUDE_PRICE_TYPES = {"FAST_BID", "SEE_DESCRIPTION"}

TITLE_PATTERN = re.compile(r"ps ?5|playstation ?5", re.IGNORECASE)
# Kandidaatwoord (9-13 letters, past bij de lengte van "playstation" +/- een
# paar typefouten) dat direct gevolgd wordt door "5" -- dezelfde nabijheids-
# eis als TITLE_PATTERN, maar dan met ruimte voor een fuzzy-match op het
# woord zelf. Voorkomt dat een losse "5" ergens anders in de titel (bv.
# "PlayStation 3 5-delige bundel") per ongeluk meetelt.
FUZZY_CANDIDATE_PATTERN = re.compile(r"\b([a-zA-Z]{9,13})\s*5\b")

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state", "seen.json")
PRUNE_AFTER_DAYS = 21  # oude entries opruimen zodat het bestand niet oneindig groeit

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# --- Marktplaats -----------------------------------------------------------

def fetch_page(l2_category_id: int, offset: int) -> dict:
    params = (
        f"l1CategoryId={L1_CATEGORY_ID}&l2CategoryId={l2_category_id}"
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
    for l2_category_id in L2_CATEGORY_IDS:
        for page in range(MAX_PAGES_PER_CATEGORY):
            data = fetch_page(l2_category_id, offset=page * PAGE_SIZE)
            page_listings = data.get("listings", [])
            listings.extend(page_listings)
            if len(page_listings) < PAGE_SIZE:
                break  # laatste pagina bereikt
    return listings


# --- Filtering ---------------------------------------------------------

def is_posted_today(listing: dict) -> bool:
    return listing.get("date") == "Vandaag"


def _levenshtein(a: str, b: str) -> int:
    """Edit-afstand tussen twee strings (aantal invoegingen/verwijderingen/
    vervangingen om van a naar b te komen). Pure Python, geen dependency."""
    if a == b:
        return 0
    prev_row = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        curr_row = [i] + [0] * len(b)
        for j, char_b in enumerate(b, start=1):
            curr_row[j] = min(
                prev_row[j] + 1,       # verwijdering
                curr_row[j - 1] + 1,   # invoeging
                prev_row[j - 1] + (char_a != char_b),  # vervanging (0 als gelijk)
            )
        prev_row = curr_row
    return prev_row[-1]


def matches_title(listing: dict) -> bool:
    title = listing.get("title", "")
    if TITLE_PATTERN.search(title):
        return True

    # Soepelere vangnet-check voor typefouten in "playstation" (bv.
    # "playstaion", "plastation"): tolereer tot 2 tekens verschil, maar
    # alleen als het kandidaatwoord ook echt direct gevolgd wordt door "5"
    # (net als bij de strikte check) -- zo telt een losse "5" die ergens
    # anders in de titel staat (bv. "PlayStation 3 5-delige bundel") niet
    # per ongeluk mee.
    for match in FUZZY_CANDIDATE_PATTERN.finditer(title.lower()):
        if _levenshtein(match.group(1), "playstation") <= 2:
            return True
    return False


# Attributen die alleen bij een los SPEL voorkomen, nooit bij een console
# (bv. "genre": "Vechten", "numberOfPlayers": "2 spelers"). Marktplaats'
# l2CategoryId-parameter is soms geen harde filter -- af en toe glipt een
# los spel toch mee, ondanks de titel-/categoriefilter. Dit signaal is
# precies genoeg om zo'n spel te weren, zonder het risico dat een echte
# PS5-console die toevallig in een net iets andere categorie staat wordt
# gemist (een harde categoryId-eis zou dat risico wel lopen).
GAME_ONLY_ATTRIBUTE_KEYS = {"genre", "numberOfPlayers", "multiplayerPossibilities"}


def is_actual_game_not_console(listing: dict) -> bool:
    attr_keys = {attr.get("key") for attr in listing.get("extendedAttributes", [])}
    return bool(attr_keys & GAME_ONLY_ATTRIBUTE_KEYS)


# --- Extra checks specifiek voor advertenties buiten de echte console-
#     categorie (voornamelijk de meegepakte Games-categorie, 2952) -------
#
# Binnen de officiele consolecategorie (2954) blijkt uit onderzoek dat
# is_actual_game_not_console (het genre/spelersaantal-attribuut) een heel
# betrouwbaar signaal is -- consoles hebben dat vrijwel nooit. Maar
# advertenties die uit de Games-categorie komen zijn verplicht een genre/
# spelersaantal in te vullen, ook als de verkoper eigenlijk een console
# aanbiedt -- daar is dat signaal dus WEL onbetrouwbaar, en is onderstaande
# striktere check nodig:
# - HARDWARE_SIGNAL_PATTERN: staat er iets over de CONSOLE zelf (opslag-
#   grootte, "Digital/Disc Edition", "console")? Dan is dit hoogst-
#   waarschijnlijk een echte (verkeerd-gecategoriseerde) console, ook als
#   er toevallig ook een specifiek spel genoemd wordt. ("Slim"/"Pro" staan
#   hier bewust NIET bij: die woorden komen net zo goed voor in accessoire-
#   titels als "hoesje voor de PS5 Pro".)
# - LEADING_ACCESSORY_PATTERN / ACCESSORY_FOR_PATTERN: "PS5 Controller" of
#   "hoesje voor PS5" -- PS5 is hier een merk-/compatibiliteitsvoorvoegsel
#   voor een accessoire, geen consolenaam. Onvoorwaardelijk uitgesloten,
#   want dit patroon is te specifiek om per ongeluk een console te raken.
# - Zonder hardware-signaal: begint de titel met "PS5"/"PlayStation 5"?
#   Dan nemen we aan dat de console zelf het onderwerp is (bv. "PS5 met 4
#   games en 2 controllers").
# - Anders: losse accessoire-taal of een bekende specifieke spel-titel
#   betekent dat het hoogstwaarschijnlijk GEEN console-advertentie is.
HARDWARE_SIGNAL_PATTERN = re.compile(
    r"\b\d{2,4}\s?(gb|tb)\b|console|spelcomputer|behuizing"
    r"|digital edition|disc edition|schijfloos",
    re.IGNORECASE,
)

# Onvoorwaardelijk: dit zijn producten, geen PS5-consoles, punt uit.
HARD_ACCESSORY_KEYWORDS = [
    "playseat", "racestuur", "race stuur", "logitech g29", "logitech g923",
    "thrustmaster",
]

_ACCESSORY_NOUNS = r"controllers?|dualsense|headsets?|koptelefoons?|hoes(je)?|laders?|opladers?|standaards?|koelers?|covers?|skins?|cases?"

# "PS5 Controller ..." / "PlayStation 5 Hoesje ..." -- PS5 direct gevolgd
# door een accessoire-zelfstandig naamwoord.
LEADING_ACCESSORY_PATTERN = re.compile(
    rf"^\s*(sony\s+)?(ps\s?5|playstation\s?5)\s+({_ACCESSORY_NOUNS})\b",
    re.IGNORECASE,
)
# "Controller voor PS5" / "hoesje for Playstation 5" -- omgekeerde volgorde.
ACCESSORY_FOR_PATTERN = re.compile(
    rf"({_ACCESSORY_NOUNS})\s*(voor|for)\s*(de\s*)?(ps\s?5|playstation\s?5)",
    re.IGNORECASE,
)
# Titel begint met "PS5"/"Sony PlayStation 5" -- waarschijnlijk de console
# zelf het onderwerp, ook zonder hardware-details (bv. "PS5 met games").
LEADS_WITH_PS5_PATTERN = re.compile(r"^\s*(sony\s+)?(ps\s?5|playstation\s?5)\b", re.IGNORECASE)
# Losse accessoire-woorden, ongeacht positie in de titel/omschrijving.
ACCESSORY_WORDS_PATTERN = re.compile(rf"\b({_ACCESSORY_NOUNS})\b", re.IGNORECASE)

# Kleine, bewust beperkte lijst actuele/bekende spellen die vaak zonder
# ingevuld genre-attribuut worden aangeboden. Later eenvoudig uit te
# breiden als er nieuwe titels opvallen (bv. na een melding die eigenlijk
# een spel bleek te zijn).
KNOWN_GAME_TITLE_HINTS = [
    "call of duty", "ea sports fc", "fifa", "grand theft auto",
    "gta 6", "gta6", "gta v", "gta5", "ghost of yotei", "mario kart",
    "zelda", "spider-man", "spiderman", "sniper elite", "assassin's creed",
    "resident evil", "god of war", "horizon forbidden west", "final fantasy",
]


def has_console_hardware_signal(listing: dict) -> bool:
    haystack = f"{listing.get('title', '')} {listing.get('description', '')}"
    return bool(HARDWARE_SIGNAL_PATTERN.search(haystack))


def is_likely_accessory_or_specific_game_only(listing: dict) -> bool:
    title = listing.get("title", "")
    haystack = f"{title} {listing.get('description', '')}"
    haystack_lower = haystack.lower()

    if any(kw in haystack_lower for kw in HARD_ACCESSORY_KEYWORDS):
        return True  # onvoorwaardelijk: dit is geen console
    if LEADING_ACCESSORY_PATTERN.search(title):
        return True  # "PS5 Controller/Hoesje/..."
    if ACCESSORY_FOR_PATTERN.search(haystack):
        return True  # "Controller voor PS5"

    if has_console_hardware_signal(listing):
        return False  # er staat genoeg over de console zelf bekend

    if LEADS_WITH_PS5_PATTERN.search(title):
        return False  # titel gaat duidelijk over de console zelf

    if ACCESSORY_WORDS_PATTERN.search(haystack):
        return True
    if any(hint in haystack_lower for hint in KNOWN_GAME_TITLE_HINTS):
        return True
    return False


def is_relevant_by_category(listing: dict) -> bool:
    """Kiest de juiste (soepele of strengere) check op basis van waar de
    advertentie daadwerkelijk in staat -- zie de uitleg hierboven."""
    if listing.get("categoryId") == L2_CONSOLE_CATEGORY_ID:
        return not is_actual_game_not_console(listing)
    return not is_likely_accessory_or_specific_game_only(listing)


def passes_price_filter(listing: dict) -> bool:
    price_info = listing.get("priceInfo", {})
    price_type = price_info.get("priceType")
    price_cents = price_info.get("priceCents", 0)

    if price_type in ALWAYS_INCLUDE_PRICE_TYPES:
        return True
    return price_cents >= MIN_PRICE_CENTS


def is_reserved(listing: dict) -> bool:
    # Marktplaats geeft dit als los datavel mee ("reserved": true/false) --
    # betrouwbaarder dan alleen op het woord zoeken. Het woord zelf checken
    # we er nog wel bij als extra vangnet, voor het geval dat veld een keer
    # niet (op tijd) is bijgewerkt. Gereserveerde advertenties worden NIET
    # uitgesloten -- de reservering kan nog afvallen -- maar dit signaal
    # wordt gebruikt om de Discord-melding van een label te voorzien.
    if listing.get("reserved") is True:
        return True
    haystack = f"{listing.get('title', '')} {listing.get('description', '')}"
    return "gereserveerd" in haystack.lower()


def is_relevant(listing: dict) -> bool:
    return (
        is_relevant_by_category(listing)
        and is_posted_today(listing)
        and matches_title(listing)
        and passes_price_filter(listing)
    )


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
    reserved = is_reserved(listing)
    title = listing.get("title", "PS5 advertentie")
    if reserved:
        title = f"🔒 [GERESERVEERD] {title}"
    embed = {
        "title": title[:256],
        "url": listing_url(listing),
        "description": f"{format_price(listing)} — {city}",
        "color": 0xE67E22 if reserved else 0x2ECC71,
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
