# Water Monitoring Runbook (self-hosted EyeOnWater + irrigation alerts)

**Audience:** future-you, picking this up months/years later.
**Last updated:** 2026-07-28.
**Repo:** `arhee/eyeonwater` (fork), branch `fix/dedicated-clientsession-waf`, PR #1.

This documents a working setup that pulls EyeOnWater water data into Home
Assistant **without** the normal HACS integration (which is blocked — see below),
derives irrigation usage, and alerts on a stuck irrigation valve.

---

## 1. TL;DR — what exists and where

| Thing | Value |
|---|---|
| Home Assistant | `http://<HA-IP>:8123` (aka `homeassistant.local:8123`), HA Green / HAOS |
| Water meter | id `<meter-id>`, uuid `<meter-uuid>`, native unit `cf` (ft³) |
| EyeOnWater account | `<eyeonwater-account-email>` (credentials live only in the add-on config) |

> The concrete meter id / uuid / account email are intentionally **not** in this
> (public) repo. Find them in the add-on's Configuration tab, or by running
> `scripts/check_connection.py` with your credentials. The statistic IDs below
> embed the meter id — substitute your own.
| The data pipeline | **Add-on** "EyeOnWater Fetcher" (slug `local_eow_fetcher`), NOT the HACS integration |
| Old HACS integration | **disabled** (config entry `01KYCHVJEAPA7ZWE5WBG6NFPBE`) — leave it disabled |
| Fork / branch / PR | `arhee/eyeonwater` · `fix/dedicated-clientsession-waf` · PR #1 |

If water data stops appearing: **check the add-on is running** (Settings → Add-ons
→ EyeOnWater Fetcher → it should be *started*, and its Log shows `pushed N points`
every ~30 min). That's 90% of troubleshooting.

---

## 2. Why the normal integration does NOT work (root cause)

EyeOnWater sits behind an **Imperva/Incapsula WAF**. Requests from Home
Assistant's **core container** (Docker *bridge* network) are rejected at the
network layer: `POST /account/signin` is redirected to `/login`, no auth cookie
(`beacon-tkt`) is issued, and meter discovery returns **HTTP 400 / empty 200**.
Result: the integration authenticates but finds **0 meters** → no data.

The **same request works** from a *host-networked* context (plain `curl`, an
external machine, or a host-networked add-on), same account, same public IP.

