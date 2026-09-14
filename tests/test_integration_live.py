"""Opt-in test against the real iAqualink cloud. Changes the pump speed for a few seconds.

Run with:
  IAQUALINK_EMAIL=... IAQUALINK_PASSWORD=... POOL_LIVE_TEST=1 python3 -m pytest tests/test_integration_live.py -s
"""
import asyncio
import os

import httpx
import pytest

from app.aqualink_auth import AqualinkAuth
from app.aqualink_client import AqualinkClient

pytestmark = pytest.mark.skipif(os.environ.get("POOL_LIVE_TEST") != "1", reason="set POOL_LIVE_TEST=1 to run")


async def test_live_login_presets_and_round_trip():
    email, password = os.environ["IAQUALINK_EMAIL"], os.environ["IAQUALINK_PASSWORD"]
    async with httpx.AsyncClient(follow_redirects=True) as http:
        auth = AqualinkAuth(http, email, password)
        link = await auth.discover_touch_link(os.environ.get("IAQUALINK_SERIAL") or None)

        async def touch_link():
            return link

        client = AqualinkClient(auth, http, touch_link, on_change=lambda: None, refresh_interval=3600)
        await client.start()
        await asyncio.wait_for(client.wait_connected(), 30)
        await client.refresh_vsp()
        print("presets:", client.state.presets, "rpm:", client.state.rpm, "active:", client.state.active_preset)
        assert client.state.presets and client.state.rpm

        original = next(p for p in client.state.presets if p["label"] == client.state.active_preset)
        other = next(p for p in client.state.presets if p["index"] != original["index"])
        await client.set_preset(other["index"])
        assert client.state.active_preset == other["label"]
        await client.set_preset(original["index"])
        assert client.state.active_preset == original["label"]
        print("air temp:", client.state.air_temp, "pool temp:", client.state.pool_temp, "waterfall:", client.state.waterfall_on)
        await client.stop()
