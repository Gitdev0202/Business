"""
Automaat-occasions monitor (autohandel-inkoopsignaal): Marktplaats + AutoScout24.

Doorzoekt Marktplaats EN AutoScout24 op een curated lijst van automaat-
modellen die doorgaans snel verkopen in het budgetsegment
(<= MAX_PRICE_EUR), filtert op bouwjaar/km-stand/budget/transmissie, en
berekent per advertentie een LEVENDE mediaan-vraagprijs op basis van
vergelijkbare advertenties die deze run zelf ophaalt (geen van beide
bronnen toont verkochte prijzen, dus een vaste schattingstabel zoals bij de
horloges-monitor is hier niet betrouwbaar genoeg -- prijzen voor auto's
variëren te veel met km-stand, staat en uitvoering).

Filtert BEWUST NIET meer op plaatsingsdatum ("vandaag"): dat filter
beperkte de referentiegroep tot een klein deel van het actuele aanbod
(alleen wat toevallig vandaag geplaatst is), terwijl de bescherming tegen
dubbele meldingen toch al via state/seen_cars.json (dedup op advertentie-
id) loopt, niet via de datum. Door het hele huidige aanbod te gebruiken
i.p.v. alleen "vandaag", krijgt elke advertentie een veel grotere en
stabielere vergelijkingsgroep.

Draait 1x per 6 uur (niet meer elke 5 minuten) en is bewust een LANGE,
grondige run: tot MAX_PAGES_PER_MODEL/AUTOSCOUT_MAX_PAGES pagina's per
model per bron, met een beleefde, licht gerandomiseerde pauze tussen elk
verzoek (zie polite_pause/PAGE_FETCH_DELAY_SEC) om niet als bot-verkeer op
te vallen bij deze grotere volumes. Eén model dat blijft weigeren (bv.
tijdelijke rate-limiting) mag nooit de hele run laten crashen: fouten
worden per model opgevangen en overgeslagen, de rest van de catalogus
draait gewoon door (zie fetch_model_listings). Eenmalig na deze wijziging
kan het aantal meldingen in één run hoger zijn dan normaal, omdat nu ook
al langer bestaande onderprijsde advertenties voor het eerst worden
meegenomen -- dat is bedoeld, geen storing.

AutoScout24-ondersteuning is GEVERIFIEERD (niet giswerk, zie
scripts/probe_sources.py): de resultatenlijst wordt serverside gerenderd
als schema.org JSON-LD, met expliciete velden voor km-stand, transmissie-
type en verkoper-type (@type "AutoDealer" = handelaar). Alle 35 merk/model-
URL's in MODEL_CATALOG zijn stuk voor stuk gecheckt. AutoScout24 is in NL
overwegend een handelaarsplatform -- verwacht dus vooral een sterkere
mediaan-referentie van deze bron, en minder particuliere kansen dan van
Marktplaats. Twee kanttekeningen specifiek voor AutoScout24: er is geen
bouwjaar-veld in de lijstweergave (bouwjaar-groepering lukt hier vaak niet)
en geen plaatsingsdatum (dus geen "vandaag"-filter, elke run scant het
actuele aanbod -- dedup via state voorkomt dubbele meldingen).
Gaspedaal.nl blokkeert met HTTP 403 en is niet meegenomen.

Twee aparte groepen, bewust, met TWEE APARTE FETCHES (zie fetch_model_listings
vs. fetch_model_listings_local):
  1. REFERENTIEGROEP (voor de mediaan): ALLE relevante advertenties, van
     zowel particulieren als handelaren, in HEEL NEDERLAND (geen
     postcode/afstandsfilter, ongeacht wat POSTCODE/DISTANCE_KM zijn). Meer
     data = een betrouwbaardere mediaan, en handelaarsprijzen vormen een
     prima bovengrens-referentie ("dit is wat de markt normaal vraagt").
  2. MELD-GROEP (wat daadwerkelijk als kans wordt doorgestuurd): een APARTE,
     LOKALE fetch -- alleen Marktplaats-advertenties van PARTICULIEREN
     binnen DISTANCE_KM van POSTCODE (default: de gedeelde POSTCODE-secret
     van dit account, Winschoten, straal 50 km) die minimaal
     DISCOUNT_THRESHOLD_PCT onder de LANDELIJKE groepsmediaan zitten.
     AutoScout24 telt wel mee voor de referentiegroep, maar nooit voor de
     meld-groep (geen geverifieerd postcode/straal-parameter voor die
     bron). Handelaren worden nooit gemeld -- die prijzen doorgaans op
     marktniveau of erboven, dus zijn zelden een "kans" en zitten in de weg
     als koper.
De referentieprijs per advertentie komt uit een OP MAAT GEMAAKTE peergroep:
zelfde model, km-stand binnen MAX_MILEAGE_DEVIATION_KM en bouwjaar binnen
MAX_YEAR_DEVIATION van die specifieke advertentie (zie find_peer_prices) --
preciezer dan een vast bouwjaar-bin, omdat elke auto met zijn eigen meest
vergelijkbare buren vergeleken wordt in plaats van met een grove, gedeelde
groep.

BELANGRIJKE BEPERKINGEN (lees dit voor je op een melding afgaat):
  - De Marktplaats-categorie-ID voor "Auto's" (L1_CATEGORY_ID hieronder), de
    veldnamen voor bouwjaar/km-stand/transmissie, EN het veld waarmee
    particulier vs. handelaar te onderscheiden is, kon ik niet live
    verifiëren -- deze omgeving heeft geen toegang tot marktplaats.nl. Het
    script zoekt daarom op vrije tekst (merk + model + "automaat") i.p.v.
    exacte categorie-ID's, probeert meerdere plausibele veldnamen voor
    bouwjaar/km-stand (zie extract_year/extract_mileage), en behandelt een
    advertentie als PARTICULIER tenzij er een expliciet handelaarssignaal
    gevonden wordt (zie is_private_seller) -- bewust de veilige kant op,
    want een gemiste particuliere advertentie is minder erg dan een
    handelaarsadvertentie die per ongeluk als "kans" wordt gemeld.
    Controleer na de EERSTE succesvolle run de output; print zo nodig
    eenmalig een ruwe listing om de juiste veldnamen te vinden.
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
import random
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
# Draait nu 1x per 6 uur i.p.v. elke 5 minuten (zie workflow) -- er is dus
# ruim tijd voor een grondige, diepe zoekopdracht per model. Pagination
# stopt vanzelf zodra een model minder aanbod heeft dan dit maximum (zie
# fetch_model_listings), dus dit is een bovengrens, geen vast aantal
# requests per model.
MAX_PAGES_PER_MODEL = 20  # tot 2.000 advertenties per model

# Pauze tussen opeenvolgende pagina-verzoeken (met wat willekeur, zodat het
# verkeer niet als geautomatiseerd/bot-patroon oogt). Met de nieuwe 6-uurs
# cadans is er geen enkele haast -- liever rustig en betrouwbaar dan snel
# en geblokkeerd (zie PAGE_FETCH_DELAY_SEC-gebruik in fetch_model_listings/
# fetch_autoscout_listings).
PAGE_FETCH_DELAY_SEC = float(os.environ.get("PAGE_FETCH_DELAY_SEC", "2.0"))


def polite_pause() -> None:
    time.sleep(PAGE_FETCH_DELAY_SEC + random.uniform(0, 1.5))

MAX_PRICE_EUR = float(os.environ.get("MAX_PRICE_EUR", "10000"))
MAX_PRICE_CENTS = int(MAX_PRICE_EUR * 100)
MIN_PRICE_EUR = float(os.environ.get("MIN_PRICE_EUR", "1500"))  # weert lege carrosserieën/onderdelen-advertenties
MIN_PRICE_CENTS = int(MIN_PRICE_EUR * 100)

# Alleen auto's onder deze km-stand en NA dit bouwjaar (dus strikt ouder/
# meer-km wordt geweerd, evenals advertenties waar km-stand/bouwjaar niet
# uit te lezen is -- zie passes_age_mileage_filter).
MAX_MILEAGE_KM = int(os.environ.get("MAX_MILEAGE_KM", "150000"))
MIN_CONSTRUCTION_YEAR = int(os.environ.get("MIN_CONSTRUCTION_YEAR", "2005"))

# Twee verschillende zoekgebieden, bewust (zie hoofd-docstring):
#   - Referentiegroep (mediaan): HEEL NEDERLAND, altijd -- fetch_page krijgt
#     hiervoor apply_location=False, ongeacht of POSTCODE/DISTANCE_KM
#     ingesteld zijn. Meer data = een betrouwbaardere mediaan.
#   - Meld-groep (kansen): alleen ROND POSTCODE, binnen DISTANCE_KM --
#     fetch_page krijgt hiervoor apply_location=True. Dit is dezelfde
#     POSTCODE-secret die de andere monitors in deze repo al gebruiken.
POSTCODE = os.environ.get("POSTCODE", "").strip()
DISTANCE_KM = os.environ.get("DISTANCE_KM", "").strip()

# Hoeveel procent onder de levende groepsmediaan een advertentie moet zitten
# om als "kans" te gelden, en hoeveel vergelijkbare advertenties er
# minimaal moeten zijn om die mediaan te vertrouwen.
DISCOUNT_THRESHOLD_PCT = float(os.environ.get("DISCOUNT_THRESHOLD_PCT", "15"))
MIN_GROUP_SIZE = int(os.environ.get("MIN_GROUP_SIZE", "4"))

# --- Curated modellenlijst --------------------------------------------------
# Automaat-modellen die in het budgetsegment (<= ~10k) doorgaans snel
# verkopen in NL, met een risico-notitie over het type transmissie.
# Torque-converter/CVT-modellen met goede reputatie wegen zwaarder mee dan
# modellen met een bekend kwetsbaar dubbele-koppelingsbak (DSG/Powershift) --
# die laatste staan er wel bij (veel aanbod, veel vraag) maar met een
# duidelijke waarschuwing in de Discord-melding.
MODEL_CATALOG = [
    {"label": "Toyota Aygo automaat", "query": "toyota aygo automaat", "autoscout_path": "toyota/aygo",
     "risk": "Laag risico: CVT/automaat met goede reputatie. Hoge vraag, vaak snel weg."},
    {"label": "Toyota Yaris Hybride (automaat)", "query": "toyota yaris hybride", "autoscout_path": "toyota/yaris",
     "risk": "Laag risico: e-CVT, taxi-beproefd. Hoge vraag kan vraagprijzen al opdrijven -- kansen zijn schaarser maar wel betrouwbaar."},
    {"label": "Toyota Auris/Corolla Hybride (automaat)", "query": "toyota auris hybride", "autoscout_path": "toyota/auris",
     "risk": "Laag risico: e-CVT, taxi-beproefd. Zelfde kanttekening als Yaris Hybride."},
    {"label": "Kia Picanto automaat", "query": "kia picanto automaat", "autoscout_path": "kia/picanto",
     "risk": "Laag risico: traditionele automaat/AMT, betrouwbaar, lange fabrieksgarantie-historie."},
    {"label": "Hyundai i10 automaat", "query": "hyundai i10 automaat", "autoscout_path": "hyundai/i10",
     "risk": "Laag risico: vergelijkbaar met Kia Picanto (zelfde groep/fabriek)."},
    {"label": "Opel Corsa automaat", "query": "opel corsa automaat", "autoscout_path": "opel/corsa",
     "risk": "Laag-gemiddeld risico: traditionele automaat, ruim aanbod, gemiddelde vraag."},
    {"label": "Opel Astra automaat", "query": "opel astra automaat", "autoscout_path": "opel/astra",
     "risk": "Laag-gemiddeld risico: traditionele automaat."},
    {"label": "Suzuki Swift automaat", "query": "suzuki swift automaat", "autoscout_path": "suzuki/swift",
     "risk": "Laag-gemiddeld risico: CVT met redelijke reputatie."},
    {"label": "Volkswagen Up! automaat", "query": "volkswagen up automaat", "autoscout_path": "volkswagen/up",
     "risk": "Gemiddeld risico: ASG (robotbak) kan schokkerig schakelen -- geen defect, wel navragen of koper dit weet."},
    {"label": "Volkswagen Polo DSG", "query": "volkswagen polo dsg", "autoscout_path": "volkswagen/polo",
     "risk": "LET OP: vroege 7-versnellings DSG (DQ200, droge koppeling, vaak <2015) heeft bekende koppelingsproblemen. Vraag onderhoudshistorie/koppelingvervanging na voor je toeslaat."},
    {"label": "Ford Fiesta Powershift", "query": "ford fiesta powershift", "autoscout_path": "ford/fiesta",
     "risk": "VERHOOGD RISICO: Powershift-bak heeft bekende, veelvuldige betrouwbaarheidsproblemen (o.a. terugroepacties). Alleen kopen met aantoonbaar recent vervangen/gereviseerde bak, anders vermijden."},
    {"label": "Peugeot 208 automaat (EAT6/EAT8)", "query": "peugeot 208 automaat", "autoscout_path": "peugeot/208",
     "risk": "Gemiddeld risico: vroege EAT6 (voor ~2015) had opstartproblemen, latere EAT8 beter. Bouwjaar checken."},
    {"label": "Citroen C3 automaat (EAT6)", "query": "citroen c3 automaat", "autoscout_path": "citroen/c3",
     "risk": "Gemiddeld risico: zelfde EAT6/EAT8-kanttekening als Peugeot 208."},
    {"label": "Renault Clio automaat", "query": "renault clio automaat", "autoscout_path": "renault/clio",
     "risk": "VERHOOGD RISICO: CVT (Jatco) in dit segment staat bekend om oververhitting/slijtage. Onderhoudshistorie CVT-olie navragen, anders vermijden."},
    {"label": "smart fortwo automaat", "query": "smart fortwo automaat", "autoscout_path": "smart/fortwo",
     "risk": "Gemiddeld risico: automatische versnellingsbak (enkele koppeling) schakelt schokkerig, geen defect maar wel een aandachtspunt bij proefrit."},
    {"label": "Fiat 500 automaat/Dualogic", "query": "fiat 500 automaat", "autoscout_path": "fiat/500",
     "risk": "Gemiddeld risico: Dualogic is een robotbak (enkele koppeling), schakelt schokkerig -- geen defect maar wel navragen of koper dit weet. Erg populair, sells fast."},
    {"label": "Fiat Panda automaat", "query": "fiat panda automaat", "autoscout_path": "fiat/panda",
     "risk": "Gemiddeld risico: zelfde Dualogic-kanttekening als Fiat 500."},
    {"label": "Seat Ibiza automaat/DSG", "query": "seat ibiza dsg", "autoscout_path": "seat/ibiza",
     "risk": "LET OP: zelfde DSG-platform/kanttekening als Volkswagen Polo (concernauto)."},
    {"label": "Seat Mii automaat", "query": "seat mii automaat", "autoscout_path": "seat/mii",
     "risk": "Gemiddeld risico: zelfde ASG-kanttekening als Volkswagen Up! (concernauto)."},
    {"label": "Skoda Fabia automaat/DSG", "query": "skoda fabia dsg", "autoscout_path": "skoda/fabia",
     "risk": "LET OP: zelfde DSG-platform/kanttekening als Volkswagen Polo (concernauto)."},
    {"label": "Skoda Citigo automaat", "query": "skoda citigo automaat", "autoscout_path": "skoda/citigo",
     "risk": "Gemiddeld risico: zelfde ASG-kanttekening als Volkswagen Up! (concernauto)."},
    {"label": "Honda Jazz automaat", "query": "honda jazz automaat", "autoscout_path": "honda/jazz",
     "risk": "Laag risico: CVT met sterke betrouwbaarheidsreputatie, populair bij oudere kopers -- stabiele vraag."},
    {"label": "Mazda 2 automaat", "query": "mazda 2 automaat", "autoscout_path": "mazda/2",
     "risk": "Laag-gemiddeld risico: traditionele automaat, degelijke reputatie."},
    {"label": "Citroen C1 automaat", "query": "citroen c1 automaat", "autoscout_path": "citroen/c1",
     "risk": "Laag risico: zelfde platform/reputatie als Toyota Aygo (samen ontwikkeld)."},
    {"label": "Peugeot 107 automaat", "query": "peugeot 107 automaat", "autoscout_path": "peugeot/107",
     "risk": "Laag risico: zelfde platform/reputatie als Toyota Aygo (samen ontwikkeld)."},
    {"label": "Nissan Micra automaat/CVT", "query": "nissan micra automaat", "autoscout_path": "nissan/micra",
     "risk": "VERHOOGD RISICO: Jatco CVT, zelfde kanttekening als Renault Clio (gedeeld platform/bak)."},
    {"label": "Nissan Note automaat/CVT", "query": "nissan note automaat", "autoscout_path": "nissan/note",
     "risk": "VERHOOGD RISICO: zelfde Jatco CVT-kanttekening als Nissan Micra."},
    {"label": "Dacia Sandero automaat/EDC", "query": "dacia sandero edc", "autoscout_path": "dacia/sandero",
     "risk": "VERHOOGD RISICO: EDC-dubbelkoppelingsbak (Renault-afkomstig) kent vergelijkbare problemen als bij Renault zelf. Onderhoudshistorie navragen."},
    {"label": "Volvo V40 automaat/Geartronic", "query": "volvo v40 automaat", "autoscout_path": "volvo/v40",
     "risk": "Gemiddeld risico: de automaat zelf (Geartronic, koppelomvormer) is betrouwbaar, maar algeheel onderhoud/reparaties zijn duurder dan bij de andere merken hier -- reken dit mee in de marge."},
    {"label": "Mini (One/Cooper) automaat", "query": "mini cooper automaat", "autoscout_path": "mini/cooper",
     "risk": "Gemiddeld risico: automaat zelf doorgaans prima, maar BMW-onderdelen/onderhoud zijn relatief duur -- reken dit mee in de marge."},
    {"label": "BMW 1-serie automaat", "query": "bmw 1 serie automaat", "autoscout_path": "bmw/1er",
     "risk": "Gemiddeld risico: Steptronic-automaat is betrouwbaar, maar onderhoud/reparaties zijn duurder dan bij de budgetmerken -- reken dit mee in de marge."},
    {"label": "Audi A1 automaat/S tronic", "query": "audi a1 s tronic", "autoscout_path": "audi/a1",
     "risk": "LET OP: S tronic is hetzelfde DSG-platform als Volkswagen Polo/Seat Ibiza (concern) -- zelfde koppelingskanttekening, plus duurder onderhoud."},
    {"label": "Mercedes A-klasse automaat", "query": "mercedes a klasse automaat", "autoscout_path": "mercedes-benz/a-klasse",
     "risk": "VERHOOGD RISICO: 7G-DCT dubbelkoppelingsbak staat bekend om schokkerig schakelen/slijtage, en reparaties zijn duur. Onderhoudshistorie goed navragen."},
    {"label": "Mitsubishi Space Star automaat/CVT", "query": "mitsubishi space star automaat", "autoscout_path": "mitsubishi/space-star",
     "risk": "Laag-gemiddeld risico: CVT met redelijke reputatie, budgetvriendelijk."},
    {"label": "Chevrolet Spark/Matiz automaat", "query": "chevrolet matiz automaat", "autoscout_path": "chevrolet/matiz",
     "risk": "Gemiddeld risico: eenvoudige, betrouwbare automaat, maar merk is uit NL vertrokken -- onderdelen/support kunnen lastiger te vinden zijn."},
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

def fetch_page(query: str, offset: int, apply_location: bool = False) -> dict:
    params = (
        f"query={urllib.parse.quote(query)}"
        f"&limit={PAGE_SIZE}&offset={offset}"
        f"&sortBy=SORT_INDEX&sortOrder=DECREASING"
    )
    if apply_location and POSTCODE and DISTANCE_KM:
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


def _fetch_model_listings(model: dict, apply_location: bool) -> list:
    listings = []
    for page in range(MAX_PAGES_PER_MODEL):
        try:
            data = fetch_page(model["query"], offset=page * PAGE_SIZE, apply_location=apply_location)
        except RuntimeError as exc:
            # Eén model dat blijft weigeren (bv. rate-limiting) mag niet de
            # hele run onderuit halen -- sla dit model verder over, de rest
            # van de catalogus (en de vorige pagina's van dit model) blijft
            # gewoon meetellen.
            print(f"[WAARSCHUWING] {model['label']}: {exc}", file=sys.stderr)
            break
        page_listings = data.get("listings", [])
        for listing in page_listings:
            listing["_model_label"] = model["label"]
            listing["_model_risk"] = model["risk"]
        listings.extend(page_listings)
        if len(page_listings) < PAGE_SIZE:
            break
        polite_pause()
    return listings


def fetch_model_listings(model: dict) -> list:
    """Landelijk, voor de mediaan-referentiegroep (geen locatiefilter)."""
    return _fetch_model_listings(model, apply_location=False)


def fetch_model_listings_local(model: dict) -> list:
    """Alleen rond POSTCODE binnen DISTANCE_KM, voor de meld-groep (kansen)."""
    return _fetch_model_listings(model, apply_location=True)


# --- AutoScout24 -------------------------------------------------------
#
# GEVERIFIEERD (via scripts/probe_sources.py, handmatig gedraaid op
# 28-9-2026): AutoScout24 rendert serverside en embedt de resultatenlijst
# als schema.org JSON-LD (@graph -> SearchResultsPage -> ItemList). Elk
# item bevat: name, brand.name, model, mileageFromOdometer.value,
# vehicleTransmission, offers.price/priceCurrency/url, en
# offers.seller.@type ("AutoDealer" voor handelaren -- dit is de
# betrouwbare handelaar-detectie voor deze bron, in tegenstelling tot de
# tekstheuristiek die nodig is voor Marktplaats). Alle 35 merk/model-url's
# in MODEL_CATALOG (autoscout_path) zijn stuk voor stuk gecheckt: status
# 200 met resultaten. GEEN bouwjaar-veld beschikbaar in deze lijst-JSON --
# extract_year() valt terug op de titel-regex, wat vaak niets oplevert
# voor AutoScout24 (titels bevatten zelden een jaartal); zulke advertenties
# tellen dan simpelweg niet mee (zie passes_age_mileage_filter). Ook geen
# plaatsingsdatum beschikbaar -- geen probleem, want het hele script filtert
# niet meer op plaatsingsdatum (zie is_relevant).
AUTOSCOUT_BASE_URL = "https://www.autoscout24.nl"
AUTOSCOUT_MAX_PAGES = 15  # tot ~300 advertenties per model (pagination stopt vanzelf bij minder aanbod)

_LD_JSON_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def fetch_autoscout_page(path: str, page: int) -> str:
    url = f"{AUTOSCOUT_BASE_URL}/lst/{path}?sort=age&desc=1&page={page}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError):
        return ""  # AutoScout24 is een aanvullende bron -- niet de hele run laten falen


def parse_autoscout_items(html: str) -> list:
    items = []
    for block in _LD_JSON_PATTERN.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        graph = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in graph:
            if not isinstance(node, dict):
                continue
            item_list = (node.get("mainEntity") or {}).get("itemListElement")
            if not item_list:
                continue
            for entry in item_list:
                item = entry.get("item")
                if item:
                    items.append(item)
    return items


def normalize_autoscout_item(item: dict, model: dict) -> dict | None:
    offers = item.get("offers") or {}
    price = offers.get("price")
    url_path = offers.get("url")
    if price is None or not url_path:
        return None  # onbruikbaar zonder prijs/url, sla over i.p.v. crashen

    seller = offers.get("seller") or {}
    is_dealer = seller.get("@type") == "AutoDealer"

    transmission = item.get("vehicleTransmission", "")
    description = f"{item.get('vehicleConfiguration', '')} {transmission}"

    address = seller.get("address") or {}
    mileage = (item.get("mileageFromOdometer") or {}).get("value")

    return {
        "itemId": f"as24:{url_path}",
        "title": item.get("name", "Auto-advertentie"),
        "description": description,
        "priceInfo": {"priceType": "FIXED", "priceCents": int(round(price * 100))},
        "location": {"cityName": address.get("addressLocality", "Onbekende locatie")},
        "attributes": [],
        "extendedAttributes": [],
        "mileage": mileage,  # rechtstreeks numeriek, geëxtraheerd door extract_mileage()
        "vipUrl": url_path,  # listing_url() plakt hier de base-url voor (Marktplaats-conventie)
        "_source": "autoscout24",
        "sellerInformation": {"isDealer": is_dealer, "companyName": seller.get("name") if is_dealer else None},
        "_model_label": model["label"],
        "_model_risk": model["risk"],
    }


def fetch_autoscout_listings(model: dict) -> list:
    path = model.get("autoscout_path")
    if not path:
        return []
    listings = []
    for page in range(1, AUTOSCOUT_MAX_PAGES + 1):
        html = fetch_autoscout_page(path, page)
        if not html:
            break
        items = parse_autoscout_items(html)
        if not items:
            break
        for item in items:
            normalized = normalize_autoscout_item(item, model)
            if normalized:
                listings.append(normalized)
        if len(items) < 20:  # aanname o.b.v. waargenomen numberOfItems, geen harde garantie
            break
        polite_pause()
    return listings


# --- Filtering / extractie -------------------------------------------------

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


# Best-effort, NIET geverifieerd tegen een live response (zie docstring
# bovenin). Bekende/vermoedelijke Marktplaats-signalen voor een
# handelaarsadvertentie: een sellerInformation-blok met een bedrijfsnaam of
# een expliciete dealer-vlag, of een "Bedrijf"/"Dealer"-achtig woord in de
# advertentie zelf (sommige categorieën tonen dit als losse tekstregel i.p.v.
# een JSON-veld). Bij twijfel/geen signaal: PARTICULIER aannemen (zie
# docstring waarom dat de veilige kant is).
DEALER_TEXT_HINTS = re.compile(
    r"\bbedrijfsactiviteit\b|\bautobedrijf\b|\bautohandel\b|\bdealer\b|\bshowroom\b|\bbovag\b",
    re.IGNORECASE,
)


def is_private_seller(listing: dict) -> bool:
    seller = listing.get("sellerInformation") or listing.get("seller") or {}
    if isinstance(seller, dict):
        if seller.get("isDealer") is True or seller.get("isCompany") is True:
            return False
        if seller.get("companyName") or seller.get("sellerWebsiteUrl"):
            return False
    for attr in listing.get("attributes", []) + listing.get("extendedAttributes", []):
        key = str(attr.get("key", "")).lower()
        value = str(attr.get("value", "")).lower()
        if key in ("sellertype", "businessseller", "isdealer") and value in ("true", "dealer", "bedrijf", "1"):
            return False
    haystack = f"{listing.get('title', '')} {listing.get('description', '')}"
    if DEALER_TEXT_HINTS.search(haystack):
        return False
    return True


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


# --- Levende mediaan-referentie ---------------------------------------------
#
# Een vast bouwjaar-bin (bv. "2016-2018") is grof: een auto van begin 2016
# met 140.000 km en eentje van eind 2018 met 60.000 km belanden in dezelfde
# groep terwijl ze niets met elkaar te maken hebben, en een auto van eind
# 2018 en begin 2019 (1 maand uit elkaar) belanden juist in VERSCHILLENDE
# groepen. In plaats daarvan krijgt elke advertentie een eigen, op maat
# gemaakte vergelijkingsgroep ("peers"): andere advertenties van HETZELFDE
# model waarvan de km-stand binnen MAX_MILEAGE_DEVIATION_KM van déze
# advertentie ligt, EN het bouwjaar binnen MAX_YEAR_DEVIATION -- die tweede
# eis voorkomt dat een auto van 2007 met 60.000 km vergeleken wordt met een
# auto van 2023 met 55.000 km (zelfde km-stand, totaal andere leeftijd/
# generatie/uitrusting). De mediaan van die peers is de referentieprijs
# voor precies déze advertentie.
MAX_MILEAGE_DEVIATION_KM = int(os.environ.get("MAX_MILEAGE_DEVIATION_KM", "50000"))
MAX_YEAR_DEVIATION = int(os.environ.get("MAX_YEAR_DEVIATION", "4"))


def group_listings_by_model(listings: list) -> dict:
    by_model: dict = {}
    for listing in listings:
        by_model.setdefault(listing["_model_label"], []).append(listing)
    return by_model


def find_peer_prices(listing: dict, by_model: dict) -> list:
    year = extract_year(listing)
    mileage = extract_mileage(listing)
    if year is None or mileage is None:
        return []
    peers = []
    listing_id = listing.get("itemId")
    for other in by_model.get(listing["_model_label"], []):
        # Op itemId i.p.v. object-identiteit uitsluiten: dezelfde advertentie
        # kan zowel in de landelijke (referentie) als de lokale (meld-)fetch
        # zitten, als twee aparte dict-objecten met hetzelfde itemId.
        if other.get("itemId") == listing_id:
            continue
        other_year = extract_year(other)
        other_mileage = extract_mileage(other)
        if other_year is None or other_mileage is None:
            continue
        if abs(other_year - year) > MAX_YEAR_DEVIATION:
            continue
        if abs(other_mileage - mileage) > MAX_MILEAGE_DEVIATION_KM:
            continue
        peers.append(other.get("priceInfo", {}).get("priceCents", 0))
    return peers


def find_deals(candidates: list, by_model: dict) -> list:
    deals = []
    for listing in candidates:
        price_cents = listing.get("priceInfo", {}).get("priceCents", 0)
        if price_cents <= 0:
            continue
        peer_prices = find_peer_prices(listing, by_model)
        if len(peer_prices) < MIN_GROUP_SIZE:
            continue
        reference_cents = median(peer_prices)
        discount_pct = (1 - price_cents / reference_cents) * 100
        if discount_pct >= DISCOUNT_THRESHOLD_PCT:
            listing["_group_median_cents"] = reference_cents
            listing["_group_size"] = len(peer_prices)
            listing["_discount_pct"] = round(discount_pct, 1)
            deals.append(listing)
    return deals


def passes_age_mileage_filter(listing: dict) -> bool:
    year = extract_year(listing)
    if year is None or year <= MIN_CONSTRUCTION_YEAR:
        return False  # onbekend bouwjaar telt als "voldoet niet" (veilige kant)
    mileage = extract_mileage(listing)
    if mileage is None or mileage >= MAX_MILEAGE_KM:
        return False  # onbekende km-stand telt als "voldoet niet" (veilige kant)
    return True


def is_relevant(listing: dict) -> bool:
    return (
        is_automatic(listing)
        and passes_price_filter(listing)
        and passes_age_mileage_filter(listing)
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
    if listing.get("_source") == "autoscout24":
        return f"{AUTOSCOUT_BASE_URL}{listing.get('vipUrl', '')}"
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

    if not (POSTCODE and DISTANCE_KM):
        print(
            "[WAARSCHUWING] Geen locatiefilter actief (POSTCODE en/of "
            "DISTANCE_KM ontbreken/leeg) -- de meld-groep wordt dan ook "
            "over HEEL NEDERLAND bepaald i.p.v. lokaal rond POSTCODE. "
            "Check of de 'POSTCODE'-secret bestaat onder Settings > "
            "Secrets and variables > Actions.",
            file=sys.stderr,
        )

    # Referentiegroep (mediaan): landelijk, particulier + handelaar, van
    # zowel Marktplaats als AutoScout24. Fouten per model (bv. aanhoudende
    # rate-limiting) worden binnen fetch_model_listings zelf opgevangen en
    # overgeslagen -- één weigerend model mag de rest van de catalogus niet
    # blokkeren. Ook tussen modellen onderling een korte pauze (niet alleen
    # tussen pagina's binnen één model) -- met de 6-uurs cadans is daar ruim
    # de tijd voor.
    all_listings = []
    for model in MODEL_CATALOG:
        all_listings.extend(fetch_model_listings(model))
        polite_pause()

    autoscout_count = 0
    for model in MODEL_CATALOG:
        as24_listings = fetch_autoscout_listings(model)
        autoscout_count += len(as24_listings)
        polite_pause()
        all_listings.extend(as24_listings)

    relevant = [l for l in all_listings if is_relevant(l)]
    by_model = group_listings_by_model(relevant)

    # Meld-groep (kansen): een APARTE fetch, alleen rond POSTCODE binnen
    # DISTANCE_KM -- alleen Marktplaats (AutoScout24 heeft geen geverifieerd
    # postcode/straal-parameter, zie docstring bij fetch_autoscout_listings,
    # en is bovendien overwegend een handelaarsplatform). De mediaan komt
    # nog steeds uit de landelijke referentiegroep hierboven.
    local_listings = []
    for model in MODEL_CATALOG:
        local_listings.extend(fetch_model_listings_local(model))
        polite_pause()

    local_relevant = [l for l in local_listings if is_relevant(l)]
    private_relevant = [l for l in local_relevant if is_private_seller(l)]
    deals = find_deals(private_relevant, by_model)

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

    if POSTCODE and DISTANCE_KM:
        print(f"[INFO] Meld-groep beperkt tot {DISTANCE_KM} km rond {POSTCODE}. Referentiegroep blijft heel NL.")

    peer_counts = [len(find_peer_prices(l, by_model)) for l in private_relevant]
    evaluable_counts = [n for n in peer_counts if n >= MIN_GROUP_SIZE]
    if evaluable_counts:
        peer_stats = (
            f"min {min(evaluable_counts)}, mediaan {int(median(evaluable_counts))}, "
            f"max {max(evaluable_counts)} vergelijkbare buren per advertentie"
        )
    else:
        peer_stats = "geen enkele advertentie had genoeg vergelijkbare buren"
    print(
        f"Referentiegroep (heel NL): {len(all_listings)} advertenties opgehaald over "
        f"{len(MODEL_CATALOG)} modellen (waarvan {autoscout_count} via AutoScout24), "
        f"{len(relevant)} relevant (automaat, bouwjaar/km/budget). "
        f"Meld-groep (lokaal): {len(local_listings)} advertenties opgehaald, "
        f"{len(local_relevant)} relevant, waarvan particulier: {len(private_relevant)}, "
        f"waarvan met genoeg vergelijkbare buren om te beoordelen: {len(evaluable_counts)} ({peer_stats}). "
        f"Nieuwe kansen: {len(new_matches)}."
    )

    if new_matches:
        send_discord_notifications(new_matches)
        for listing in new_matches:
            print(
                f"  -> {listing.get('title')} | {fmt_euro(listing['priceInfo']['priceCents'])} "
                f"| -{listing['_discount_pct']}% (vs {listing['_group_size']} vergelijkbare buren) "
                f"| {listing_url(listing)}"
            )

    state = prune_state(state)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
