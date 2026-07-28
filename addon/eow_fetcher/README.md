# EyeOnWater Fetcher (Home Assistant add-on)

Pulls your EyeOnWater meter readings and writes them into Home Assistant's
statistics (`eyeonwater:water_meter_<id>`), so the Energy Dashboard shows water
usage — **without** the official integration.

## Why this exists

EyeOnWater sits behind an Imperva WAF. Requests from Home Assistant's **core
container** (bridge network) get bounced to `/login` and never authenticate, so
the HACS integration discovers 0 meters. The exact same request from a
**host-networked** context (plain `curl`, an external machine, and this add-on)
authenticates fine. This add-on runs `host_network: true`, so it's on the good
path, fetches the data with a normal client, and pushes it into HA. No browser
fingerprint tricks, nothing to keep chasing.

## Install (HA Green / HAOS)

1. Copy the whole `eow_fetcher/` folder into your HA config's **`/addons`**
   share (via the Samba add-on: the `addons` share; or the SSH add-on:
   `/addons/eow_fetcher`).
2. **Settings → Add-ons → Add-on Store → ⋮ (top right) → Check for updates**,
   then reload the page. "EyeOnWater Fetcher" appears under **Local add-ons**.
3. Open it → **Install** (first build takes a couple of minutes).

## Configure

Create a Home Assistant **long-lived access token**:
Profile (your name, bottom-left) → **Security** → **Long-lived access tokens**
→ **Create Token**. Copy it.

Then in the add-on's **Configuration** tab:

| Option | Value |
|---|---|
| `username` | your EyeOnWater email |
| `password` | your EyeOnWater password |
| `ha_token` | the long-lived token you just created |
| `ha_url` | `http://localhost:8123` (if pushes fail, use `http://<HA-IP>:8123`) |
| `hostname` | `eyeonwater.com` (US) or `eyeonwater.ca` (Canada) |
| `days` | `3` for normal runs; set high once (e.g. `365`) to backfill history, then lower it |
| `interval_minutes` | `30` (how often to fetch) |

Save, then **Start** the add-on. Enable **Start on boot** and **Watchdog**.

## Verify

Check the add-on **Log** tab — you should see `discovered 1 meter(s)` and
`pushed N points to eyeonwater:water_meter_...`. Then:

**Settings → Dashboards → Energy → Water consumption → Add** the
`eyeonwater:water_meter_<id>` statistic.

## Notes

- `import_statistics` upserts by timestamp, so re-running is safe/idempotent.
- To backfill a long history: set `days: 365`, start, wait one cycle, then set
  `days: 3` again.
- Data appears roughly a day behind real time — that's how EyeOnWater reports.