**Ruled out** (don't re-investigate these — already tested): credentials, region,
`prefer_new_search`, cookies/quoting, aiohttp version, **TLS/JA3 fingerprint**
(tried 10 `curl_cffi` browser profiles — all failed from the core container),
egress IP, backend IP, IPv6, proxy env. The differentiator is purely the core
container's bridge-network path. **This cannot be fixed inside the integration.**
The `curl_cffi` approach was a dead end and was removed from the branch.

---

## 3. Architecture / data flow

```
[EyeOnWater cloud]
      ▲  (pyonwater, host network → WAF accepts)
      │
[EyeOnWater Fetcher add-on]  (host_network: true, runs on the HA Green)
      │  every ~30 min:
      ├── recorder/import_statistics ──► eyeonwater:water_meter_<meter-id>  (ft³, whole house)
      ├── compute irrigation = EOW − Moen Flo (gallons)
      ├── recorder/import_statistics ──► eyeonwater:irrigation_<meter-id>   (gal)
      ├── POST /api/states ─────────────► sensor.eyeonwater_irrigation_run_hours
      │                                   sensor.eyeonwater_irrigation_last_hour_gal
      │                                   sensor.eyeonwater_irrigation_today_gal
      │                                   sensor.eyeonwater_irrigation_yesterday_gal
      │                                   sensor.eyeonwater_data_last_reading  (timestamp of newest reading)
      ▼
[Home Assistant]
      ├── Energy Dashboard → Water  (uses eyeonwater:water_meter_...)
      ├── automation: Stuck irrigation valve suspected  → iPhone push
      └── automation: EyeOnWater data stale (watchdog)   → iPhone push
```

"Irrigation" = whole-property (EyeOnWater) minus main-house (Moen Flo), because
irrigation is the big draw that bypasses the Flo. Both normalized to gallons
(1 ft³ = 7.48052 gal).

---

## 4. Component inventory

### 4a. The add-on
- **Source:** `addon/eow_fetcher/` in this repo (`config.yaml`, `Dockerfile`,
  `README.md`, `eow_fetch_push.py`). `eow_fetch_push.py` is the entrypoint and
  the canonical copy of the fetch/push/irrigation logic.
- **On the HA box:** `/addons/eow_fetcher/`, slug `local_eow_fetcher`.
- **Key traits:** `host_network: true` (REQUIRED — this is the whole point),
  base image `python:3.13-slim` (HA's base has no `pip`), installs
  `pyonwater==0.3.32`.
- **Options** (Configuration tab): `username`, `password`, `ha_token`
  (HA long-lived token), `ha_url` (`http://localhost:8123` or
  `http://<HA-IP>:8123`), `hostname` (`eyeonwater.com`), `days` (3),
  `interval_minutes` (30), `flo_entity`
  (`sensor.flo_shutoff_today_s_water_usage`), `irrigation_active_gal` (10).

### 4b. Statistics (long-term, chartable, Energy Dashboard)
- `eyeonwater:water_meter_<meter-id>` — whole-house, **ft³**, cumulative.
- `eyeonwater:irrigation_<meter-id>` — irrigation, **gal**, cumulative.

### 4c. Live sensors (published via `/api/states`)
- `sensor.eyeonwater_irrigation_run_hours` — consecutive "active" hours ending at
  the latest data point. **This is the stuck-valve signal.**
- `sensor.eyeonwater_irrigation_last_hour_gal`
- `sensor.eyeonwater_irrigation_today_gal`
- `sensor.eyeonwater_irrigation_yesterday_gal`

> Note: `/api/states` sensors do NOT survive an HA restart until the add-on's
> next cycle republishes them (≤ interval). That's expected.

### 4d. Automations (created via the automation config API; editable in the UI)
- **`Stuck irrigation valve suspected`** (`automation.stuck_irrigation_valve_suspected`)
  - Trigger: `sensor.eyeonwater_irrigation_run_hours` **above 2** (i.e. ≥3
    consecutive active hours). Normal irrigation cycles max out at 2 hours.
  - Action: time-sensitive push to `notify.mobile_app_alexs_iphone_17_pro` +
    persistent notification.
- **`EyeOnWater data stale (watchdog)`** (`automation.eyeonwater_data_stale_watchdog`)
  - Trigger: true **data age** > **12h**, i.e. `now() - sensor.eyeonwater_data_last_reading`
    (the timestamp of the newest EyeOnWater reading the add-on publishes each
    cycle, v1.4.0+). Because it compares against `now()`, it fires for BOTH
    failure modes: EyeOnWater's backend stuck *and* the add-on dead (a frozen
    timestamp still ages past 12h).
  - Action: push + persistent notification, both reporting the **exact delay**
    (e.g. "No fresh EyeOnWater reading for 13h 24m").
  - Definition kept in-repo at
    `addon/eow_fetcher/automations/eyeonwater_data_stale_watchdog.yaml`.
  - NOTE: the old watchdog (≤v1.3.0) keyed off `sensor.eyeonwater_irrigation_run_hours`
    `last_updated`, which the add-on bumps every 30 min regardless of data
    freshness — so it only caught the add-on *dying*, never EyeOnWater lag. The
    12h/data-age version supersedes it.

### 4e. Not managed here (intentionally)
- **In-house leaks** are handled by the **Moen Flo app's own notifications** —
  we deliberately removed the HA Flo automations as redundant.
- Relevant Flo entities if you want them: `sensor.flo_shutoff_today_s_water_usage`
  (gal, main house), `sensor.flo_shutoff_water_flow_rate` (gal/min),
  `binary_sensor.flo_shutoff_pending_system_alerts`.

---

## 5. Operations

### Update the add-on to the latest fork version
On the HA box (SSH & Web Terminal add-on), pull files **SHA-pinned** (GitHub's
raw CDN caches branch URLs, so a bare branch URL can serve stale content):
```
cd /addons/eow_fetcher
BASE=https://raw.githubusercontent.com/arhee/eyeonwater/<commit-sha>/addon/eow_fetcher
for f in config.yaml Dockerfile README.md eow_fetch_push.py; do
  curl -fsSL "$BASE/$f" -o "$f" && echo "ok $f"
done
```
Then, in the app UI:
- If you **bumped the version** in `config.yaml`: use **Update** (NOT Rebuild —
  the Supervisor errors "use Update instead of Rebuild" when versions differ).
- If the version is unchanged: use **Rebuild**.
- CLI equivalents: `ha apps reload && ha apps update local_eow_fetcher` (or
  `ha apps rebuild local_eow_fetcher`), then `ha apps restart local_eow_fetcher`.

> On this HA version the add-on system is surfaced as **"Apps"** (CLI `ha apps …`),
> not "Add-ons". The Add-on/App Store lives under Settings; if the panel is
> missing, hard-refresh or use the CLI.

### Check health
- Add-on **Log** tab: should show `discovered 1 meter(s)`, `pushed N points…`,
  `irrigation: run_hours=… today=… gal` every ~30 min.
- If the data goes stale (EyeOnWater lag or a dead add-on), the **watchdog**
  automation pushes an alert after 12h, with the exact delay.

### Diagnostic scripts (run from a dev machine that CAN reach EyeOnWater)
In `scripts/` (need Python 3.13 + `pyonwater==0.3.32`; `check_ha_stats.py` also
needs `aiohttp`):
- `check_connection.py` — auth + fetch meters + read data directly from
  EyeOnWater. Isolates "is it the API/credentials?" from "is it HA?".
  Env: `EOW_USERNAME`, `EOW_PASSWORD`, optional `EOW_HOSTNAME`, `EOW_NEW_SEARCH`.
- `check_ha_stats.py` — inspects `eyeonwater:` statistics + entities in a live HA
  over REST + WebSocket. Env: `EOW_HA_TOKEN`, optional `EOW_HA_URL`.
- (The add-on's own `eow_fetch_push.py` can also be run standalone with env vars
  `EOW_USERNAME/EOW_PASSWORD/HA_URL/HA_TOKEN/EOW_DAYS` for a one-shot push.)

---

## 6. Tuning knobs

| Want to… | Change |
|---|---|
| Stuck-valve alert less/more sensitive | automation `Stuck irrigation valve suspected`, trigger `above: 2` → `3` (less) or `1` (more, = ≥2h) |
| What counts as an "active" irrigation hour | add-on option `irrigation_active_gal` (default 10 gal/hr) |
| How often data refreshes | add-on option `interval_minutes` (default 30) |
| Backfill long history | add-on option `days` → e.g. 365, run one cycle, set back to 3 |
| Watchdog window | automation `EyeOnWater data stale (watchdog)`, the `43200` seconds (=12h) in its trigger template |
| Where alerts go | replace `notify.mobile_app_alexs_iphone_17_pro` in both automations |

---

## 7. Known gotchas (things that already bit us)

- **HA base image has no `pip`** → Dockerfile pins `python:3.13-slim`. Don't
  switch it back to the HA base image.
- **"Use Update instead of Rebuild"** — appears whenever `config.yaml` version
  differs from the installed one. Use Update.
- **GitHub raw CDN caching** — pin `curl` URLs to a commit SHA, not the branch
  name, or you may fetch stale files.
- **`/api/states` POST always bumps `last_updated`** (even for an identical
  value); `last_changed` only moves when the value changes. The watchdog relies
  on this. Don't "fix" it.
- **EyeOnWater lag is ~1–3h**, hourly buckets — NOT a full day. So the
  stuck-valve alert is same-day (~1–3h delayed), not next-day.
- **Irrigation math is daily-robust, hourly-noisy.** Hour-to-hour it can jitter
  from meter/Flo timing; the run-hours + active-gal threshold absorbs that.
- **Add-on must be `host_network: true`.** Bridge network = the WAF block returns.

---

## 8. Where to pick up / possible next steps

- **Trend chart** (never finished): a built-in `statistics-graph` card comparing
  daily `sensor.flo_shutoff_today_s_water_usage` (main house) vs
  `eyeonwater:irrigation_<meter-id>` (irrigation), `period: day`,
  `chart_type: bar`, `stat_types: [change]`. Both are gallons and sum to ≈ total.
  For a matched-unit whole-house line too, have the add-on also publish a
  gallons total statistic.
- **Threshold tuning:** if a legit long-soak day false-alarms, bump the
  stuck-valve trigger to `above: 3`.
- **Upstream:** intentionally NOT contributing to `kdeyev/eyeonwater`. Stay on
  the `arhee` fork.

---

## 9. Quick reference (IDs & names)

```
HA:                 http://<HA-IP>:8123
Add-on slug:        local_eow_fetcher   (path /addons/eow_fetcher)
Meter:              id <meter-id>, uuid <meter-uuid>, unit cf
Disabled entry:     01KYCHVJEAPA7ZWE5WBG6NFPBE  (old HACS integration)

Statistics:         eyeonwater:water_meter_<meter-id>   (ft³)
                    eyeonwater:irrigation_<meter-id>    (gal)
Sensors:            sensor.eyeonwater_irrigation_run_hours
                    sensor.eyeonwater_irrigation_last_hour_gal
                    sensor.eyeonwater_irrigation_today_gal
                    sensor.eyeonwater_irrigation_yesterday_gal
Automations:        automation.stuck_irrigation_valve_suspected
                    automation.eyeonwater_data_stale_watchdog
Notify target:      notify.mobile_app_alexs_iphone_17_pro
Flo (main house):   sensor.flo_shutoff_today_s_water_usage  (gal)
```
