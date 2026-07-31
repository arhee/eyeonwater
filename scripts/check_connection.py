#!/usr/bin/env python3
"""Standalone EyeOnWater connectivity check.

Mirrors what the Home Assistant integration does at runtime, without needing
Home Assistant: authenticate, discover meters, read meter info, and pull a few
days of historical data. Useful for diagnosing "no data" problems.

Usage:
    export EOW_USERNAME="you@example.com"
    export EOW_PASSWORD="yourpassword"
    # optional: eyeonwater.com (default) or eyeonwater.ca
    export EOW_HOSTNAME="eyeonwater.com"
    # optional: extra days of history to try (default 3)
    export EOW_DAYS=3

    .venv-test/bin/python scripts/check_connection.py

Exit code is 0 on success, non-zero if any step fails.
"""

from __future__ import annotations

import asyncio
import os
import sys

import aiohttp
from pyonwater import (
    Account,
    Client,
    EyeOnWaterAPIError,
    EyeOnWaterAuthError,
)


def _fail(msg: str) -> None:
    print(f"\n❌ {msg}")
    sys.exit(1)


async def main() -> None:
    username = os.environ.get("EOW_USERNAME")
    password = os.environ.get("EOW_PASSWORD")
    hostname = os.environ.get("EOW_HOSTNAME", "eyeonwater.com")
    days = int(os.environ.get("EOW_DAYS", "3"))
    prefer_new_search = os.environ.get("EOW_NEW_SEARCH", "").lower() in {"1", "true", "yes"}

    if not username or not password:
        _fail("Set EOW_USERNAME and EOW_PASSWORD environment variables first.")

    print(f"Host:            {hostname}")
    print(f"Username:        {username}")
    print(f"Days to load:    {days}")
    print(f"prefer_new_search: {prefer_new_search}")

    account = Account(
        eow_hostname=hostname,
        username=username,
        password=password,
    )

    async with aiohttp.ClientSession() as session:
        client = Client(session, account)

        # 1. Authenticate
        print("\n[1/4] Authenticating ...")
        try:
            await client.authenticate()
        except EyeOnWaterAuthError as err:
            _fail(f"Authentication failed (bad username/password?): {err}")
        except (EyeOnWaterAPIError, aiohttp.ClientError, TimeoutError) as err:
            _fail(f"Could not connect to {hostname}: {err}")
        print("      ✅ Authenticated. Token valid:", client.is_token_valid)

        # 2. Discover meters
        print("\n[2/4] Fetching meters ...")
        try:
            meters = await account.fetch_meters(
                client,
                prefer_new_search=prefer_new_search,
            )
        except Exception as err:  # noqa: BLE001
            _fail(f"fetch_meters failed: {err!r}")
        if not meters:
            _fail(
                "Authenticated but NO meters were returned. "
                "Try EOW_NEW_SEARCH=1 (the 'prefer new search' option).",
            )
        print(f"      ✅ Found {len(meters)} meter(s):")
        for m in meters:
            print(f"         - id={m.meter_id} uuid={m.meter_uuid} "
                  f"unit={m.native_unit_of_measurement}")

        # 3 & 4. Read info + historical data for each meter
        for m in meters:
            print(f"\n[3/4] Reading meter info for {m.meter_id} ...")
            try:
                await m.read_meter_info(client=client)
                print(f"      ✅ Latest reading: {m.reading}")
            except Exception as err:  # noqa: BLE001
                print(f"      ⚠️  read_meter_info failed: {err!r}")

            print(f"\n[4/4] Reading {days} day(s) of history for {m.meter_id} ...")
            try:
                data = await m.read_historical_data(
                    client=client,
                    days_to_load=days,
                )
            except Exception as err:  # noqa: BLE001
                _fail(f"read_historical_data failed for {m.meter_id}: {err!r}")

            if not data:
                print("      ⚠️  No historical data points returned "
                      "(meter may not have reported recently).")
                continue
            print(f"      ✅ {len(data)} data point(s). "
                  f"First: {data[0].dt} = {data[0].reading} {data[0].unit}; "
                  f"Last: {data[-1].dt} = {data[-1].reading} {data[-1].unit}")

    print("\n\U0001f389 All checks passed — EyeOnWater is reachable and returning data.")


if __name__ == "__main__":
    asyncio.run(main())
