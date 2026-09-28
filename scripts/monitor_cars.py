"""
Automaat-occasions Marktplaats monitor (autohandel-inkoopsignaal).

Doorzoekt Marktplaats op een curated lijst van automaat-modellen die
doorgaans snel verkopen in het budgetsegment (<= MAX_PRICE_EUR), filtert op
vandaag geplaatst + binnen budget, en berekent per model+bouwjaar-groep een
LEVENDE mediaan-vraagprijs op basis van de advertenties die deze run zelf
ophaalt (Marktplaats toont geen verkochte prijzen, dus een vaste
schattingstabel zoals bij de horloges-monitor is hier niet betrouwbaar
genoeg -- prijzen voor auto's variëren te veel met km-stand, staat en
uitvoering). Een advertentie is een "kans" als de vraagprijs minimaal
DISCOUNT_THRESHOLD_PCT onder die groepsmediaan ligt, met een minimum aantal
vergelijkbare advertenties om de mediaan betekenisvol te maken.

BELANGRIJKE BEPERKINGEN (lees dit voor je op een melding afgaat):
  - De Marktplaats-categorie-ID voor "Auto's" (L1_CATEGORY_ID hieronder) en
    de veldnamen voor bouwjaar/km-stand/transmissie kon ik niet live
    verifiëren -- deze omgeving heeft geen toegang tot marktplaats.nl.
    Het script zoekt daarom op vrije tekst (merk + model + "automaat") in
    plaats van op exacte categorie-ID's, en probeert meerdere plausibele
    veldnamen voor bouwjaar/km-stand (zie extract_year/extract_mileage).
    Controleer na de EERSTE succesvolle run de output; als bouwjaar/km-stand
    leeg blijven, print dan eenmalig een ruwe listing erbij om de juiste
    veldnaam te vinden.
  - Dit is een KOOPSIGNAAL, geen koopadvies. Altijd zelf de auto bekijken,
    proefrijden, onderhoudshistorie en (bij DSG/CVT/Powershift) het type
    transmissie navragen voor je toeslaat -- zie RISK_NOTE per model.
  - "Snel verkopen" wordt benaderd via de curated modellenlijst (alleen
    modellen met bekende hoge vraag) -- Marktplaats geeft geen verkochte
    prijzen of verkoopsnelheid, dus dit is geen harde meting.

Gezien-ids worden bijgehouden in state/seen_cars.json zodat elke run alleen
de incrementele (nieuwe) kansen meldt.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from statistics import median

# --- Configuratie ---------------------------------------------------------

# NIET geverifieerd tegen de live API (zie docstring) -- vrije-tekstzoeken
# via 'query' is daarom de primaire matchmethode, deze ID is een extra
# filter die het script negeert als 'ie niets oplevert.
L1_CATEGORY_ID = 91  # verondersteld: "Auto's"

SEARCH_URL = "https://www.marktplaats.nl/lrp/api/search"
PAGE_SIZE = 100
MAX_PAGES_PER_MODEL = 2  # 200 advertenties per model is ruim genoeg

MAX_PRICE_EUR = float(os.environ.get("MAX_PRICE_EUR", "10000"))
MAX_PRICE_CENTS = int(MAX_PRICE_EUR * 100)
MIN_PRICE_EUR = float(os.environ.get("MIN_PRICE_EUR", "1500"))  # weert lege carrosserieën/onderdelen-advertenties
MIN_PRICE_CENTS = int(MIN_PRICE_EUR * 100)

POSTCODE = os.environ.get("POSTCODE", "").strip()
DISTANCE_KM = os.environ.get("DISTANCE_KM", "50").strip()

# Hoeveel procent onder de levende groepsmediaan een advertentie moet zitten
# om als "kans" te gelden, en hoeveel vergelijkbare advertenties er
# minimaal moeten zijn om die mediaan te vertrouwen.
DISCOUNT_THRESHOLD_PCT = float(os.environ.get("DISCOUNT_THRESHOLD_PCT", "15"))
MIN_GROUP_SIZE = int(os.environ.get("MIN_GROUP_SIZE", "4"))
BOUWJAAR_BIN_SIZE = 3  # groepeer per 3 bouwjaren (2016-2018, 2019-2021, ...)

# --- Curated modellenlijst --------------------------------------------------
# Automaat-modellen die in het budgetsegment (<= ~10k) doorgaans snel
# verkopen in NL, met een risico-notitie over het type transmissie.
# Torque-converter/CVT-modellen met goede reputatie wegen zwaarder mee dan
# modellen met een bekend kwetsbaar dubbele-koppelingsbak (DSG/Powershift) --
# die laatste staan er wel bij (veel aanbod, veel vraag) maar met een
# duidelijke waarschuwing in de Discord-melding.
MODEL_CATALOG = [
    {"label": "Toyota Aygo automaat", "query": "toyota aygo automaat",
     "risk": "Laag risico: CVT/automaat met goede reputatie. Hoge vraag, vaak snel weg."},
    {"label": "Toyota Yaris Hybride (automaat)", "query": "toyota yaris hybride",
     "risk": "Laag risico: e-CVT, taxi-beproefd. Hoge vraag kan vraagprijzen al opdrijven -- kansen zijn schaarser maar wel betrouwbaar."},
    {"label": "Toyota Auris/Corolla Hybride (automaat)", "query": "toyota auris hybride",
     "risk": "Laag risico: e-CVT, taxi-beproefd. Zelfde kanttekening als Yaris Hybride."},
    {"label": "Kia Picanto automaat", "query": "kia picanto automaat",
     "risk": "Laag risico: traditionele automaat/AMT, betrouwbaar, lange fabrieksgarantie-historie."},
    {"label": "Hyundai i10 automaat", "query": "hyundai i10 automaat",
     "risk": "Laag risico: vergelijkbaar met Kia Picanto (zelfde groep/fabriek)."},
    {"label": "Opel Corsa automaat", "query": "opel corsa automaat",
     "risk": "Laag-gemiddeld risico: traditionele automaat, ruim aanbod, gemiddelde vraag."},
    {"label": "Opel Astra automaat", "query": "opel astra automaat",
     "risk": "Laag-gemiddeld risico: traditionele automaat."},
    {"label": "Suzuki Swift automaat", "query": "suzuki swift automaat",
     "risk": "Laag-gemiddeld risico: CVT met redelijke reputatie."},
    {"label": "Volkswagen Up! automaat", "query": "volkswagen up automaat",
     "risk": "Gemiddeld risico: ASG (robotbak) kan schokkerig schakelen -- geen defect, wel navragen of koper dit weet."},
    {"label": "Volkswagen Polo DSG", "query": "volkswagen polo dsg",
     "risk": "LET OP: vroege 7-versnellings DSG (DQ200, droge koppeling, vaak <2015) heeft bekende koppelingsproblemen. Vraag onderhoudshistorie/koppelingvervanging na voor je toeslaat."},
    {"label": "Ford Fiesta Powershift", "query": "ford fiesta powershift",
     "risk": "VERHOOGD RISICO: Powershift-bak heeft bekende, veelvuldige betrouwbaarheidsproblemen (o.a. terugroepacties). Alleen kopen met aantoonbaar recent vervangen/gereviseerde bak, anders vermijden."},
    {"label": "Peugeot 208 automaat (EAT6/EAT8)", "query": "peugeot 208 automaat",
     "risk": "Gemiddeld risico: vroege EAT6 (voor ~2015) had opstartproblemen, latere EAT8 beter. Bouwjaar checken."},
    {"label": "Citroen C3 automaat (EAT6)", "query": "citroen c3 automaat",
     "risk": "Gemiddeld risico: zelfde EAT6/EAT8-kanttekening als Peugeot 208."},
    {"label": "Renault Clio automaat", "query": "renault clio automaat",
     "risk": "VERHOOGD RISICO: CVT (Jatco) in dit segment staat bekend om oververhitting/slijtage. Onderhoudshistorie CVT-olie navragen, anders vermijden."},
    {"label": "smart fortwo automaat", "query": "smart fortwo automaat",
     "risk": "Gemiddeld risico: automatische versnellingsbak (enkele koppeling) schakelt schokkerig, geen defect maar wel een aandachtspunt bij proefrit."},
]

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state", "seen_cars.json")
PRUNE_AFTER_DAYS = 21

# Automaat-signaal in titel/omschrijving -- extra vangnet naast de
# modelspecifieke zoekopdracht, weert handgeschakelde exemplaren die soms
# toch meekomen in de zoekresultaten.
AUTOMAAT_PATTERN = re.compile(
    r"\bautomaa[a-z]*\b|\bdsg\b|\bcvt\b|\bpowershift\b|\beat6\b|\beat8\b|\basg\b|\btiptronic\b",
    re.IGNORECASE,
)
MANUAL_HINT_PATTERN = re.compile(r"\bhandgeschakeld\b|\bhandbak\b", re.IGNORECASE)


# --- Marktplaats -----------------------------------------------------------

def fetch_page(query: str, offset: int) -> dict:
    params = (
        f"query={urllib.parse.quote(query)}"
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


def fetch_model_listings(model: dict) -> list:
    listings = []
    for page in range(MAX_PAGES_PER_MODEL):
        data = fetch_page(model["query"], offset=page * PAGE_SIZE)
        page_listings = data.get("listings", [])
        for listing in page_listings:
            listing["_model_label"] = model["label"]
            listing["_model_risk"] = model["risk"]
        listings.extend(page_listings)
        if len(page_listings) < PAGE_SIZE:
            break
    return listings


# --- Filtering / extractie -------------------------------------------------

def is_posted_today(listing: dict) -> bool:
    return listing.get("date") == "Vandaag"


def is_automatic(listing: dict) -> bool:
    haystack = f"{listing.get('title', '')} {listing.get('description', '')}"
    if MANUAL_HINT_PATTERN.search(haystack):
        return False
    return bool(AUTOMAAT_PATTERN.search(haystack))


def passes_price_filter(listing: dict) -> bool:
    price_info = listing.get("priceInfo", {})
    price_cents = price_info.get("priceCents", 0)
    price_type = price_info.get("priceType")
    if price_type not in ("FIXED", "MIN_BID"):
        return False  # "Bieden"/"Zie omschrijving" geeft geen vergelijkbaar getal voor de mediaan
    return MIN_PRICE_CENTS <= price_cents <= MAX_PRICE_CENTS


# Best-effort: bouwjaar en km-stand kunnen bij Marktplaats op verschillende
# plekken in de listing-JSON staan afhankelijk van categorie. NIET
# geverifieerd tegen een live response -- zie docstring bovenin.
def extract_year(listing: dict) -> int | None:
    for key in ("constructionYear", "buildYear", "year"):
        value = listing.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    for attr in listing.get("attributes", []) + listing.get("extendedAttributes", []):
        if attr.get("key") in ("constructionYear", "buildYear"):
            try:
                return int(attr.get("value"))
            except (TypeError, ValueError):
                continue
    match = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", listing.get("title", ""))
    if match:
        return int(match.group(1))
    return None


def extract_mileage(listing: dict) -> int | None:
    for key in ("mileage", "kilometerage"):
        value = listing.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    for attr in listing.get("attributes", []) + listing.get("extendedAttributes", []):
        if attr.get("key") in ("mileage", "kilometerage"):
            try:
                return int(re.sub(r"[^\d]", "", str(attr.get("value"))))
            except (TypeError, ValueError):
                continue
    return None


def bouwjaar_bin(year: int) -> str:
    start = (year // BOUWJAAR_BIN_SIZE) * BOUWJAAR_BIN_SIZE
    return f"{start}-{start + BOUWJAAR_BIN_SIZE - 1}"


# --- Levende mediaan-referentie ---------------------------------------------

def group_key(listing: dict) -> tuple | None:
    year = extract_year(listing)
    if year is None:
        return None
    return (listing["_model_label"], bouwjaar_bin(year))


def compute_group_medians(listings: list) -> dict:
    groups: dict = {}
    for listing in listings:
        key = group_key(listing)
        if key is None:
            continue
        price = listing.get("priceInfo", {}).get("priceCents", 0)
        groups.setdefault(key, []).append(price)
    return {key: (median(prices), len(prices)) for key, prices in groups.items() if len(prices) >= MIN_GROUP_SIZE}


def find_deals(listings: list, medians: dict) -> list:
    deals = []
    for listing in listings:
        key = group_key(listing)
        if key is None or key not in medians:
            continue
        group_median_cents, group_size = medians[key]
        price_cents = listing.get("priceInfo", {}).get("priceCents", 0)
        if price_cents <= 0:
            continue
        discount_pct = (1 - price_cents / group_median_cents) * 100
        if discount_pct >= DISCOUNT_THRESHOLD_PCT:
            listing["_group_median_cents"] = group_median_cents
            listing["_group_size"] = group_size
            listing["_discount_pct"] = round(discount_pct, 1)
            deals.append(listing)
    return deals


def is_relevant(listing: dict) -> bool:
    return is_posted_today(listing) and is_automatic(listing) and passes_price_filter(listing)


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
    price_cents = listing.get("priceInfo", {}).get("priceCents", 0)
    year = extract_year(listing)
    mileage = extract_mileage(listing)
    year_str = str(year) if year else "onbekend bouwjaar"
    mileage_str = f"{mileage:,} km".replace(",", ".") if mileage else "km onbekend"

    description = (
        f"{fmt_euro(price_cents)} — {year_str}, {mileage_str} — {city}\n"
        f"~{listing['_discount_pct']}% onder de levende mediaan "
        f"({fmt_euro(listing['_group_median_cents'])}, n={listing['_group_size']})\n"
        f"⚠️ {listing['_model_risk']}"
    )

    embed = {
        "title": listing.get("title", "Auto-advertentie")[:256],
        "url": listing_url(listing),
        "description": description[:4096],
        "color": 0x2ECC71,
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
            "username": "Auto-inkoop Monitor",
            "content": (
                f"🚗 {len(chunk)} nieuwe inkoopkans(en) onder de marktmediaan!"
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

    all_listings = []
    try:
        for model in MODEL_CATALOG:
            all_listings.extend(fetch_model_listings(model))
    except RuntimeError as exc:
        print(f"[FOUT] {exc}", file=sys.stderr)
        return 1

    relevant = [l for l in all_listings if is_relevant(l)]
    medians = compute_group_medians(relevant)
    deals = find_deals(relevant, medians)

    now_iso = datetime.now(timezone.utc).isoformat()
    new_matches = []
    for listing in deals:
        item_id = listing.get("itemId")
        if not item_id or item_id in state:
            continue
        new_matches.append(listing)
        state[item_id] = {
            "firstSeen": now_iso,
            "title": listing.get("title", ""),
            "model": listing["_model_label"],
        }

    if not (POSTCODE and DISTANCE_KM):
        print(
            "[WAARSCHUWING] Geen locatiefilter actief (POSTCODE en/of "
            "DISTANCE_KM ontbreken/leeg) -- er wordt over HEEL NEDERLAND "
            "gezocht i.p.v. lokaal.",
            file=sys.stderr,
        )

    print(
        f"Opgehaald: {len(all_listings)} advertenties over {len(MODEL_CATALOG)} modellen. "
        f"Relevant (automaat, vandaag, binnen budget): {len(relevant)}. "
        f"Prijsgroepen met genoeg data: {len(medians)}. "
        f"Nieuwe kansen: {len(new_matches)}."
    )

    if new_matches:
        send_discord_notifications(new_matches)
        for listing in new_matches:
            print(
                f"  -> {listing.get('title')} | {fmt_euro(listing['priceInfo']['priceCents'])} "
                f"| -{listing['_discount_pct']}% | {listing_url(listing)}"
            )

    state = prune_state(state)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
