"""iAqualink WebTouch session: stream reader, page navigation, pump and waterfall commands."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

import httpx

from app.aqualink_auth import AqualinkAuth, AqualinkAuthError
from app.webtouch_parser import (
    PAGE_DEVICES, PAGE_HOME, PAGE_VSP, ScreenModel, StreamParser, command_for_button,
)

LOGGER = logging.getLogger(__name__)

WEBTOUCH_API = "https://prm.iaqualink.net/v2/webtouch"
NAV_HOME = 1
HOME_OTHER_DEVICES_INDEX = 7
DEVICES_VSP_ADJ_LABEL = "VSP1 Spd"
HOME_WATERFALL_LABEL = "Water-fall"
CUSTOM_RPM_COMMAND = 128
MAX_RECONNECT_DELAY = 300
HOME_INFO_POOL_TEMP = 0
HOME_INFO_AIR_TEMP = 1
HOME_INFO_SPA_TEMP = 3


class AqualinkCommandError(Exception):
    pass


@dataclass
class AqualinkState:
    connected: bool = False
    rpm: int | None = None
    active_preset: str | None = None
    presets: list[dict] = field(default_factory=list)
    waterfall_on: bool | None = None
    pool_temp: float | None = None
    air_temp: float | None = None
    spa_temp: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_number(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"-?\d+(\.\d+)?", text)
    return float(match.group()) if match else None


def _action_id(master: str) -> str:
    # Accepts a bare id ("NL_XYxCH3nqtqVa") or the page's "?actionID=NL_..." form.
    return master.split("actionID=", 1)[-1].split("&", 1)[0]


class AqualinkClient:
    def __init__(
        self,
        auth: AqualinkAuth,
        http: httpx.AsyncClient,
        touch_link_provider: Callable[[], Awaitable[str]],
        on_change: Callable[[], None],
        refresh_interval: float = 300,
    ):
        self._auth = auth
        self._http = http
        self._touch_link_provider = touch_link_provider
        self._on_change = on_change
        self.refresh_interval = refresh_interval
        self.page_timeout = 5.0
        self.reconnect_delay = 5.0

        self.state = AqualinkState()
        self.screen = ScreenModel()
        self._master_action = ""
        self._stb_action = ""
        self._stream_url = ""
        self._task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._screen_changed = asyncio.Condition()
        self._stopping = False

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="aqualink-stream")
        self._refresh_task = asyncio.create_task(self._periodic_refresh(), name="aqualink-refresh")

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._task, self._refresh_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def wait_connected(self) -> None:
        await self._connected.wait()

    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:  # never let a listener kill the client
            LOGGER.exception("on_change listener failed")

    # ---------- session / stream ----------

    async def _run(self) -> None:
        delay = self.reconnect_delay
        while not self._stopping:
            opened = False
            try:
                await self._open_session()
                opened = True
                await self._read_stream()
                LOGGER.warning("WebTouch stream ended; reconnecting")
            except asyncio.CancelledError:
                raise
            except (AqualinkAuthError, httpx.HTTPError, ValueError, KeyError) as exc:
                LOGGER.warning("WebTouch session error: %s", exc)
                self.state.error = str(exc)
            except Exception as exc:  # never let an unexpected error kill the session loop
                LOGGER.exception("WebTouch session failed unexpectedly")
                self.state.error = str(exc)
            self._connected.clear()
            self.state.connected = False
            self._notify()
            if opened:
                delay = self.reconnect_delay  # session opened: reset backoff (read live)
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _init_request(self, touch_link: str) -> httpx.Response:
        token = await self._auth.ensure_token()
        return await self._http.get(
            f"{WEBTOUCH_API}/init",
            params={"actionID": touch_link},
            headers={"Authorization": token},
            timeout=20,
        )

    async def _open_session(self) -> None:
        touch_link = await self._touch_link_provider()
        r = await self._init_request(touch_link)
        if r.status_code in (401, 403):
            LOGGER.warning("WebTouch init rejected the token (HTTP %s); logging in again", r.status_code)
            self._auth.invalidate()
            r = await self._init_request(touch_link)
        if r.status_code != 200:
            raise AqualinkAuthError(f"webtouch init failed: HTTP {r.status_code}")
        data = r.json()
        self._stream_url = data["serverConnection"]
        # Real init response keys (captured 2026-09-14): actionIdMasterId, actionIdMasterSTB,
        # actionIdMasterStart, actionIdMasteReset (sic). Values are bare action ids.
        self._master_action = _action_id(data.get("actionIdMasterId") or data["masterID"])
        self._stb_action = _action_id(data.get("actionIdMasterSTB") or data["masterSTB"])
        self.screen = ScreenModel()
        LOGGER.info("WebTouch session opened (system type %s)", data.get("systemTypeDisplay", data.get("systemType")))

    async def _read_stream(self) -> None:
        parser = StreamParser()
        async with self._http.stream("GET", self._stream_url, timeout=httpx.Timeout(None, connect=20)) as response:
            if response.status_code != 200:
                raise AqualinkAuthError(f"stream failed: HTTP {response.status_code}")
            async for chunk in response.aiter_text():
                messages = parser.feed(chunk)
                if not messages:
                    continue
                for msg in messages:
                    self.screen.apply(msg)
                self._update_state_from_screen()
                if not self._connected.is_set():
                    self.state.connected = True
                    self.state.error = None
                    self._connected.set()
                async with self._screen_changed:
                    self._screen_changed.notify_all()
                self._notify()

    def _update_state_from_screen(self) -> None:
        screen = self.screen
        if screen.page_id == PAGE_HOME:
            self.state.pool_temp = _parse_number(screen.info.get(HOME_INFO_POOL_TEMP))
            self.state.air_temp = _parse_number(screen.info.get(HOME_INFO_AIR_TEMP))
            self.state.spa_temp = _parse_number(screen.info.get(HOME_INFO_SPA_TEMP))
            waterfall = screen.button_by_label(HOME_WATERFALL_LABEL)
            if waterfall is not None:
                self.state.waterfall_on = waterfall.state == 1
        elif screen.page_id == PAGE_VSP:
            presets = []
            active = None
            for index in sorted(screen.buttons):
                button = screen.buttons[index]
                rpm = _parse_number(button.value)
                if rpm is None or not button.label:
                    continue
                presets.append({"index": index, "label": button.label, "rpm": int(rpm)})
                if button.state == 1:
                    active = button.label
            if presets:
                self.state.presets = presets
                self.state.active_preset = active
            rpm = _parse_number(screen.info.get(0))
            if rpm is not None:
                self.state.rpm = int(rpm)

    async def _refresh_vsp_quietly(self) -> None:
        try:
            await self.refresh_vsp()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("Periodic VSP refresh failed: %s", exc)

    async def _periodic_refresh(self) -> None:
        await self._connected.wait()
        await self._refresh_vsp_quietly()  # populate presets and RPM right after connecting
        while not self._stopping:
            await asyncio.sleep(self.refresh_interval)
            await self._refresh_vsp_quietly()

    # ---------- commands ----------

    async def _command_request(self, body: dict) -> httpx.Response:
        token = await self._auth.ensure_token()
        return await self._http.post(
            f"{WEBTOUCH_API}/command",
            json=body,
            headers={"Authorization": token, "Content-Type": "application/json"},
            timeout=15,
        )

    async def _send(self, command: int, action_id: str | None = None, text: str | None = None) -> None:
        body = {
            "actionID": action_id or self._master_action,
            "command": str(command),
            "dt": str(int(time.time() * 1000)),
        }
        if text is not None:
            body["text"] = text
        r = await self._command_request(body)
        if r.status_code in (401, 403):
            LOGGER.warning("WebTouch command rejected the token (HTTP %s); logging in again", r.status_code)
            self._auth.invalidate()
            r = await self._command_request(body)
        if r.status_code != 200:
            raise AqualinkCommandError(f"command {command} failed: HTTP {r.status_code}")

    async def _wait_for(self, predicate: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + self.page_timeout
        async with self._screen_changed:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._screen_changed.wait(), remaining)
                except asyncio.TimeoutError:
                    break
            # look once more: the reader may have applied the update as the clock ran out
            if not predicate():
                raise AqualinkCommandError(f"timed out waiting for {what}")

    async def _go_home(self) -> None:
        await self._send(NAV_HOME)
        await self._wait_for(lambda: self.screen.page_id == PAGE_HOME, "Home page")

    async def _goto_vsp(self) -> None:
        for attempt in (1, 2):
            try:
                await self._go_home()
                await self._send(command_for_button(HOME_OTHER_DEVICES_INDEX))
                await self._wait_for(lambda: self.screen.page_id == PAGE_DEVICES, "Devices page")
                adj = self.screen.button_by_label(DEVICES_VSP_ADJ_LABEL)
                if adj is None:
                    raise AqualinkCommandError("VSP1 Spd button not found on Devices page")
                await self._send(command_for_button(adj.index))
                await self._wait_for(lambda: self.screen.page_id == PAGE_VSP and bool(self.screen.buttons), "VSP page")
                return
            except AqualinkCommandError as exc:
                if attempt == 2:
                    raise
                LOGGER.warning("VSP navigation failed (%s); retrying from Home", exc)

    def _require_connected(self) -> None:
        if not self.state.connected:
            raise AqualinkCommandError("iAqualink not connected")

    async def set_preset(self, index: int) -> None:
        async with self._lock:
            self._require_connected()
            await self._goto_vsp()
            if index not in self.screen.buttons:
                raise AqualinkCommandError(f"no preset at index {index}")
            await self._send(command_for_button(index))
            await self._wait_for(lambda: self.screen.buttons.get(index) is not None and self.screen.buttons[index].state == 1, "preset confirmation")
            await self._go_home()

    async def set_custom_rpm(self, rpm: int) -> None:
        async with self._lock:
            self._require_connected()
            await self._goto_vsp()
            await self._send(CUSTOM_RPM_COMMAND, action_id=self._stb_action, text=str(int(rpm)))
            await self._wait_for(lambda: self.state.rpm == int(rpm), "custom RPM confirmation")
            await self._go_home()

    async def refresh_vsp(self) -> None:
        async with self._lock:
            self._require_connected()
            await self._goto_vsp()
            await self._go_home()

    async def set_waterfall(self, on: bool) -> None:
        async with self._lock:
            self._require_connected()
            if self.screen.page_id != PAGE_HOME:
                await self._go_home()
            button = self.screen.button_by_label(HOME_WATERFALL_LABEL)
            if button is None:
                raise AqualinkCommandError("Waterfall button not found on Home page")
            if (button.state == 1) == on:
                return
            await self._send(command_for_button(button.index))
            await self._wait_for(lambda: (self.screen.button_by_label(HOME_WATERFALL_LABEL) or button).state == (1 if on else 0), "waterfall confirmation")
