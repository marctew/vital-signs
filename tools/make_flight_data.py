"""Build the lookup tables the Flights module uses from open aircraft data.

    python tools/make_flight_data.py AIRLINES_DAT AIRCRAFT_CSV_GZ TYPES_JS_GZ [OUT_DIR]

AIRLINES_DAT     OpenFlights airlines.dat
                 https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat
AIRCRAFT_CSV_GZ  tar1090-db aircraft.csv.gz (Mictronics database)
                 https://github.com/wiedehopf/tar1090-db/raw/refs/heads/csv/aircraft.csv.gz
TYPES_JS_GZ      tar1090-db db/icao_aircraft_types.js (gzipped JSON despite the name)
                 https://raw.githubusercontent.com/wiedehopf/tar1090-db/master/db/icao_aircraft_types.js

Only airlines OpenFlights marks active are kept, then tools/airlines_extra.json
(hand-checked corrections and additions) is merged over them.

Writes airlines.json (ICAO designator -> [name, IATA]) and aircraft_types.json
(ICAO type code -> [model name, class such as L2J, wake category]) to OUT_DIR,
by default server/flightdata. Only the per-type summary is kept, not the
per-aircraft rows.
"""
import collections
import csv
import gzip
import json
import sys
from pathlib import Path


def airlines(path):
    out = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 8:
                continue
            name, iata, icao, active = row[1], row[3], row[4], row[7]
            iata = "" if iata in ("\\N", "-", "") else iata
            if len(icao) != 3 or not icao.isalpha() or icao == "N/A":
                continue
            # Defunct airlines' designators are often reused, so a stale name is worse than none.
            if active == "Y" and icao not in out:
                out[icao] = [name.strip(), iata]
    extra = json.loads((Path(__file__).with_name("airlines_extra.json")).read_text(encoding="utf-8"))
    out.update({code: value for code, value in extra.items() if not code.startswith("_")})
    return dict(sorted(out.items()))


def tidy(desc):
    """'AIRBUS A-320neo' -> 'Airbus A-320neo': capitalise words that are all upper-case letters."""
    words = []
    for word in desc.split():
        if word.isalpha() and word.isupper() and len(word) > 2:
            word = word.capitalize()
        words.append(word)
    return " ".join(words)


def aircraft_types(csv_gz, types_gz):
    names = collections.defaultdict(collections.Counter)
    with gzip.open(csv_gz, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split(";")
            if len(parts) > 4 and parts[2] and parts[4]:
                names[parts[2]][parts[4].strip()] += 1
    with gzip.open(types_gz, "rt", encoding="utf-8") as f:
        classes = json.load(f)
    out = {}
    for code in sorted(set(names) | set(classes)):
        if code == "ZZZZ":
            continue
        name = tidy(names[code].most_common(1)[0][0]) if names[code] else ""
        cls = classes.get(code, {})
        out[code] = [name, cls.get("desc", ""), cls.get("wtc", "")]
    return out


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    out_dir = Path(sys.argv[4] if len(sys.argv) > 4 else Path(__file__).resolve().parent.parent / "server" / "flightdata")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (("airlines.json", airlines(sys.argv[1])),
                       ("aircraft_types.json", aircraft_types(sys.argv[2], sys.argv[3]))):
        (out_dir / name).write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{name}: {len(data)} entries")


if __name__ == "__main__":
    main()
