import asyncio
import json

import pytest
import websockets

from app.ha_client import HAClient, HAError


class FakeHA:
    def __init__(self):
        self.calls = []
        self.conn = None
        self.server = None
        self.url = ""

    async def start(self):
        self.server = await websockets.serve(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/api/websocket"

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, ws):
        self.conn = ws
        await ws.send(json.dumps({"type": "auth_required"}))
        msg = json.loads(await ws.recv())
        if msg.get("access_token") != "T":
            await ws.send(json.dumps({"type": "auth_invalid"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "get_states":
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": True, "result": [
                    {"entity_id": "switch.spa_heater", "state": "off"},
                    {"entity_id": "sensor.spa_temp", "state": "unknown"},
                    {"entity_id": "sensor.other", "state": "1"},
                ]}))
            elif msg["type"] == "subscribe_events":
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": True, "result": None}))
            elif msg["type"] == "call_service":
                self.calls.append(msg)
                ok = msg["service_data"]["entity_id"] != "switch.broken"
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": ok, "error": None if ok else {"message": "nope"}}))

    async def push_state(self, entity_id, state):
        await self.conn.send(json.dumps({"type": "event", "event": {"event_type": "state_changed", "data": {
            "entity_id": entity_id, "new_state": {"entity_id": entity_id, "state": state}}}}))


@pytest.fixture
async def ha():
    fake = FakeHA()
    await fake.start()
    yield fake
    await fake.stop()


async def test_connects_loads_states_and_tracks_changes(ha):
    changes = []
    client = HAClient(ha.url, "T", ["switch.spa_heater", "sensor.spa_temp"], on_change=lambda: changes.append(1))
    await client.start()
    await asyncio.wait_for(client.wait_connected(), 5)
    assert client.states == {"switch.spa_heater": "off", "sensor.spa_temp": "unknown"}
    assert client.is_on("switch.spa_heater") is False and client.number("sensor.spa_temp") is None
    await ha.push_state("sensor.spa_temp", "95.5")
    await ha.push_state("sensor.other", "2")
    await asyncio.sleep(0.2)
    assert client.number("sensor.spa_temp") == 95.5 and "sensor.other" not in client.states
    assert len(changes) >= 2
    await client.stop()


async def test_missing_entities_are_warned_about(ha, caplog):
    client = HAClient(ha.url, "T", ["switch.spa_heater", "switch.gone", "sensor.also_gone"], on_change=lambda: None)
    await client.start()
    await asyncio.wait_for(client.wait_connected(), 5)
    assert "switch.gone" in caplog.text and "sensor.also_gone" in caplog.text
    assert "switch.spa_heater" not in caplog.text
    await client.stop()


async def test_call_service_round_trip_and_error(ha):
    client = HAClient(ha.url, "T", ["switch.spa_heater"], on_change=lambda: None)
    await client.start()
    await asyncio.wait_for(client.wait_connected(), 5)
    await client.call_service("switch", "turn_on", "switch.spa_heater")
    assert ha.calls[-1]["domain"] == "switch" and ha.calls[-1]["service"] == "turn_on"
    assert ha.calls[-1]["service_data"] == {"entity_id": "switch.spa_heater"}
    await client.call_service("select", "select_option", "switch.spa_heater", option="40")
    assert ha.calls[-1]["service_data"] == {"entity_id": "switch.spa_heater", "option": "40"}
    with pytest.raises(HAError):
        await client.call_service("switch", "turn_on", "switch.broken")
    await client.stop()


async def test_call_service_when_disconnected_raises():
    client = HAClient("ws://127.0.0.1:1/api/websocket", "T", [], on_change=lambda: None)
    with pytest.raises(HAError):
        await client.call_service("switch", "turn_on", "switch.x")


async def test_bad_token_does_not_connect(ha):
    client = HAClient(ha.url, "WRONG", [], on_change=lambda: None)
    client.reconnect_delay = 0.05
    await client.start()
    await asyncio.sleep(0.3)
    assert client.connected is False
    await client.stop()


async def test_call_service_after_stop_raises_haerror(ha):
    client = HAClient(ha.url, "T", ["switch.spa_heater"], on_change=lambda: None)
    await client.start()
    await asyncio.wait_for(client.wait_connected(), 5)
    await client.stop()
    assert client.connected is False
    with pytest.raises(HAError):
        await client.call_service("switch", "turn_on", "switch.spa_heater")
