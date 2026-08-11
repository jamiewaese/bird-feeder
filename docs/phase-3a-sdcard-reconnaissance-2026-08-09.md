# Phase 3A — camera microSD reconnaissance

## Outcome

The B4 already records useful, directly playable motion clips and matching
thumbnails to its microSD card. This creates a practical replacement-client
path that does not initially depend on decoding the live media transport:

1. discover a supported network method for listing/downloading SD media;
2. import new clips idempotently to the Raspberry Pi;
3. classify and retain likely bird clips; and
4. present them in a Pi-hosted local web gallery.

Physical card access proves the media format, but it does **not** yet prove the
camera exposes its filesystem as SMB, FTP, HTTP, or another network “root
folder.” Phase 1 found no inbound TCP service. Remote daily ingestion may
therefore require reproducing UBox's SD-card playback/download requests over its
binary protocol.

## Original card layout

The repurposed card was a 31.1 GB Raspberry Pi OS card:

```text
MBR disk, 31.1 GB total
├─ 536.9 MB FAT32, label bootfs
└─ 30.6 GB Linux partition
```

The B4 wrote recordings only to the small FAT32 partition. At inspection time
that partition was 75.7% full, even though most of the physical card was in the
old Linux partition and unavailable as FAT32 storage.

## Camera-created files

```text
video/
    YYMMDD/
        HHMMSS_mmm_DDD_P.mp4
snaps/
    YYMMDD/
        HHMMSS_mmm_DDD_P.jpg
ubia_record.db
ubia_record.dup
record.jpg
logfile.txt
```

Observed inventory before formatting:

- 74 user-visible MP4 clips under eight dated directories;
- 74 matching JPEG snapshots;
- approximately 290 MB of video and 4 MB of snapshots; and
- two identical 512 KB binary index files, `ubia_record.db` and
  `ubia_record.dup`.

The `.db` files are not SQLite. Their duplication suggests a primary/mirror
recording index, but their format is unknown.

## Filename interpretation

Example:

```text
video/260809/141537_530_061_P.mp4
snaps/260809/141537_530_061_P.jpg
```

Observed correlation supports:

| Segment | Interpretation | Confidence |
| --- | --- | --- |
| `260809` | date `2026-08-09` | High |
| `141537` | start time `14:15:37` | High |
| `530` | millisecond/subsecond component | Medium |
| `061` | nominal duration in seconds | High; sample duration was 61.923 s |
| `P` | event/type flag, possibly PIR | Low; not decoded |

Each MP4 and JPEG pair shares the same basename.

## Media format

Representative MP4 clips contain:

| Property | Observed value |
| --- | --- |
| Container | ISO BMFF / MP4 v2 |
| Video codec | H.264 |
| Resolution | 2304 × 1296 |
| Frame rate | approximately 15 fps |
| Audio codec | AAC |
| Audio | mono, 16 kHz |

Representative snapshots are baseline JPEG images at 640 × 360.

`ffprobe` reported a few malformed/empty H.264 access units in sampled clips,
but it decoded the streams and metadata. The importer should tolerate minor
container/stream irregularities rather than rejecting an otherwise playable
clip.

## Card reformat

The owner confirmed that the media had been backed up elsewhere and authorized
erasing the repurposed card. On 2026-08-09 the verified removable device
`/dev/disk8` was repartitioned as:

```text
MBR disk, 31.1 GB total
└─ 31.1 GB FAT32, label BIRDFEEDER
```

Post-format verification showed 31.1 GB total/free space and a writable FAT32
filesystem. The card was safely ejected. The old Pi boot files, Linux
partition, camera index, and on-card media were removed; the owner's external
backup is the recovery copy.

## Proposed importer boundary

The eventual Raspberry Pi importer should treat the camera/card as read-only
and maintain its own state:

- identify clips by content hash plus relative source path;
- copy to a temporary filename and atomically rename after verification;
- never delete a camera-side file during initial deployment;
- retain original MP4 and JPEG bytes;
- extract timestamps, duration, codec, and dimensions into a local catalog;
- generate web thumbnails separately from originals;
- run bird detection as an independent stage; and
- apply storage retention only to the Pi copy after explicit policy is set.

This design works both for a future network downloader and for a manual mounted
card importer.

## Next experiment

After the B4 is paired to the stable PRIMARY_LAN 2.4 GHz network and records at least
one new event:

1. confirm it recreated `video/`, `snaps/`, and its index files on the clean
   FAT32 card;
2. capture UBox while opening the SD/TF playback view and selecting one clip;
3. determine whether listing and download use local UDP discovery, the selected
   Access Service peer, or another endpoint; and
4. implement only the observed listing/download interface.

The first website should be LAN-only on the Pi. Public Internet publishing,
accounts, and remote access are separate decisions and are not assumed.
