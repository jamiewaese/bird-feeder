# Phase 3F: compact gallery header

Date: 2026-08-09
Deployment: `http://192.168.1.20:8080/` on PRIMARY_LAN

## Changes

The gallery header was revised after live use showed that it consumed too much
vertical space and included unnecessary promotional copy.

- Replaced `Your local bird feeder archive` with the useful location label
  `Toronto`.
- Removed the descriptive subheader.
- Removed the redundant `The archive` heading.
- Removed the `Showing X of Y captures` sentence.
- Kept the compact capture, star, and identification summary pills.
- Put `Backyard Birds` on one line at desktop widths and placed the summary
  pills beside it.
- Reduced header and archive padding so the controls and first card row appear
  substantially higher in the initial viewport.
- Preserved natural title wrapping and stacked summary pills on narrow phones.

The date controls, sorting, filters, stars, downloads, watch mode, and confirmed
deletion behavior are unchanged.

## Verification

- Focused gallery suite: eight tests passing, including explicit checks that
  the removed copy is absent and `Toronto` is present.
- Python compilation and whitespace validation passed.
