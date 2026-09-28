"""
Voegt twee versies van state/seen_cars.json semantisch samen (unie van de
JSON-dicts), i.p.v. een tekstuele git-merge/rebase. Wordt alleen aangeroepen
door .github/workflows/monitor_cars.yml als een gewone git-push faalt door
een content-conflict -- dat kan gebeuren omdat de auto-monitor bewust een
lange run is (tot enkele uren), wat de kans vergroot dat een andere push
naar main hetzelfde bestand in de tussentijd ook wijzigde.

Gebruik: python3 scripts/merge_cars_state.py <ons-bestand> <state-bestand>
Schrijft het resultaat terug naar <state-bestand>.
"""

import json
import sys


def merge_state(ours_path: str, theirs_path: str) -> None:
    with open(ours_path, encoding="utf-8") as f:
        ours = json.load(f)
    with open(theirs_path, encoding="utf-8") as f:
        theirs = json.load(f)

    merged = dict(theirs)
    merged.update(ours)  # unie van beide kanten; bij overlap wint onze net gevonden data

    with open(theirs_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, sort_keys=True)
        f.write("\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Gebruik: merge_cars_state.py <ons-bestand> <state-bestand>", file=sys.stderr)
        sys.exit(1)
    merge_state(sys.argv[1], sys.argv[2])
