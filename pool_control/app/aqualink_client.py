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
HOME_OTHER_DEVICES_INDEX = 7  # fallback only; the button is normally found by label
HOME_OTHER_DEVICES_LABEL = "Other Devices"
STREAM_SILENCE_TIMEOUT = 1800  # reconnect if the panel says nothing for 30 min
DEVICES_VSP_ADJ_LABEL = "VSP1 Spd"
HOME_WATERFALL_LABEL = "Water-fall"
CUSTOM_RPM_COMMAND = 128
MAX_RECONNECT_DELAY = 300
OFFLINE_RETRY_DELAY = 30
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
        refresh_interval: float = 900,
    ):
        self._auth = auth
        self._http = http
        self._touch_link_provider = touch_link_provider
        self._on_change = on_change
        self.refresh_interval = refresh_interval
        self.page_timeout = 15.0  # cloud round trips can be slow; a page arrives as many padded chunks
        self.reconnect_delay = 5.0
        self.start_delay = 1.0  # seconds between stream connect and the panel's start command
        self.offline_retry_delay = OFFLINE_RETRY_DELAY
        self.stream_silence_timeout = STREAM_SILENCE_TIMEOUT
        self._page_seq = 0  # bumps on every page message, so waits can demand a *fresh* page
        self._stream_response = None
        self._pump_task: asyncio.Task | None = None
        self._drop_requested = False

        self.state = AqualinkState()
        self.screen = ScreenModel()
        self._master_action = ""
        self._stb_action = ""
        self._start_action = ""
        self._offline = False
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
            if self._offline:
                self._offline = False
                await asyncio.sleep(self.offline_retry_delay)
                continue
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
        self._start_action = _action_id(data.get("actionIdMasterStart") or data.get("masterStart") or "")
        self.screen = ScreenModel()
        LOGGER.info("WebTouch session opened (system type %s)", data.get("systemTypeDisplay", data.get("systemType")))

    async def _read_stream(self) -> None:
        parser = StreamParser()
        # The panel pads every message to 4 KB so proxies flush it at once; a gzip-compressed
        # response would buffer those padded messages back together, so ask for identity.
        stream_headers = {"Accept-Encoding": "identity", "Cache-Control": "no-cache"}
        async with self._http.stream("GET", self._stream_url, headers=stream_headers,
                                     timeout=httpx.Timeout(None, connect=20)) as response:
            if response.status_code != 200:
                raise AqualinkAuthError(f"stream failed: HTTP {response.status_code}")
            LOGGER.info("WebTouch stream connected (content-type %s, content-encoding %s, transfer-encoding %s)",
                        response.headers.get("content-type"), response.headers.get("content-encoding", "none"),
                        response.headers.get("transfer-encoding", "none"))
            kick = asyncio.create_task(self._kick_start(), name="aqualink-kick-start")
            self._stream_response = response
            self._drop_requested = False
            self._pump_task = asyncio.create_task(self._pump(response, parser), name="aqualink-pump")
            try:
                await self._pump_task
            except asyncio.CancelledError:
                if not self._drop_requested:
                    raise  # we are being stopped
                raise AqualinkAuthError("stream dropped after the panel stopped answering")
            finally:
                self._pump_task = None
                self._stream_response = None
                kick.cancel()

    async def _pump(self, response, parser: StreamParser) -> None:
        seen = 0
        chunks = response.aiter_text().__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(chunks.__anext__(), self.stream_silence_timeout)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                raise AqualinkAuthError(f"stream silent for {self.stream_silence_timeout:.0f}s")
            if seen < 3:
                seen += 1
                LOGGER.debug("WebTouch stream chunk %d (%d chars): %r", seen, len(chunk), chunk[:400])
            self._handle_chunk(parser, chunk)

    async def _kick_start(self) -> None:
        """The panel stays silent until the session's start command is sent (the page sends
        '<masterStart>&command=1' shortly after loading). Send it once the stream is open."""
        await asyncio.sleep(self.start_delay)
        if not self._start_action:
            LOGGER.warning("No start action id in init response; stream may stay silent")
            return
        try:
            await self._send(NAV_HOME, action_id=self._start_action)
        except (AqualinkCommandError, AqualinkAuthError, httpx.HTTPError) as exc:
            LOGGER.warning("WebTouch start command failed: %s", exc)

    def _handle_chunk(self, parser: StreamParser, chunk: str) -> None:
        messages = parser.feed(chunk)
        if not messages:
            return
        for msg in messages:
            if isinstance(msg.code, str):
                if msg.code == "OFFLINE":
                    # the device reports OFFLINE to a new session while the previous one winds down;
                    # re-initialising immediately just prolongs it, so the reconnect loop pauses
                    LOGGER.warning("WebTouch reports the device OFFLINE; waiting %ss before a new session", self.offline_retry_delay)
                    self.state.error = "Device offline"
                    self._offline = True
                else:
                    LOGGER.debug("WebTouch marker %s %s", msg.code, msg.params)
                continue
            if msg.code == 23:
                self._page_seq += 1
                LOGGER.info("WebTouch page %s", msg.params[0] if msg.params else "?")
            elif msg.code == 24:
                LOGGER.debug("WebTouch button %s", msg.params)
            self.screen.apply(msg)
        if not any(isinstance(m.code, int) for m in messages):
            self._notify()
            return
        self._update_state_from_screen()
        if not self._connected.is_set():
            self.state.connected = True
            self.state.error = None
            self._connected.set()
        self._wake_waiters()
        self._notify()

    def _wake_waiters(self) -> None:
        # asyncio.Condition.notify_all() needs the lock; do it from a task so the stream loop stays sync.
        async def _notify_all():
            async with self._screen_changed:
                self._screen_changed.notify_all()
        asyncio.create_task(_notify_all(), name="aqualink-screen-notify")

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

    async def _wait_for_settle(self, quiet: float = 0.6, limit: float = 6.0) -> None:
        """Buttons of a page arrive one chunk at a time; wait until no new ones appear for `quiet` s."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            count = len(self.screen.buttons)
            await asyncio.sleep(quiet)
            if len(self.screen.buttons) == count:
                return

    async def _drop_stream(self, why: str) -> None:
        """The panel stopped answering: our picture of its screen is untrustworthy. Close the
        stream so the session loop reconnects, and refuse further commands until it does."""
        LOGGER.warning("WebTouch %s; dropping the stream to reconnect", why)
        self.state.connected = False
        self._connected.clear()
        self.state.error = "Panel stopped responding"
        self._drop_requested = True
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
        response = self._stream_response
        if response is not None:
            try:
                await response.aclose()
            except Exception:  # closing a dead stream can itself fail; the loop handles the rest
                pass
        self._notify()

    async def _send_and_wait_page(self, command: int, page: str, what: str,
                                  extra: Callable[[], bool] | None = None, action_id: str | None = None) -> None:
        """Send a command and require a *fresh* page message (one that arrives after the send)
        matching `page`. Screen buttons are addressed by position and the same position means
        different things on different pages, so a stale picture must never be acted on."""
        before = self._page_seq
        await self._send(command, action_id=action_id)
        try:
            await self._wait_for(
                lambda: self._page_seq > before and self.screen.page_id == page and bool(self.screen.buttons)
                and (extra() if extra else True),
                what,
            )
        except AqualinkCommandError:
            await self._drop_stream(f"did not confirm the {what}")
            raise
        await self._wait_for_settle()
        self._update_state_from_screen()

    async def _go_home(self) -> None:
        LOGGER.debug("WebTouch: going Home (current page %s)", self.screen.page_id)
        await self._send_and_wait_page(NAV_HOME, PAGE_HOME, "Home page")

    async def _goto_vsp(self) -> None:
        await self._go_home()
        other = self.screen.button_by_label(HOME_OTHER_DEVICES_LABEL)
        other_index = other.index if other is not None else HOME_OTHER_DEVICES_INDEX
        await self._send_and_wait_page(
            command_for_button(other_index), PAGE_DEVICES, "Devices page with VSP1 Spd button",
            extra=lambda: self.screen.button_by_label(DEVICES_VSP_ADJ_LABEL) is not None,
        )
        adj = self.screen.button_by_label(DEVICES_VSP_ADJ_LABEL)
        LOGGER.info("Devices page has %d buttons; VSP1 Spd is index %d (command %d)",
                    len(self.screen.buttons), adj.index, command_for_button(adj.index))
        await self._send_and_wait_page(command_for_button(adj.index), PAGE_VSP, "VSP page")

    def _require_connected(self) -> None:
        if not self.state.connected:
            raise AqualinkCommandError("iAqualink not connected")

    async def set_preset(self, index: int) -> None:
        async with self._lock:
            self._require_connected()
            await self._goto_vsp()
            await self._wait_for(lambda: index in self.screen.buttons, f"preset button {index}")
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
            await self._go_home()  # always refresh the Home page first; never trust a stale picture
            button = self.screen.button_by_label(HOME_WATERFALL_LABEL)
            if button is None:
                raise AqualinkCommandError("Waterfall button not found on Home page")
            if (button.state == 1) == on:
                return
            await self._send(command_for_button(button.index))
            await self._wait_for(lambda: (self.screen.button_by_label(HOME_WATERFALL_LABEL) or button).state == (1 if on else 0), "waterfall confirmation")
