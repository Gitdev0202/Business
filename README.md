# Marktplaats Monitor

Checkt elke 5 minuten verschillende Marktplaats-categorieën op nieuwe
advertenties die vandaag zijn geplaatst, en stuurt alleen de advertenties die
je nog niet eerder hebt gezien naar een Discord-kanaal. Draait volledig in de
cloud via GitHub Actions — je pc hoeft niet aan te staan.

## Overzicht van alle monitors

| Monitor | Workflow | Script | State-bestand | Categorie |
| --- | --- | --- | --- | --- |
| PS5 | `monitor.yml` | `scripts/monitor.py` | `state/seen_ps5.json` | Spelcomputers \| Sony PlayStation 5 (2954) |
| iPhone | `monitor_iphones.yml` | `scripts/monitor_iphones.py` | `state/seen_iphones.json` | Mobiele telefoons \| Apple iPhone (1953) |
| MacBook | `monitor_macbooks.yml` | `scripts/monitor_macbooks.py` | `state/seen_macbooks.json` | Apple Macbooks (325) |
| Nintendo Switch 2 | `monitor_switch2.yml` | `scripts/monitor_switch2.py` | `state/seen_switch2.json` | Spelcomputers \| Nintendo Switch (2943) |
| Horloges | `monitor_watches.yml` | `scripts/monitor_watches.py` | `state/seen_watches.json` | Horloges \| Heren (1831) |

Elke monitor draait als losse GitHub Actions workflow, gebruikt dezelfde
`DISCORD_WEBHOOK_URL`-secret, en houdt zijn eigen state-bestand bij zodat
monitors elkaar niet beïnvloeden. De sectie hieronder beschrijft de
PS5-monitor als voorbeeld — het volledige filtergedrag van elke monitor staat
in de docstring bovenin het bijbehorende script.

## Hoe de PS5-monitor filtert

- **Categorie**: alleen de officiële Marktplaats-categorie voor PS5-consoles
  (id 2954). Dit sluit losse accessoires en losse games automatisch uit,
  maar laat combi-advertenties (console + controllers/games) wél toe, omdat
  die ook onder de consolecategorie vallen.
- **Vandaag geplaatst**: alleen advertenties met `date == "Vandaag"`. Let op:
  Marktplaats toont een advertentie ook als "Vandaag" wanneer een verkoper
  'm die dag heeft "gedagtopt" (opnieuw naar boven geduwd) — dat is een
  Marktplaats-eigenaardigheid, niet iets wat dit script kan omzeilen. Omdat
  we op advertentie-id dedupliceren, krijg je zo'n advertentie hoe dan ook
  maar één keer te zien.
- **Titel bevat "ps5" of "playstation 5"**: extra vangnet bovenop de
  categoriefilter.
- **Prijs**: advertenties onder €250 worden overgeslagen, BEHALVE
  "Bieden"-advertenties (open bieden zonder vraagprijs) en advertenties
  waarbij de prijs "in de omschrijving" staat — die worden altijd getoond
  omdat de prijs dan niet betrouwbaar te filteren is.
- **Geen duplicaten**: elke advertentie-id die ooit gemeld is, staat in
  `state/seen_ps5.json` en wordt nooit opnieuw gemeld. Entries ouder dan 21
  dagen worden automatisch opgeruimd zodat het bestand niet blijft groeien.

Wil je dit later aanscherpen (bv. alleen PS5 Slim, of een lagere/hogere
prijsgrens)? Pas `scripts/monitor.py` aan (zie de constanten bovenin) of zet
de `MIN_PRICE_EUR` env var in `.github/workflows/monitor.yml` op een andere
waarde.

## Setup (eenmalig, ~10 minuten)

### 1. Discord webhook aanmaken

1. Open Discord, ga naar de server en het kanaal waar je de meldingen wilt
   ontvangen.
2. Kanaalinstellingen (tandwiel-icoon naast het kanaal) → **Integraties** →
   **Webhooks** → **Nieuwe webhook**.
3. Geef 'm een naam (bv. "PS5 Monitor"), kies het kanaal, klik op
   **Webhook-URL kopiëren**. Bewaar deze URL — die heb je zo nodig.
   (Heb je geen eigen server? Maak gratis een nieuwe server aan via het
   plusje linksonder in Discord, alleen voor jezelf.)

### 2. GitHub-repository aanmaken

1. Ga naar [github.com/new](https://github.com/new) (maak gratis een account
   als je die nog niet hebt).
2. Repository-naam: bv. `ps5-marktplaats-monitor`. Zet 'm op **Private**
   (aanbevolen, hoeft niet openbaar te zijn). Geen README/gitignore
   aanvinken. Klik **Create repository**.
3. Op de lege repo-pagina klik je op **"uploading an existing file"**.
4. Sleep de hele inhoud van deze map
   (`C:\Users\djten\Documents\ps5-marktplaats-monitor`) — dus de mappen
   `.github`, `scripts`, `state` en de bestanden `README.md`, `.gitignore` —
   in het upload-vak. GitHub behoudt de mapstructuur.
5. Klik **Commit changes**.

### 3. Webhook-URL als secret toevoegen

1. In je nieuwe repo: **Settings** → **Secrets and variables** → **Actions**.
2. **New repository secret**.
3. Naam: `DISCORD_WEBHOOK_URL`. Waarde: plak de webhook-URL uit stap 1.
4. **Add secret**.

### 4. Workflow activeren en testen

1. Ga naar het tabblad **Actions**. Als GitHub vraagt om workflows in te
   schakelen, klik dat aan.
2. Klik op **PS5 Marktplaats Monitor** in de linkerlijst → **Run workflow**
   → **Run workflow** (dit is de handmatige trigger, zo hoef je niet 5
   minuten te wachten op de eerste test).
3. Na ~30 seconden zie je de run. Klik erop om de logs te checken — je ziet
   hoeveel advertenties zijn opgehaald en hoeveel er nieuw/relevant waren.
4. Check je Discord-kanaal: als er vandaag relevante PS5-advertenties
   staan, komen die nu binnen.

Vanaf nu draait de check automatisch elke 5 minuten, zonder dat je iets
hoeft te doen.

## Belangrijk om te weten

- **Interval**: 5 minuten is het snelste dat GitHub Actions' `schedule`
  ondersteunt (jouw wens van 3 minuten is technisch niet mogelijk zonder
  betaalde infrastructuur). Tijdens drukke momenten kan GitHub een run een
  paar minuten later starten dan gepland.
- **Automatisch uitschakelen**: GitHub zet scheduled workflows automatisch
  op pauze als een repository 60 dagen lang geen enkele activiteit heeft
  gehad. Omdat dit script elke run een commit doet (bij nieuwe
  advertenties) blijft de repo "actief" zodra er iets nieuws gevonden
  wordt; is er lang niets nieuws, log dan af en toe even in Actions om te
  checken dat de workflow nog aanstaat.
- **Kosten**: gratis. GitHub Actions geeft gratis accounts 2.000
  build-minuten per maand; deze check duurt ~15-20 seconden per run, dus
  bij elke 5 minuten (~8.640 runs/maand) blijf je ruim binnen die grens.
- **Aanpassen**: alle instellingen (minimumprijs, categorie, titel-check)
  staan bovenin `scripts/monitor.py`. Wijzig het bestand in GitHub (potlood-
  icoon bij het bestand) en commit — de volgende run gebruikt automatisch de
  nieuwe instellingen.
