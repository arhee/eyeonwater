#!/usr/bin/env python3
"""Fetch EyeOnWater meter data and push it into Home Assistant statistics.

This is the out-of-band fetcher: it runs somewhere that can actually reach
EyeOnWater (e.g. a host-networked HAOS add-on on the HA Green), pulls each
meter's hourly readings with pyonwater, and writes them into Home Assistant's
external statistics via the `recorder/import_statistics` WebSocket command —
using the same `eyeonwater:water_meter_<id>` statistic IDs the Energy
Dashboard expects, so nothing downstream changes.

import_statistics upserts by `start`, so re-pushing the last few days each run
is safe and idempotent.

Env:
  EOW_USERNAME, EOW_PASSWORD          (required)
  EOW_HOSTNAME   default eyeonwater.com
  EOW_DAYS       default 3            (days of history to (re)push)
  HA_URL         default http://supervisor/core   (add-on -> core proxy)
  HA_TOKEN       required             (long-lived token, or SUPERVISOR_TOKEN)
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import pathlib
import sys

import aiohttp
from pyonwater import Account, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_LOGGER = logging.getLogger("eow_fetch_push")

# pyonwater native unit value -> HA unit string
UNIT_MAP = {"gal": "gal", "cf": "ft³", "cm": "m³"}

# native unit value -> gallons, for the irrigation computation (Flo is in gal)
CF_TO_GAL = 7.48052
NATIVE_TO_GAL = {"gal": 1.0, "cf": CF_TO_GAL, "cm": 264.172}

# Irrigation defaults (tunable via add-on options / env)
DEFAULT_FLO_ENTITY = "sensor.flo_shutoff_today_s_water_usage"
DEFAULT_ACTIVE_GAL = 10.0  # irrigation gal/hr above which an hour counts "active"

# HAOS writes add-on options here; env vars take precedence for local testing.
_OPTIONS_FILE = pathlib.Path("/data/options.json")


def _cfg(key: str, env: str, default: str | None = None) -> str | None:
    """Read config from env var first, then /data/options.json, then default."""
    if os.environ.get(env) is not None:
        return os.environ[env]
    if _OPTIONS_FILE.is_file():
        opts = json.loads(_OPTIONS_FILE.read_text())
        if opts.get(key) not in (None, ""):
            return str(opts[key])
    return default


def _normalize_id(meter_id: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in meter_id).lower()


def _statistic_id(meter_id: str) -> str:
    return f"eyeonwater:water_meter_{_normalize_id(meter_id)}"


def _ha_unit(meter) -> str:  # noqa: ANN001
    native = getattr(
        meter.native_unit_of_measurement,
        "value",
        str(meter.native_unit_of_measurement),
    )
    return UNIT_MAP.get(native, native)


async def _ws_import(
    session: aiohttp.ClientSession,
    ha_url: str,
    token: str,
    metadata: dict,
    stats: list[dict],
) -> None:
    """Send one recorder/import_statistics command over the HA WebSocket API."""
    ws_url = ha_url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    async with session.ws_connect(ws_url, heartbeat=30) as ws:
        hello = await ws.receive_json()
        if hello.get("type") != "auth_required":
            msg = f"unexpected WS greeting: {hello}"
            raise RuntimeError(msg)
        await ws.send_json({"type": "auth", "access_token": token})
        auth = await ws.receive_json()
        if auth.get("type") != "auth_ok":
            msg = f"WS auth failed: {auth}"
            raise RuntimeError(msg)
        await ws.send_json(
            {
                "id": 1,
                "type": "recorder/import_statistics",
                "metadata": metadata,
                "stats": stats,
            },
        )
        while True:
            msg = await ws.receive_json()
            if msg.get("id") == 1 and msg.get("type") == "result":
                if not msg.get("success"):
                    err = f"import_statistics failed: {msg.get('error')}"
                    raise RuntimeError(err)
                return


def _native_to_gal(unit_value: str) -> float:
    return NATIVE_TO_GAL.get(unit_value, 1.0)


def _eow_hourly_gallons(meter, data) -> list[tuple]:  # noqa: ANN001
    """Return [(utc_hour, local_date, gallons)] of per-hour EOW consumption."""
    native = getattr(
        meter.native_unit_of_measurement,
        "value",
        str(meter.native_unit_of_measurement),
    )
    factor = _native_to_gal(native)
    out: list[tuple] = []
    ordered = sorted(data, key=lambda d: d.dt)
    prev = None
    for d in ordered:
        if prev is not None:
            gal = (d.reading - prev.reading) * factor
            utc_hour = d.dt.astimezone(datetime.timezone.utc).replace(
                minute=0, second=0, microsecond=0,
            )
            out.append((utc_hour, d.dt.date(), round(gal, 2)))
        prev = d
    return out


async def _fetch_flo_hourly_gal(
    session: aiohttp.ClientSession,
    ha_url: str,
    token: str,
    entity: str,
    start_iso: str,
) -> dict:
    """Return {utc_hour_datetime: gallons} of Flo hourly consumption from HA."""
    ws_url = ha_url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    result: dict = {}
    async with session.ws_connect(ws_url, heartbeat=30) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": token})
        if (await ws.receive_json()).get("type") != "auth_ok":
            return result
        await ws.send_json(
            {
                "id": 1,
                "type": "recorder/statistics_during_period",
                "start_time": start_iso,
                "statistic_ids": [entity],
                "period": "hour",
            },
        )
        while True:
            msg = await ws.receive_json()
            if msg.get("id") == 1 and msg.get("type") == "result":
                break
        for row in msg.get("result", {}).get(entity, []):
            change = row.get("change")
            if change is None:
                continue
            hour = datetime.datetime.fromtimestamp(
                row["start"] / 1000, datetime.timezone.utc,
            ).replace(minute=0, second=0, microsecond=0)
            result[hour] = float(change)
    return result


async def _post_state(
    session: aiohttp.ClientSession,
    ha_url: str,
    token: str,
    entity_id: str,
    state: str,
    attributes: dict,
) -> None:
    """Set a sensor state via the HA REST API."""
    async with session.post(
        f"{ha_url.rstrip('/')}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"state": state, "attributes": attributes},
    ) as resp:
        if resp.status not in (200, 201):
            _LOGGER.warning(
                "failed to set %s: HTTP %s %s",
                entity_id,
                resp.status,
                (await resp.text())[:120],
            )


async def publish_irrigation(
    session: aiohttp.ClientSession,
    ha_url: str,
    token: str,
    meter,  # noqa: ANN001
    data,  # noqa: ANN001
    flo_entity: str,
    active_gal: float,
) -> None:
    """Compute irrigation = EOW - Flo and publish live HA sensors."""
    eow_hourly = _eow_hourly_gallons(meter, data)
    if not eow_hourly:
        return
    start_iso = (
        min(h for h, _, _ in eow_hourly) - datetime.timedelta(hours=1)
    ).isoformat()
    flo_hourly = await _fetch_flo_hourly_gal(
        session, ha_url, token, flo_entity, start_iso,
    )

    # per-hour irrigation, chronological
    rows: list[tuple] = []  # (utc_hour, local_date, irrigation_gal)
    for utc_hour, local_date, eow_gal in eow_hourly:
        irr = max(0.0, eow_gal - flo_hourly.get(utc_hour, 0.0))
        rows.append((utc_hour, local_date, round(irr, 1)))

    last_hour_gal = rows[-1][2]
    # consecutive active hours ending at the most recent hour
    run_hours = 0
    for _, _, irr in reversed(rows):
        if irr > active_gal:
            run_hours += 1
        else:
            break

    # daily totals (local date)
    daily: dict = {}
    for _, local_date, irr in rows:
        daily[local_date] = daily.get(local_date, 0.0) + irr
    today = max(daily) if daily else None
    dates = sorted(daily)
    today_gal = round(daily.get(today, 0.0), 1) if today else 0.0
    yesterday_gal = round(daily[dates[-2]], 1) if len(dates) >= 2 else 0.0

    base = _normalize_id(meter.meter_id)
    await _post_state(
        session, ha_url, token, "sensor.eyeonwater_irrigation_run_hours",
        str(run_hours),
        {"unit_of_measurement": "h", "icon": "mdi:sprinkler-variant",
         "friendly_name": "Irrigation consecutive run hours",
         "state_class": "measurement", "meter": base,
         "active_gal_threshold": active_gal},
    )
    await _post_state(
        session, ha_url, token, "sensor.eyeonwater_irrigation_last_hour_gal",
        str(last_hour_gal),
        {"unit_of_measurement": "gal", "icon": "mdi:sprinkler",
         "friendly_name": "Irrigation last hour", "state_class": "measurement"},
    )
    await _post_state(
        session, ha_url, token, "sensor.eyeonwater_irrigation_today_gal",
        str(today_gal),
        {"unit_of_measurement": "gal", "icon": "mdi:sprinkler",
         "friendly_name": "Irrigation today", "device_class": "water",
         "state_class": "total_increasing"},
    )
    await _post_state(
        session, ha_url, token, "sensor.eyeonwater_irrigation_yesterday_gal",
        str(yesterday_gal),
        {"unit_of_measurement": "gal", "icon": "mdi:sprinkler",
         "friendly_name": "Irrigation yesterday", "state_class": "measurement"},
    )
    _LOGGER.info(
        "irrigation: run_hours=%d last_hour=%.1f today=%.1f yesterday=%.1f gal",
        run_hours, last_hour_gal, today_gal, yesterday_gal,
    )


async def run_once(
    *,
    username: str,
    password: str,
    hostname: str,
    days: int,
    ha_url: str,
    token: str,
    flo_entity: str = DEFAULT_FLO_ENTITY,
    active_gal: float = DEFAULT_ACTIVE_GAL,
) -> None:
    """Fetch every meter and push its readings into HA once."""
    account = Account(eow_hostname=hostname, username=username, password=password)
    async with aiohttp.ClientSession() as session:
        client = Client(session, account)
        await client.authenticate()
        meters = await account.fetch_meters(client)
        _LOGGER.info("discovered %d meter(s)", len(meters))

        for meter in meters:
            await meter.read_meter_info(client=client)
            data = await meter.read_historical_data(client=client, days_to_load=days)
            if not data:
                _LOGGER.warning("no data for meter %s", meter.meter_id)
                continue

            unit = _ha_unit(meter)
            metadata = {
                "has_mean": False,
                "has_sum": True,
                "name": f"Water Meter {_normalize_id(meter.meter_id)}",
                "source": "eyeonwater",
                "statistic_id": _statistic_id(meter.meter_id),
                "unit_of_measurement": unit,
            }
            stats = [
                {
                    "start": row.dt.isoformat(),
                    "sum": float(row.reading),
                    "state": float(row.reading),
                }
                for row in data
            ]
            await _ws_import(session, ha_url, token, metadata, stats)
            _LOGGER.info(
                "pushed %d points to %s (unit=%s, last=%s %s)",
                len(stats),
                metadata["statistic_id"],
                unit,
                data[-1].dt,
                data[-1].reading,
            )

            try:
                await publish_irrigation(
                    session, ha_url, token, meter, data, flo_entity, active_gal,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("irrigation computation failed (non-fatal)")


async def main() -> None:
    username = _cfg("username", "EOW_USERNAME")
    password = _cfg("password", "EOW_PASSWORD")
    hostname = _cfg("hostname", "EOW_HOSTNAME", "eyeonwater.com")
    days = int(_cfg("days", "EOW_DAYS", "3"))
    ha_url = _cfg("ha_url", "HA_URL", "http://localhost:8123")
    token = _cfg("ha_token", "HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
    interval = int(_cfg("interval_minutes", "EOW_INTERVAL_MINUTES", "0"))
    flo_entity = _cfg("flo_entity", "FLO_ENTITY", DEFAULT_FLO_ENTITY)
    active_gal = float(_cfg("irrigation_active_gal", "IRRIGATION_ACTIVE_GAL",
                            str(DEFAULT_ACTIVE_GAL)))

    if not username or not password:
        _LOGGER.error("username and password are required")
        sys.exit(1)
    if not token:
        _LOGGER.error("ha_token (or SUPERVISOR_TOKEN) is required")
        sys.exit(1)

    kwargs = {
        "username": username,
        "password": password,
        "hostname": hostname,
        "days": days,
        "ha_url": ha_url,
        "token": token,
        "flo_entity": flo_entity,
        "active_gal": active_gal,
    }

    while True:
        try:
            await run_once(**kwargs)
            _LOGGER.info("cycle complete")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("cycle failed; will retry next interval")
        if interval <= 0:
            break
        await asyncio.sleep(interval * 60)


if __name__ == "__main__":
    asyncio.run(main())
