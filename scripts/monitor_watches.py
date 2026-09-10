"""
Horloge-koopjes Marktplaats monitor.

Haalt nieuwe advertenties op uit de Marktplaats-categorie "Horloges | Heren"
(id 1831), matcht de titel tegen een curated lijst van 23 modellen (TAG
Heuer, Tudor, Longines, Oris, Omega, Baume & Mercier, Frederique Constant,
Raymond Weil, Certina, Mido) met een gemiddelde 2e-hands marktprijs tussen
~€650 en €2.500, en meldt alleen advertenties waarbij:

  geschatte marge = gemiddelde marktprijs - vraagprijs - onderhandelbuffer

  >= MIN_MARGIN_EUR (standaard €100).

BELANGRIJKE BEPERKINGEN (lees dit voor je op een melding afgaat):
  - De "gemiddelde marktprijs" per model is een ruwe schatting op basis van
    een klein aantal onderzochte referenties (Chrono24/WatchCharts, medio
    2026), NIET een live prijs-feed. Ga er niet blind van uit dat dit exact
    klopt voor het specifieke horloge in de advertentie.
  - Prijs hangt sterk af van conditie, jaar, en of doos/papieren aanwezig
    zijn. Dit script houdt daar geen rekening mee.
  - "Bieden" en "Zie omschrijving" advertenties (geen concrete vraagprijs)
    worden overgeslagen omdat er dan niets te vergelijken valt.
  - Een té grote marge bij een duur horloge is een bekend signaal voor
    NEPPE of GESTOLEN horloges. Controleer altijd zelf: serienummer,
    garantiekaart, doos/papieren, en spreek af op een veilige, openbare
    plek. Dit script is een hulpmiddel om kansen te vinden, geen
    koopadvies.

Gezien-ids worden bijgehouden in state/seen_watches.json zodat elke run
alleen de incrementele (nieuwe) advertenties meldt.
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

L1_CATEGORY_ID = 1826  # Sieraden, Tassen en Uiterlijk
L2_CATEGORY_ID = 1831  # Horloges | Heren

SEARCH_URL = "https://www.marktplaats.nl/lrp/api/search"
PAGE_SIZE = 100
MAX_PAGES = 6

# Locatiefilter (optioneel). Leeg laten = heel Nederland.
POSTCODE = os.environ.get("POSTCODE", "").strip()
DISTANCE_KM = os.environ.get("DISTANCE_KM", "").strip()

ASKING_MIN_EUR = float(os.environ.get("ASKING_MIN_EUR", "400"))
ASKING_MAX_EUR = float(os.environ.get("ASKING_MAX_EUR", "2500"))
ASKING_MIN_CENTS = int(ASKING_MIN_EUR * 100)
ASKING_MAX_CENTS = int(ASKING_MAX_EUR * 100)

NEGOTIATION_BUFFER_EUR = float(os.environ.get("NEGOTIATION_BUFFER_EUR", "150"))
NEGOTIATION_BUFFER_CENTS = int(NEGOTIATION_BUFFER_EUR * 100)

MIN_MARGIN_EUR = float(os.environ.get("MIN_MARGIN_EUR", "100"))
MIN_MARGIN_CENTS = int(MIN_MARGIN_EUR * 100)

# Alleen priceTypes met een concrete vraagprijs -- "Bieden" (FAST_BID) en
# "Zie omschrijving" (SEE_DESCRIPTION) geven geen bruikbaar getal.
USABLE_PRICE_TYPES = {"FIXED", "MIN_BID"}

# --- Curated modellenlijst -------------------------------------------------
# (label, match-functie, geschatte gemiddelde marktprijs in centen)
#
# Alle modellen hebben bewust een gemiddelde marktprijs tussen ~€650 en
# €2.500: bij een vraagprijs-ondergrens van €400, een onderhandelbuffer van
# €150 en een vereiste marge van €100 kan een model met een gemiddelde
# marktprijs onder ~€650 wiskundig NOOIT een match opleveren (400+150+100).
# Hamilton en Khaki-achtige instapmerken vallen daar nog steeds onder af.
# Rolex/de meeste Omega-/Tudor-vlaggenschepen/IWC/Breitling/Panerai/Monaco
# zijn te duur (gemiddelde >€2.500) en vallen om die reden af. Een paar
# modellen met te wisselvallige/inconsistente prijsdata (Aquaracer
# Chronograaf, Carrera Quartz, Longines Flagship, Oris Aquis, Omega
# Constellation) zijn bewust weggelaten -- liever minder modellen met
# betrouwbare cijfers dan veel modellen met giswerk.
#
# Sommige modellen bestaan zowel als quartz als automaat met een groot
# prijsverschil (bv. TAG Heuer Aquaracer/Link). Daar wordt het bewegingstype
# expliciet in de titel vereist -- staat het er niet duidelijk bij, dan
# wordt die advertentie overgeslagen in plaats van geraden, om een verkeerde
# marge-berekening te voorkomen.

QUARTZ_WORD = re.compile(r"quartz", re.IGNORECASE)
AUTOMATIC_WORDS = re.compile(r"(automatic|automaat|automatisch|mechanical|mechanisch|calibre\s*5|cal\.?\s*5)", re.IGNORECASE)
CHRONO_WORDS = re.compile(r"(chrono|chronograph|chronograaf)", re.IGNORECASE)
VINTAGE_WORD = re.compile(r"vintage", re.IGNORECASE)


def _quartz_only(model_pattern, exclude_chrono=False):
    def _match(t):
        if not model_pattern.search(t):
            return False
        if not QUARTZ_WORD.search(t) or AUTOMATIC_WORDS.search(t):
            return False
        if exclude_chrono and CHRONO_WORDS.search(t):
            return False
        return True
    return _match


def _automatic_only(model_pattern, exclude_chrono=False):
    def _match(t):
        if not model_pattern.search(t):
            return False
        if not AUTOMATIC_WORDS.search(t) or QUARTZ_WORD.search(t):
            return False
        if exclude_chrono and CHRONO_WORDS.search(t):
            return False
        return True
    return _match


_AQUARACER = re.compile(r"aquaracer", re.IGNORECASE)
_LINK = re.compile(r"\blink\b", re.IGNORECASE)
_AQUA_TERRA = re.compile(r"aqua\s*terra", re.IGNORECASE)
_FORMULA1 = re.compile(r"formula\s*1", re.IGNORECASE)


def _formula1_quartz_modern(t):
    return bool(_FORMULA1.search(t)) and bool(QUARTZ_WORD.search(t)) and not CHRONO_WORDS.search(t) and not VINTAGE_WORD.search(t)


def _formula1_mechanical(t):
    return bool(_FORMULA1.search(t)) and bool(AUTOMATIC_WORDS.search(t)) and not CHRONO_WORDS.search(t) and not QUARTZ_WORD.search(t)


# Elke entry: (label, match-functie, gem. marktprijs in centen, onderbouwing)
MODEL_CATALOG = [
    # --- TAG Heuer ---
    ("TAG Heuer Aquaracer (Quartz)", _quartz_only(_AQUARACER), 80000,
     "Ongedragen quartz-Aquaracers rond €800, oudere quartz-modellen vaak <€1.000 (Chrono24)."),
    ("TAG Heuer Aquaracer (Automaat)", _automatic_only(_AQUARACER), 150000,
     "Gedragen automaten rond €1.000, ongedragen mechanisch ~€1.300, Calibre 5 full-set tot ~€2.090 (Chrono24)."),
    ("TAG Heuer Carrera Calibre 5", re.compile(r"carrera\D{0,15}(calibre\s*5|cal\.?\s*5)", re.IGNORECASE), 200000,
     "Gebruikte exemplaren doorgaans €1.700-2.400, nieuwstaat op leer ~€2.700 (Chrono24)."),
    ("TAG Heuer Carrera Calibre 16 Chronograaf", re.compile(r"carrera\D{0,15}(calibre\s*16|cal\.?\s*16)", re.IGNORECASE), 180000,
     "Gedragen vanaf ~€1.500, ongedragen ~€2.000; gezien range €1.159-2.850 (Chrono24)."),
    ("TAG Heuer Formula 1 Chronograaf", re.compile(r"formula\s*1\D{0,15}(chrono|chronograph|chronograaf)", re.IGNORECASE), 190000,
     "Chronograaf-kalibers vanaf ~€1.700, vaak boven €2.000 (Chrono24)."),
    ("TAG Heuer Formula 1 (Quartz)", _formula1_quartz_modern, 80000,
     "Moderne ongedragen quartz ~€800. Let op: vintage quartz uit de jaren '90 is veel goedkoper (~€200-300) en apart uitgesloten."),
    ("TAG Heuer Formula 1 (Mechanisch)", _formula1_mechanical, 100000,
     "Mechanische (niet-chronograaf) modellen vanaf ~€1.000 (Chrono24)."),
    ("TAG Heuer Link (Quartz)", _quartz_only(_LINK), 120000,
     "Quartz-modellen rond $1.300 (~€1.200), USD-data omgerekend (Chrono24)."),
    ("TAG Heuer Link (Automaat)", _automatic_only(_LINK), 175000,
     "Automaat/Calibre 5 rond $1.900 (~€1.750), USD-data omgerekend (Chrono24)."),
    # --- Tudor ---
    ("Tudor Royal", re.compile(r"tudor\D{0,10}royal", re.IGNORECASE), 200000,
     "Brede spreiding €1.658-3.500 afhankelijk van maat/conditie; 41mm modellen vaak rond €2.000 (Chrono24)."),
    ("Tudor 1926", re.compile(r"tudor\D{0,10}1926", re.IGNORECASE), 180000,
     "Instapmodel (2019) vanaf €865 tot volledig nieuwstaat ~€1.984, gemiddeld ~€1.800 (Chrono24)."),
    ("Tudor Style", re.compile(r"tudor\D{0,10}style", re.IGNORECASE), 140000,
     "Beperkte data beschikbaar, prijzen vanaf €824; conservatieve schatting (Chrono24)."),
    # --- Longines ---
    ("Longines HydroConquest", re.compile(r"hydroconquest", re.IGNORECASE), 165000,
     "Duikershorloges doorgaans €1.500-2.200, gebruikte full-sets gemiddeld ~€1.650 (Chrono24)."),
    ("Longines Conquest", re.compile(r"longines\D{0,10}conquest", re.IGNORECASE), 160000,
     "Brede spreiding ($680-5.200); veelvoorkomende gebruikte modellen rond $2.000, conservatief bijgesteld (Chrono24)."),
    # --- Oris ---
    ("Oris Big Crown Pointer Date", re.compile(r"big\s*crown\D{0,15}pointer", re.IGNORECASE), 115000,
     "Gebruikt met leren band ~€1.000, met stalen/verguld tot ~€1.300 (Chrono24)."),
    ("Oris Divers Sixty-Five", re.compile(r"divers?\D{0,5}sixty[\s\-]?five", re.IGNORECASE), 110000,
     "Gebruikte exemplaren tussen €970-1.200, gemiddeld ~€1.100 (Chrono24)."),
    # --- Omega ---
    ("Omega Seamaster Aqua Terra (Quartz)", _quartz_only(_AQUA_TERRA), 240000,
     "Specifieke referentie gemiddeld ~$2.400 (~€2.240); kleinere 28mm modellen vanaf ~$2.500-2.900 (Chrono24)."),
    # --- Baume & Mercier ---
    ("Baume & Mercier Classima", re.compile(r"classima", re.IGNORECASE), 110000,
     "Prijzen $925-2.200 afhankelijk van uitvoering, gemiddeld conservatief geschat (Chrono24)."),
    # --- Frederique Constant ---
    ("Frederique Constant Classics", re.compile(r"frederique\s*constant.{0,20}classics", re.IGNORECASE), 70000,
     "Meeste gebruikte exemplaren €400-1.000, veel rond €575-680 (Chrono24). Dicht bij de ondergrens -> kleine marge-kansen."),
    # --- Raymond Weil ---
    ("Raymond Weil Freelancer", re.compile(r"freelancer", re.IGNORECASE), 100000,
     "Gebruikte modellen typisch €700-1.500, gemiddeld ~€1.000 (Chrono24)."),
    # --- Certina ---
    ("Certina DS Action / PH200M", re.compile(r"\bds\D{0,10}(action|ph\s*200)", re.IGNORECASE), 75000,
     "Voorbeelden gezien tussen €479-836 (Chrono24). Dicht bij de ondergrens -> kleine marge-kansen."),
    # --- Mido ---
    ("Mido Multifort", re.compile(r"multifort", re.IGNORECASE), 85000,
     "Voorbeelden €672-1.030, gemiddeld ~€850 (Chrono24)."),
    ("Mido Ocean Star", re.compile(r"ocean\s*star", re.IGNORECASE), 70000,
     "Vanaf €600-800, gemiddeld ~€700 (Chrono24). Dicht bij de ondergrens -> kleine marge-kansen."),
]

# --- Uitsluitingen: defect en (vermoedelijk) nep ---

BROKEN_CONDITION_VALUES = {"Niet werkend"}
BROKEN_KEYWORDS = [
    "voor onderdelen", "defect", "niet werkend", "loopt niet",
    "kapot", "start niet",
]
FAKE_KEYWORDS = [
    "replica", "namaak", "nep", "look-a-like", "lookalike", "look a like",
    "eta clone", "homage", "aaa kwaliteit", "1:1 kwaliteit",
]

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state", "seen_watches.json")
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


def _pattern_matches(pattern, title: str) -> bool:
    if hasattr(pattern, "search"):
        return bool(pattern.search(title))
    return bool(pattern(title))  # lambda-matcher (quartz/automaat-specifiek)


def classify_model(title: str):
    for label, pattern, avg_price_cents, rationale in MODEL_CATALOG:
        if _pattern_matches(pattern, title):
            return label, avg_price_cents, rationale
    return None, None, None


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
    if _contains_any(haystack, FAKE_KEYWORDS):
        return True
    return False


def evaluate_listing(listing: dict):
    """Geeft (modelnaam, vraagprijs_centen, marktprijs_centen, marge_centen,
    onderbouwing) terug als de advertentie een kansje is, anders None."""
    if not is_posted_today(listing):
        return None

    price_info = listing.get("priceInfo", {})
    price_type = price_info.get("priceType")
    if price_type not in USABLE_PRICE_TYPES:
        return None

    asking_price_cents = price_info.get("priceCents", 0)
    if not (ASKING_MIN_CENTS <= asking_price_cents <= ASKING_MAX_CENTS):
        return None

    if is_excluded(listing):
        return None

    model_name, avg_price_cents, rationale = classify_model(listing.get("title", ""))
    if model_name is None:
        return None

    margin_cents = avg_price_cents - asking_price_cents - NEGOTIATION_BUFFER_CENTS
    if margin_cents < MIN_MARGIN_CENTS:
        return None

    return model_name, asking_price_cents, avg_price_cents, margin_cents, rationale


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


def listing_image(listing: dict):
    images = listing.get("imageUrls") or []
    if not images:
        return None
    url = images[0]
    if url.startswith("//"):
        url = "https:" + url
    return url


def build_embed(listing: dict, model_name: str, asking_cents: int, avg_cents: int, margin_cents: int, rationale: str) -> dict:
    city = listing.get("location", {}).get("cityName", "Onbekende locatie")
    embed = {
        "title": listing.get("title", "Horloge advertentie")[:256],
        "url": listing_url(listing),
        "description": (
            f"**{model_name}**\n"
            f"Vraagprijs: {fmt_euro(asking_cents)} — {city}\n"
            f"Geschatte marktprijs: ~{fmt_euro(avg_cents)}\n"
            f"**Geschatte marge: ~{fmt_euro(margin_cents)}** (na €{NEGOTIATION_BUFFER_EUR:.0f} onderhandelbuffer)\n"
            f"📊 *Onderbouwing marktprijs:* {rationale}\n"
            f"⚠️ Schatting o.b.v. model, niet conditie/doos-papieren. Verifieer zelf echtheid en staat."
        ),
        "color": 0xF1C40F,
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
            "username": "Horloge Koopjes Monitor",
            "content": (
                f"⌚ {len(chunk)} mogelijk(e) horloge-koopje(s) gevonden! Controleer altijd zelf echtheid/conditie voor je een bod doet."
                if i == 0 else None
            ),
            "embeds": [
                build_embed(listing, model_name, asking_cents, avg_cents, margin_cents, rationale)
                for listing, model_name, asking_cents, avg_cents, margin_cents, rationale in chunk
            ],
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

    new_matches = []  # (listing, model_name, asking_cents, avg_cents, margin_cents, rationale)
    now_iso = datetime.now(timezone.utc).isoformat()

    for listing in listings:
        item_id = listing.get("itemId")
        if not item_id or item_id in state:
            continue

        result = evaluate_listing(listing)
        if result is None:
            continue
        model_name, asking_cents, avg_cents, margin_cents, rationale = result

        new_matches.append((listing, model_name, asking_cents, avg_cents, margin_cents, rationale))
        state[item_id] = {
            "firstSeen": now_iso,
            "title": listing.get("title", ""),
            "model": model_name,
            "askingCents": asking_cents,
            "estMarginCents": margin_cents,
        }

    print(
        f"Opgehaald: {len(listings)} advertenties uit categorie Horloges | Heren. "
        f"Nieuw en relevant: {len(new_matches)}."
    )

    if new_matches:
        send_discord_notifications(new_matches)
        for listing, model_name, asking_cents, avg_cents, margin_cents, rationale in new_matches:
            print(
                f"  -> [{model_name}] vraag {fmt_euro(asking_cents)} / markt ~{fmt_euro(avg_cents)} "
                f"/ marge ~{fmt_euro(margin_cents)} | {listing_url(listing)}"
            )

    state = prune_state(state)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
