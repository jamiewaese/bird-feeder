# Phase 3C: idempotent importer and Raspberry Pi gallery

Date: 2026-08-09
Deployment host: Raspberry Pi 3 Model B Rev 1.2
LAN address: `192.168.1.20` on PRIMARY_LAN

## Goal

Build the storage and presentation portions of the replacement system without
pretending that the camera's FAT32 directories are ordinary network shares.
The result must accept the exact observed `video/` and `snaps/` layout, avoid
duplicate copies on repeated runs, and keep the eventual proprietary camera
transport separate from the library and website.

## Source boundary

`camera.sdcard.MediaSource` is the small interface between acquisition and
storage. A source lists immutable media objects and opens one object as a binary
stream. `FilesystemMediaSource` implements that contract for a mounted card or
backup. A future UBox network source can implement the same contract without
changing the importer, database, gallery, or scheduler.

The filesystem source accepts both:

- canonical camera paths such as `video/260809/...mp4`;
- the owner's backup alias `videos/260809/...mp4`.

Both normalize to canonical `video/` paths in the Pi library. The source is
never renamed or modified.

## Import behavior

`MediaImporter` stores files beneath:

```text
/srv/bird-feeder/
    catalog.sqlite3
    media/
        yard/
            video/YYMMDD/*.mp4
            snaps/YYMMDD/*.jpg
```

Properties:

- imports only names matching the observed B4 layout;
- ignores unrelated files and symlinks;
- uses source ID plus source-relative path as the stable identity;
- skips objects whose size and modification time are unchanged;
- hashes each copied object with SHA-256;
- copies to a temporary file, flushes it, validates its size, then atomically
  replaces the library path;
- records neutral parsed fields without overclaiming the unknown filename
  semantics;
- does not delete source files or library files.

## Backup import

The owner authorized use of an offline backup made before the camera card
was reformatted.

| Kind | Count | Bytes |
| --- | ---: | ---: |
| MP4 video | 56 | 223,215,616 |
| JPEG snapshot | 74 | 4,124,781 |
| Total objects | 130 | 227,340,397 |

The importer reported 130 discovered, 130 imported, and zero failures. The
catalog contains 74 distinct capture keys: 56 paired snapshot/video captures
and 18 snapshot-only captures.

The library was staged in a temporary local directory. Before service startup:

- the staged and Pi `catalog.sqlite3` SHA-256 values matched;
- the Pi contained 56 MP4s and 74 JPEGs;
- a checksum-mode dry-run reported no difference between staged and Pi media.

## Gallery

`web.app` uses only the Python standard library. It provides:

- newest-first cards pairing snapshots with videos;
- 640×360 JPEG thumbnails;
- direct MP4 playback with single byte-range support for browser seeking;
- `/api/media` for a small JSON representation;
- `/healthz` for service monitoring;
- catalog checks before serving a file;
- traversal rejection and containment checks.

The systemd service is:

```text
bird-feeder-gallery.service
```

It runs as the unprivileged `birdgallery` user, is enabled at boot, restarts on
failure, and applies basic systemd filesystem/process hardening. The PRIMARY_LAN-only
LAN URL is:

```text
http://192.168.1.20:8080/
```

## Verification

- Full automated suite: 32 tests passing after backup-alias coverage was added.
- Resource warnings: treated as test failures; none remained.
- Service: enabled and active.
- Health: HTTP 200 with 74 items.
- Index: HTTP 200.
- Real MP4 range: HTTP 206.
- Browser QA: real snapshot dimensions were 640×360 and no console errors
  were reported.
- Pi power after deployment: `get_throttled=0x0`, 51.5°C.

## Known limitation

The camera does not expose `/video` and `/snaps` through FTP, SMB, HTTP, SSH,
or another discovered network filesystem. Those are paths on its FAT32 card.
The official UBox listing advertises TF playback, and direct local playback was
observed, but the vendor describes proprietary, dynamically keyed transport and
does not publish a compatible SDK.

Therefore the once-daily timer is not enabled yet. Enabling a filesystem job
against a nonexistent camera mount would falsely report success while importing
nothing.

## Next experiment

Implement a `UBoxSdSource` that can enumerate and retrieve camera-card objects.
Because the owner does not need another packet-capture session, prefer
software-side discovery first: locate an official vendor SDK or inspect an
official Android application artifact for its TF list/playback implementation.
If neither yields a supported download operation, document that the observed
TF playback stream must be reconstructed rather than scraped as files.
