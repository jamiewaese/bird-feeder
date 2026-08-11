# Phase 3G: simplified identification cards

Date: 2026-08-09
Deployment: authenticated `https://192.168.1.20/` on PRIMARY_LAN

## Changes

The gallery now presents the classifier's best identification directly instead
of qualifying it with `Likely identification` or `Uncertain identification`
badges.

Home-page cards retain the useful compact fields—common name, scientific name,
sex, age, and count—but no longer show:

- the Observed behavior field;
- the Species Note block.

The compact stat strip is ordered `Sex · Age · Count`, placing Count last.

The underlying certainty, behavior, evidence, notes, and interesting-fact data
remain in SQLite and in the media API. Observed behavior and the Species Note
also remain available on the dedicated watch page, where there is more room for
detail. This is a presentation change only; no classification data was deleted.

## Verification

- Focused gallery suite: 10 tests passing.
- Tests confirm that certainty badges, Observed, and Species Note are absent
  from the home card while detailed watch-page metadata and API fields remain.
- Live verification used a temporary localhost-only SSH tunnel to the backend;
  the firewall remained closed to direct TCP/8080 access.
- The gallery service now explicitly accepts the reverse proxy's LAN Host value
  while remaining bound to `127.0.0.1:8080` behind nginx authentication.
