#!/usr/bin/env python3
"""Inspect the EyeOnWater data inside a live Home Assistant instance.

Answers "why doesn't HA see the eyeonwater metric?" by checking, over HA's
REST + WebSocket APIs:

  1. Does the live `sensor.water_meter_*` entity exist and what's its state?
  2. What `eyeonwater:` external-statistic IDs does the recorder know about?
  3. Do those statistics actually contain recent data points?

Requires a Home Assistant long-lived access token
(Profile -> Security -> Long-lived access tokens -> Create Token).

Usage:
    export EOW_HA_URL="http://homeassistant.local:8123"   # optional, this is default
    export EOW_HA_TOKEN="<long-lived-token>"
    .venv-test/bin/python scripts/check_ha_stats.py
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sys

import aiohttp


def _fail(msg: str) -> None:
    print(f"\n❌ {msg}")
    sys.exit(1)


async def _ws_call(ws: aiohttp.ClientWebSocketResponse, msg_id: int, payload: dict) -> dict:
    """Send one WS command and return its result message."""
    await ws.send_json({"id": msg_id, **payload})
    while True:
        msg = await ws.receive_json()
        if msg.get("id") == msg_id and msg.get("type") == "result":
            return msg


async def main() -> None:
    base = os.environ.get("EOW_HA_URL", "http://homeassistant.local:8123").rstrip("/")
    token = os.environ.get("EOW_HA_TOKEN")
    if not token:
        _fail("Set EOW_HA_TOKEN (Profile -> Security -> Long-lived access tokens).")

    headers = {"Authorization": f"Bearer {token}"}
    ws_url = base.replace("http", "ws", 1) + "/api/websocket"

    async with aiohttp.ClientSession() as session:
        # ---- 1. REST: live entities matching water_meter ----
        print(f"HA: {base}\n")
        print("[1/3] Live states (REST /api/states) matching 'water' ...")
        async with session.get(f"{base}/api/states", headers=headers) as resp:
            if resp.status == 401:
                _fail("401 Unauthorized — token is wrong or expired.")
            resp.raise_for_status()
            states = await resp.json()
        water = [s for s in states if "water" in s["entity_id"].lower()]
        if not water:
            print("      ⚠️  No entities with 'water' in the id. "
                  "Integration may not be loaded — check Settings -> Devices & Services.")
        for s in water:
            print(f"      - {s['entity_id']} = {s['state']} "
                  f"({s['attributes'].get('unit_of_measurement', '')})")

        # ---- WebSocket: authenticate ----
        async with session.ws_connect(ws_url, heartbeat=30) as ws:
            hello = await ws.receive_json()
            if hello.get("type") != "auth_required":
                _fail(f"Unexpected WS greeting: {hello}")
            await ws.send_json({"type": "auth", "access_token": token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                _fail(f"WS auth failed: {auth}")

            # ---- 2. Statistic IDs the recorder knows ----
            print("\n[2/3] Recorder statistic IDs (WS recorder/list_statistic_ids) ...")
            res = await _ws_call(ws, 10, {"type": "recorder/list_statistic_ids"})
            all_ids = res.get("result", [])
            eow = [x for x in all_ids if x["statistic_id"].startswith("eyeonwater:")]
            if not eow:
                print("      ❌ NO 'eyeonwater:' statistics exist in the recorder.")
                print("         => Statistics have never been written. Likely causes:")
                print("            - integration not set up / failed to load")
                print("            - no data imported yet (call eyeonwater.import_historical_data)")
                print("            - recorder purge/config issue")
                print(f"      (recorder currently tracks {len(all_ids)} statistic IDs total.)")
            else:
                print(f"      ✅ Found {len(eow)} eyeonwater statistic(s):")
                for x in eow:
                    print(f"         - {x['statistic_id']}  unit={x.get('statistics_unit_of_measurement')}  "
                          f"source={x.get('source')}  has_sum={x.get('has_sum')}")

            # ---- 3. Recent data points in each eyeonwater statistic ----
            print("\n[3/3] Recent data in eyeonwater statistics (WS statistics_during_period) ...")
            if not eow:
                print("      (skipped — no statistics to query)")
            else:
                start = (datetime.datetime.now(datetime.UTC)
                         - datetime.timedelta(days=7)).isoformat()
                for x in eow:
                    sid = x["statistic_id"]
                    res = await _ws_call(ws, 20, {
                        "type": "recorder/statistics_during_period",
                        "start_time": start,
                        "statistic_ids": [sid],
                        "period": "hour",
                    })
                    points = res.get("result", {}).get(sid, [])
                    if not points:
                        print(f"      ⚠️  {sid}: 0 points in the last 7 days.")
                        continue
                    first, last = points[0], points[-1]
                    def _t(ms):  # noqa: ANN001, ANN202
                        return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC)
                    print(f"      ✅ {sid}: {len(points)} points, "
                          f"{_t(first['start'])} -> {_t(last['start'])}, "
                          f"last sum={last.get('sum')} state={last.get('state')}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
