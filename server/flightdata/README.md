# Flight lookup tables

Used by the Flights overhead module (`fetch_flights` in `server/modules.py`). Built by `tools/make_flight_data.py`; do not edit by hand.

- `airlines.json`: ICAO airline designator to `[name, IATA code]`. From [OpenFlights](https://openflights.org/data) `airlines.dat` (Open Database License), active airlines only, with the corrections in `tools/airlines_extra.json` merged over it.
- `aircraft_types.json`: ICAO type designator to `[model name, ICAO class such as L2J, wake category]`. Model names are the most common description per type in the [Mictronics aircraft database](https://www.mictronics.de/aircraft-database/) as published by [tar1090-db](https://github.com/wiedehopf/tar1090-db); classes from tar1090-db's `icao_aircraft_types.js`.
