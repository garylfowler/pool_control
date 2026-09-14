import asyncio
import json

import httpx
import pytest

from app.aqualink_auth import AqualinkAuth
from app.aqualink_client import AqualinkClient, AqualinkCommandError

INIT_BODY = {
    "systemType": 0,
    "serverConnection": "https://webtouch.iaqualink.net/5E/STREAM",
    "masterID": "?actionID=NL_MASTER",
    "masterStart": "?actionID=NL_START",
    "masterSTB": "?actionID=NL_STB",
    "masterReset": "?actionID=NL_RESET",
}


def nl(code, params):
    return f"<script type='text/javascript'>parent.printNL({code}, \"{params}\");</script>"


HOME = nl(23, "1") + nl(24, "0||1||8||Filter||Pump") + nl(24, "4||0||3||Water-||fall") + nl(24, "7||0||0||Other||Devices") + nl(25, "0||82º") + nl(25, "1||63º")
DEVICES = nl(23, "54") + nl(24, "2||0||0||VSP1 Spd||ADJ") + nl(24, "6||0||0||Waterfall||OFF")
VSP = nl(23, "30") + nl(24, "0||1||0||Pool||2950") + nl(24, "6||0||0||Cloudy||2800") + nl(25, "0||2950")


class FakeCloud:
    """Simulates prm.iaqualink.net + the webtouch stream. Commands push new screen chunks."""

    def __init__(self):
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.commands: list[dict] = []
        self.init_calls = 0
        self.page = "1"

    async def handler(self, request: httpx.Request):
        path = request.url.path
        if path.endswith("/users/v1/login"):
            return httpx.Response(200, json={"userPoolOAuth": {"IdToken": "tok", "RefreshToken": "r", "ExpiresIn": 3600}})
        if path.endswith("/webtouch/init"):
            self.init_calls += 1
            assert request.headers["Authorization"] == "tok"
            assert request.url.params["actionID"] == "LINK"
            await self.queue.put(HOME)
            return httpx.Response(200, json=INIT_BODY, headers={"set-cookie": "wt=1; Path=/"})
        if path == "/5E/STREAM":
            return httpx.Response(200, stream=QueueStream(self.queue))
        if path.endswith("/webtouch/command"):
            body = json.loads(request.content)
            self.commands.append(body)
            self.react(body)
            return httpx.Response(200, content=b"")
        return httpx.Response(404)

    def react(self, body):
        cmd = int(body["command"])
        if cmd == 1:
            self.page = "1"; self.queue.put_nowait(HOME)
        elif self.page == "1" and cmd == 24:
            self.page = "54"; self.queue.put_nowait(DEVICES)
        elif self.page == "1" and cmd == 21:
            self.queue.put_nowait(nl(24, "4||1||3||Water-||fall"))
        elif self.page == "54" and cmd == 19:
            self.page = "30"; self.queue.put_nowait(VSP)
        elif self.page == "30" and cmd == 23:
            self.queue.put_nowait(nl(24, "0||0||0||Pool||2950") + nl(24, "6||1||0||Cloudy||2800") + nl(25, "0||2800"))
        elif self.page == "30" and cmd == 128:
            self.queue.put_nowait(nl(25, "0||" + body["text"]))


class QueueStream(httpx.AsyncByteStream):
    def __init__(self, queue):
        self.queue = queue

    async def __aiter__(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item.encode()


@pytest.fixture
async def client_and_cloud():
    cloud = FakeCloud()
    http = httpx.AsyncClient(transport=httpx.MockTransport(cloud.handler))
    auth = AqualinkAuth(http, "e", "p")
    changes = []

    async def touch_link():
        return "LINK"

    client = AqualinkClient(auth, http, touch_link, on_change=lambda: changes.append(1), refresh_interval=3600)
    await client.start()
    await asyncio.wait_for(client.wait_connected(), 5)
    yield client, cloud, changes
    await client.stop()
    await http.aclose()


async def test_connects_and_reads_home_page(client_and_cloud):
    client, cloud, changes = client_and_cloud
    s = client.state
    assert s.connected and s.pool_temp == 82 and s.air_temp == 63 and s.waterfall_on is False
    assert cloud.init_calls == 1 and changes


async def test_set_preset_navigates_and_confirms(client_and_cloud):
    client, cloud, _ = client_and_cloud
    await client.set_preset(6)
    assert [int(c["command"]) for c in cloud.commands] == [1, 24, 19, 23, 1]
    assert all(c["actionID"] == "NL_MASTER" for c in cloud.commands)
    assert client.state.rpm == 2800 and client.state.active_preset == "Cloudy"
    assert [p["label"] for p in client.state.presets] == ["Pool", "Cloudy"]
    assert client.state.presets[0]["rpm"] == 2950


async def test_set_custom_rpm_uses_stb_action(client_and_cloud):
    client, cloud, _ = client_and_cloud
    await client.set_custom_rpm(1500)
    last = cloud.commands[-2]
    assert last == {"actionID": "NL_STB", "command": "128", "text": "1500", "dt": last["dt"]}
    assert client.state.rpm == 1500


async def test_set_waterfall_from_home(client_and_cloud):
    client, cloud, _ = client_and_cloud
    await client.set_waterfall(True)
    assert int(cloud.commands[-1]["command"]) == 21
    assert client.state.waterfall_on is True


async def test_set_waterfall_noop_when_already_in_state(client_and_cloud):
    client, cloud, _ = client_and_cloud
    await client.set_waterfall(False)
    assert cloud.commands == []


async def test_command_fails_when_page_never_arrives(client_and_cloud):
    client, cloud, _ = client_and_cloud
    cloud.react = lambda body: None  # cloud stops answering
    client.page_timeout = 0.2
    with pytest.raises(AqualinkCommandError):
        await client.set_preset(0)


async def test_stream_end_reconnects(client_and_cloud):
    client, cloud, _ = client_and_cloud
    client.reconnect_delay = 0.05
    await cloud.queue.put(None)  # server closes stream
    await asyncio.sleep(0.5)
    assert cloud.init_calls == 2 and client.state.connected
