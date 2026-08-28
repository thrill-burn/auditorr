# Remote access & authentication

How auditorr decides who is allowed to talk to it, and what to change if you
reach it from outside your home network.

> **Upgrading from 1.7.0 or earlier?** Jump to
> [Everything returns 401 after upgrading](#everything-returns-401-after-upgrading).

---

## How access control works

auditorr's API can read your library, change your configuration, and — if you
opt in — delete torrents and files. Two rules decide who gets through:

| If you have… | auditorr serves |
| --- | --- |
| **No access key set** (the default) | Local clients only. Everything else gets `401 Unauthorized`. |
| **`AUDITORR_SECRET` set** | Anyone who sends the key, from anywhere. Everyone else gets `401`. |

"Local" means the connection arrives from your own machine or your own network:
loopback (`127.0.0.1`, `::1`), the private ranges `10.0.0.0/8`,
`172.16.0.0/12`, `192.168.0.0/16`, and IPv6 unique-local / link-local
addresses. You can add more ranges — see
[Trust an extra network](#option-2--trust-an-extra-network).

`/health` and the web UI's static files are always served, so container health
checks keep working no matter how you configure this.

---

## Reaching auditorr from outside your LAN

If you use auditorr from the same network the container runs on, there is
nothing to configure — this page doesn't apply to you.

If you reach it from anywhere else — a port forward, a VPS, a phone on mobile
data, a VPN or mesh network — pick one of the following.

### Option 1 — Set an access key

Best when auditorr is reachable from the public internet.

Generate a long random value and set it as `AUDITORR_SECRET`:

```bash
openssl rand -hex 32
```

**Docker Compose:**

```yaml
services:
  auditorr:
    environment:
      - AUDITORR_SECRET=your-long-random-value
```

**Unraid:** edit the container, switch on **Advanced View** (top right), and
fill in the `AUDITORR_SECRET` variable.

Restart the container. The next time you open the web UI it will prompt you for
the key once and remember it in that browser. Every client now needs the key —
including clients on your LAN.

Sending the key by hand:

```bash
curl -H "X-Auditorr-Secret: your-long-random-value" http://host:8677/api/results
```

> The key is accepted **only** as the `X-Auditorr-Secret` header. A `?secret=`
> query parameter used to work and was removed in 1.7.1 — query strings end up
> in reverse-proxy access logs and browser history.

### Option 2 — Trust an extra network

Best for VPN and mesh networks, where the tunnel already handles
authentication. Add the tunnel's address range to `AUDITORR_TRUSTED_NETWORKS`
and clients on it are treated as local, with no key needed.

**Tailscale:**

```yaml
      - AUDITORR_TRUSTED_NETWORKS=100.64.0.0/10
```

Tailscale assigns addresses from `100.64.0.0/10`, which is carrier-grade NAT
space rather than a private range, so it is **not** local by default. Tailscale's
IPv6 addresses *are* unique-local and pass without configuration — which is why
a tailnet can appear to work on one device and fail on another. Setting the
variable above makes both consistent.

Multiple ranges are comma-separated:

```yaml
      - AUDITORR_TRUSTED_NETWORKS=100.64.0.0/10,10.8.0.0/24
```

Entries that aren't valid CIDR notation are logged and skipped; they won't stop
auditorr from starting.

---

## Reverse proxies

auditorr judges locality by the address the connection actually arrives from.
It deliberately ignores the `X-Forwarded-For` header, because any client can
set that header to whatever it likes — trusting it would let a remote attacker
claim to be on your LAN.

The practical consequence: **a reverse proxy on the same host or LAN always
passes the local check**, because auditorr sees the proxy's address, not your
visitor's. auditorr cannot tell a LAN visitor from an internet visitor behind
your proxy.

So if the proxy is reachable from the internet, the default protection does not
extend to it. Either:

- set `AUDITORR_SECRET`, so the key is required regardless of where the request
  appears to come from, **or**
- enforce authentication at the proxy itself (basic auth, an SSO forward-auth
  provider, an allow-list).

If you also proxy qui, qBittorrent, Sonarr, or Radarr, set an **External URL**
for each so auditorr's `↗` buttons link to the proxied address while it keeps
connecting internally — see
[External URLs](configuration.md#external-urls-reverse-proxy). Keeping the two
separate is what lets you put SSO in front of those apps without breaking
auditorr's scans, since it never fetches the external address.

---

## Strict mode

`AUDITORR_REQUIRE_AUTH=true` removes the local exemption entirely: the key is
required from every client, including loopback and your LAN.

```yaml
      - AUDITORR_REQUIRE_AUTH=true
      - AUDITORR_SECRET=your-long-random-value
```

Set strict mode **without** an `AUDITORR_SECRET` and auditorr refuses all API
access rather than running open — every `/api/*` route returns
`503 auth_not_configured` and the web UI shows a setup notice. It clears itself
once you set a key and restart.

---

## Environment variable reference

| Variable | Default | Effect |
| --- | --- | --- |
| `AUDITORR_SECRET` | *(unset)* | Access key. Once set, required from every client, sent as the `X-Auditorr-Secret` header. |
| `AUDITORR_TRUSTED_NETWORKS` | *(unset)* | Comma-separated CIDRs treated as local in addition to loopback and private ranges. |
| `AUDITORR_REQUIRE_AUTH` | `false` | `true` requires the key even from local clients. Fails closed if no key is set. |

See [configuration](configuration.md) for the rest of auditorr's settings.

---

## Troubleshooting

### Everything returns 401 after upgrading

**Symptom:** the web UI won't load, or the browser asks for an access key you
never configured. Worked on 1.7.0, stopped on 1.7.1.

**Cause:** you reach auditorr from outside its LAN. Before 1.7.1, leaving
`AUDITORR_SECRET` unset disabled authentication entirely and served everyone;
now, with no key set, non-local clients are refused.

**Fix:** [set an access key](#option-1--set-an-access-key), or
[trust your tunnel's network](#option-2--trust-an-extra-network) if you connect
over a VPN or mesh network. Tailscale users usually want the second one.

### The browser keeps asking for an access key I never set

The web UI prompts for a key whenever the API answers `401`, which it also does
when you are simply not a local client. Entering a value won't help if no key is
configured on the server — fix the access itself using the section above.

If you typed a wrong value at the prompt, the browser cached it. Clear it from
the browser console and reload:

```js
localStorage.removeItem('auditorr_secret')
```

### "AUDITORR_REQUIRE_AUTH is set but AUDITORR_SECRET is not"

Strict mode is on with no key to check against, so auditorr fails closed rather
than running open. Set `AUDITORR_SECRET` and restart, or remove
`AUDITORR_REQUIRE_AUTH`. The notice disappears on its own once the server
answers again.

### I set a key, but LAN clients now get refused too

That's intended: a configured key is required from **everyone**, local included.
If you only wanted to open up remote access without prompting every device at
home, use [Option 2](#option-2--trust-an-extra-network) instead.

### Checking what the server sees

`GET /api/debug/report` reports the live auth settings under `runtime`:

```json
"runtime": {
  "auth_enabled": false,
  "auth_strict": false,
  "trusted_networks": 0
}
```

`auth_enabled` is whether an `AUDITORR_SECRET` is set, `auth_strict` whether
`AUDITORR_REQUIRE_AUTH` is on, and `trusted_networks` how many CIDRs parsed
successfully — never the values themselves. The report is privacy-scrubbed and
safe to paste into an issue.
