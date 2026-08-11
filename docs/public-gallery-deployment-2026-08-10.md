# Public gallery deployment

Date: 2026-08-10

## Boundary

The public gallery is a second process, separate from the authenticated LAN
administrator gallery. It listens only on `127.0.0.1:8081` and is intended to
be reached solely by `cloudflared` on the same Pi.

Public mode:

- accepts `GET`, `HEAD`, and same-origin, CSRF-protected `POST /api/stars`;
- shows Star controls on gallery cards and watch pages; browser-local storage
  makes the control toggle one contribution to the shared counter;
- omits Delete controls and rejects public delete requests with `405`;
- does not expose `/healthz` or `/api/media`;
- returns `X-Robots-Tag: noindex, nofollow, noarchive` and a disallowing
  `robots.txt`;
- shows `Toronto` rather than the more precise neighborhood;
- returns `Cache-Control: no-store` for pages and ordinary media, while prepared
  share attachments use a one-hour private browser cache and remain ineligible
  for shared proxy/CDN caching;
- serves an existing phone-compatible cache without generating files during a
  public request, returning `503` rather than exposing an incompatible original
  MP4 when the prepared copy is not ready.

The systemd unit still prevents application-code writes and allows only
loopback IP networking. It grants `/srv/bird-feeder` write access because
SQLite must create journal files beside the catalog when a star is added.
Application routing remains the boundary that prevents public media deletion;
the external backup is therefore especially important.

## Pi preparation

Do not continue until an external backup filesystem is mounted at
`/mnt/bird-feeder-backup`. The backup unit deliberately uses both
`ConditionPathIsMountPoint` and `--require-mount`; this prevents an absent drive
from silently filling the Pi's system microSD.

Create the service identities and install the units:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin birdpublic
sudo useradd --system --no-create-home --shell /usr/sbin/nologin birdbackup
sudo usermod -a -G birdfeeder birdpublic
sudo usermod -a -G birdfeeder birdbackup
sudo chown birdbackup:birdfeeder /mnt/bird-feeder-backup
sudo chmod 0750 /mnt/bird-feeder-backup
sudo install -o root -g root -m 0644 \
  deploy/systemd/bird-feeder-gallery-public.service \
  /etc/systemd/system/bird-feeder-gallery-public.service
sudo install -o root -g root -m 0644 \
  deploy/systemd/bird-feeder-backup.service \
  deploy/systemd/bird-feeder-backup.timer \
  /etc/systemd/system/
```

Copy `public-gallery.env.example` to
`/etc/bird-feeder/public-gallery.env`, replace the example hostname, and make
the file root-owned mode `0644`. Start the existing LAN service once before the
public service so it performs the one-time migration from boolean star rows to
star counters. The public unit now fails fast unless both the catalog and its
containing directory are group-writable for SQLite journaling.

Verify backup and the loopback-only public process before creating public DNS:

```bash
sudo systemctl daemon-reload
sudo systemctl start bird-feeder-backup.service
sudo systemctl enable --now bird-feeder-backup.timer
sudo systemctl enable --now bird-feeder-gallery-public.service
curl -fsS -H 'Host: backyard-birds.ca' http://127.0.0.1:8081/ >/dev/null
curl -i -X POST -H 'Host: backyard-birds.ca' http://127.0.0.1:8081/api/delete
```

The first request must return `200`; the delete POST must return `405` with
`Allow: GET, HEAD`. Add a star through the browser and reload to confirm its
counter persists.

## Cloudflare Tunnel

Create a named production tunnel, copy `deploy/cloudflare/config.yml.example`
to `/etc/cloudflared/config.yml`, and replace the UUID and hostname. Keep the
tunnel credential root-readable only. The final catch-all ingress rule must
remain `http_status:404`.

Do not create a Cloudflare Access application. The published hostname is
intentionally open to the web. Do not forward router ports 80, 443, or 8081.

Before inviting anyone:

1. Create a Cache Rule that bypasses cache for the entire public hostname.
2. Confirm HTML, JPEG, and MP4 responses show `CF-Cache-Status: BYPASS` and
   `Cache-Control: no-store`.
3. Enable Bot Fight Mode if it does not interfere with playback or Share.
4. Add a conservative rate-limit rule for abnormal bursts; test video seeking
   before leaving the rule enabled.
5. Test from cellular data, including playback, seeking, download, and iOS
   Share.
6. Confirm `/healthz` and `/api/media` return `404`; `POST /api/delete` and
   unsupported PUT, PATCH, DELETE, OPTIONS, and TRACE requests return `405`.
   `POST /api/stars` is the only public mutation route.

Stopping `cloudflared` must remove public access without affecting the LAN
administrator gallery.
