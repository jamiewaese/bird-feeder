# Phase 3D: starring, downloads, and dedicated watch mode

Date: 2026-08-09
Deployment: `http://192.168.1.20:8080/` on PRIMARY_LAN

## Requested changes

1. Star a video from its home-page card and from within the video screen.
2. Download a video.
3. Return from video watch mode to the main gallery.
4. Show capture time as the card eyebrow and date alone as the title, leaving
   room for a future `Date: Species` title.

## Persistent star model

Stars are not stored in browser local storage. The gallery creates a small
`stars` table in `/srv/bird-feeder/catalog.sqlite3`:

```text
(source_id, pair_key) -> starred_at
```

This makes the state consistent across browsers and between the home and watch
screens. The pair-level identity also keeps a future retention policy from
deleting a starred video's associated snapshot.

The browser sends an idempotent JSON mutation to `/api/stars`. The server
requires `application/json`, limits the request body to 4 KB, validates field
types, and confirms that the pair contains a catalogued video before writing.
No cross-origin access headers are emitted.

## Navigation and download behavior

`Play video` now opens:

```text
/watch/<catalogued-video-path>
```

The responsive watch screen includes:

- a poster image from the paired snapshot;
- native HTML5 video controls and inline mobile playback;
- a visible `Back to gallery` link;
- the same persistent Star/Starred control;
- a `Download video` action.

Downloads use a separate `/download/` route with
`Content-Disposition: attachment`. Playback remains on `/media/` and retains
single-range HTTP support for browser seeking.

## Card hierarchy

The source/camera label was removed from the card eyebrow. Each card now uses:

```text
2:33 PM
August 9, 2026
```

The second line can later become `August 9, 2026: Northern Cardinal` without
changing capture metadata or the star model.

## Verification

- Focused gallery suite: six tests covering card hierarchy, watch controls,
  persistent star toggling, attachment downloads, byte ranges, and traversal
  rejection.
- Full project suite: 35 tests passing with resource warnings treated as
  failures.
- Live home page: real 640×360 thumbnails, time eyebrow, date title, and all
  three actions rendered.
- Live watch page: real video metadata loaded as a 49-second clip; Back, Star,
  and Download were visible.
- Live star flow: the newest card toggled to Starred and the watch page reflected
  it; the star remained set across a real gallery-service restart. QA then
  toggled it off and returned the catalog to its original state.
- Responsive check: both screens inspected at 390×844; controls wrapped without
  horizontal overflow and the player remained usable.
- Pi after deployment: service active; power throttle flags remained `0x0`.

## Remaining work

These UX features operate on imported media. They do not change the current
acquisition limitation: the proprietary UBox SD source adapter is still needed
before the Pi can retrieve new camera-card recordings automatically each day.
