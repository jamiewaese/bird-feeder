# Bird Feeder Pi security hardening

Date: 2026-08-09
Host: `bird-feeder-pi`, `192.168.1.20`
Co-hosted service: TRMNL Terminus

> Publication note: network names, addresses, hostnames, account names, and
> hardware identifiers in this document are sanitized examples.

## Result

The Bird Feeder gallery is no longer directly exposed as a Python HTTP server.
It is reachable through authenticated TLS on TCP/443, with TCP/80 used only for
redirects. The backend binds to `127.0.0.1:8080`, and the host firewall blocks
direct LAN access to that port.

Terminus was kept online throughout the migration. Its web service remains
available to `PRIMARY_LAN` on TCP/2300. Its container-published PostgreSQL and
Valkey ports are blocked at `wlan0`; their Docker-internal connectivity was not
changed.

## Controls installed

- Nginx TLS reverse proxy with a private local CA and bcrypt HTTP Basic
  credentials.
- Root-only CA/server private keys and plaintext recovery credential.
- Host/Origin allowlisting and CSRF tokens for star/delete mutations.
- Content Security Policy, clickjacking denial, MIME sniffing denial,
  same-origin resource policy, restrictive referrer and permissions policies.
- No third-party browser resources; Google Fonts was removed.
- Nginx request-body, connection, rate, and timeout limits.
- `birdgallery` and `birdclassifier` service identities with a shared media-only
  group.
- Root-owned application code under `/opt/bird-feeder`.
- Strict systemd filesystem, device, kernel, namespace, capability, executable
  memory, address-family, and process visibility restrictions.
- Key-only SSH for `birdadmin`; password/root login, X11, and TCP forwarding are
  disabled.
- Idempotent IPv4/IPv6 firewall chains installed by
  `pi-host-firewall.service`, ordered after and tied to Docker restarts.

## Network policy

From `wlan0`:

- LAN IPv4 `192.168.1.0/24`: SSH 22, HTTP redirect 80, HTTPS 443.
- LAN IPv4 `192.168.1.0/24` through Docker: Terminus 2300 only.
- Private/link-local IPv6: SSH 22, HTTP 80, HTTPS 443, Terminus 2300.
- DHCP, mDNS, ICMP, established and related traffic required for normal host
  operation.
- Other unsolicited inbound traffic is dropped.

Outbound traffic is unchanged so Terminus and OpenAI classification continue
to operate.

## Client enrollment

The public CA is [`../bird-feeder-ca.crt`](../bird-feeder-ca.crt). The username
is configured during deployment; retrieve the password over key-authenticated SSH:

```bash
ssh birdadmin@192.168.1.20 'sudo cat /etc/bird-feeder/gallery-credentials.txt'
```

On macOS, import the CA into the System keychain and mark it Always Trust. On
iOS/iPadOS, install the certificate profile, then enable full trust under
Settings > General > About > Certificate Trust Settings.

## OpenAI boundary

The environment file remains `/etc/bird-feeder/openai.env`, root-owned and mode
`0600`. systemd reads it for the one-shot classifier; the gallery service never
receives it. API requests use HTTPS, `store: false`, a bounded batch, bounded
image size, bounded output tokens, no automatic retry, and a local monthly
attempt/cost ledger.

The classifier now uses a dedicated OpenAI project named `Bird Feeder`, separate
from the Default project and its other API keys. Its replacement key is limited
to model-request permissions; the prior Default-project Bird Feeder key was
revoked after the replacement completed a real image classification. The
project permits only `gpt-5.4-mini`, enforces a $10 monthly spend limit with a
100% alert, and limits that model to 20,000 tokens/minute and 10 requests/minute.
The Pi's temporary rollback key copy and the local transfer file were removed
after verification.

OpenAI may retain API customer content in abuse-monitoring logs for up to 30
days unless the project is approved for a more restrictive data-retention
control. Automatic camera retrieval is still unimplemented and remains outside
this review.

The credential-rotation test attempted one catalogued snapshot and succeeded
once with no failures. Its estimated API cost was $0.00154725.

## Rollback material

Pre-hardening Bird Feeder unit files are retained on the Pi with the suffix
`.pre-security-20260809`. The original application checkout remains at
`/home/birdadmin/bird-feeder`; deployed services use `/opt/bird-feeder`.
