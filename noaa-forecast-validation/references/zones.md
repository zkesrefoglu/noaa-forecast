# Zones Reference

Everything about which temperature zones this project tracks, why, and how they map to the vendor file.

## The seven electricity zones

This is the authoritative list. `zones.csv` matches this.

| zone | c_region | ICAO | WBAN | lat | lon | what it represents |
|------|----------|------|------|-----|-----|--------------------|
| DCA  | 1  | KDCA | 13743 | 38.8521  | -77.0377 | Washington National — DC metro load |
| ABE  | 3  | KABE | 14737 | 40.6521  | -75.4408 | Allentown/Lehigh Valley — PPL territory |
| PHL  | 4  | KPHL | 13739 | 39.8721  | -75.2411 | Philadelphia — PECO territory |
| PIT  | 5  | KPIT | 94823 | 40.4915  | -80.2329 | Pittsburgh — Duquesne / First Energy PA |
| CLE  | 8  | KCLE | 14820 | 41.4117  | -81.8497 | Cleveland — First Energy Ohio |
| LCK  | 9  | KLCK | 13812 | 39.8138  | -82.9278 | Columbus Rickenbacker — AEP Ohio |
| EWR  | 11 | KEWR | 14734 | 40.6925  | -74.1687 | Newark — PSE&G / NJ |

## Excluded on purpose

These appear in the vendor's `C_REGION` column but are out of scope.

| c_region | vendor code | why excluded |
|----------|-------------|--------------|
| 2  | BWI | Baltimore — not used by the scheduling model Ziya operates |
| 6  | TOL | Toledo — natural gas, not electricity |
| 7  | CAK | Akron/Canton — natural gas, not electricity |
| 10 | ERIE | Erie PA — natural gas, not electricity |

**Don't add these back** without explicit direction from Ziya. The electricity and natural gas books are separate concerns operationally, and conflating them in this pipeline creates bogus comparisons.

## Why airport coordinates

NOAA's DWML forecast endpoint takes a lat/lon and returns the forecast for the nearest grid cell. Iowa Mesonet's ASOS endpoint takes an ICAO station code. To make the join honest, both sides have to describe the same physical point. Airports have precisely known coordinates published by the FAA, and ASOS observations come from sensors physically at those airports. Using the airport lat/lon for NOAA means both forecast and truth describe the same cell on the Earth.

Alternatives that were considered and rejected:
- **Zone centroids** — vague, not tied to any ASOS station, would require interpolating truth across multiple stations. More complexity, less signal.
- **Vendor's lat/lon if present** — vendor doesn't publish the coordinate model they use. Asking them invites a political conversation we don't need to have.

## Why these seven, specifically

The vendor file is an internal scheduling artifact. The seven zones above are the ones that feed the electricity load model Ziya maintains. BWI appears in the file but the model doesn't consume it. TOL/CAK/ERIE appear because the same vendor and same file also support the natural gas book, which is run by a different team with different scheduling needs.

The scoring question for electricity is: does NOAA match or beat vendor on these seven? That's it.

## Name history (for context, not for action)

Before the pipeline was live, zone slugs followed the internal nickname convention: PPL (Allentown), PCO (Philly), FEO (FirstEnergy Ohio, Cleveland), NJ (Newark). Those names survived briefly in an early draft of `zones.csv`.

They were renamed to airport codes (ABE, PHL, CLE, EWR) on 2026-04-19 for three reasons:

1. **Clean join with ASOS.** ASOS only speaks ICAO codes; aligning on airport codes removes a translation layer.
2. **Unambiguous.** "PPL" is a utility holding company; "ABE" is a specific airport. No confusion about what "the PPL zone" means across departments.
3. **Cleaner in logs and reports.** "NOAA at EWR" reads the same way as "ASOS at KEWR" — everyone knows what airport is being described.

The rename was surgical: `zones.csv` edited, stale `data/<OLD_ZONE>/` directories git-rm'd, next hourly NOAA run repopulated with new names. No backward compatibility — there is no pre-rename history to preserve.

## Adding or removing a zone

If Ziya ever asks to add or remove a zone (more electricity zones come under scope, or a zone is retired):

1. Edit `zones.csv`. Keep the schema stable (`zone, c_region, icao, wban, lat, lon`).
2. If removing: `git rm -r data/<OLD_ZONE>/` to drop its historical snapshots.
3. If adding: the next hourly NOAA workflow run will create `data/<NEW_ZONE>/` automatically.
4. Verify `asos_truth.py` handles the new ICAO by running a single manual trigger.
5. Score_daily picks up the new zone automatically via `zones.csv`.

**Don't skip step 1.** Everything downstream reads `zones.csv`; out-of-band additions break invariants.
