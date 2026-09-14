"""Minimal Home Assistant websocket client: state cache + service calls."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import websockets

LOGGER = logging.getLogger(__name__)
MAX_RECONNECT_DELAY = 300


class HAError(Exception):
    pass


class HAClient:
    def __init__(self, ws_url: str, token: str, entity_ids: list[str], on_change: Callable[[], None]):
        self._ws_url = ws_url
        self._token = token
        self._entity_ids = set(entity_ids)
        self._on_change = on_change
        self.reconnect_delay = 5.0
        self.states: dict[str, str] = {}
        self.connected = False
        self._ws = None
        self._task: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._connected_event = asyncio.Event()
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="ha-websocket")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            await self._ws.close()

    async def wait_connected(self) -> None:
        await self._connected_event.wait()

    def is_on(self, entity_id: str) -> bool:
        return self.states.get(entity_id) == "on"

    def number(self, entity_id: str) -> float | None:
        try:
            return float(self.states[entity_id])
        except (KeyError, ValueError, TypeError):
            return None

    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:
            LOGGER.exception("on_change listener failed")

    async def _run(self) -> None:
        delay = self.reconnect_delay
        while not self._stopping:
            try:
                async with websockets.connect(self._ws_url, max_size=16 * 1024 * 1024) as ws:
                    self._ws = ws
                    await self._authenticate(ws)
                    await self._load_states()
                    await self._request({"type": "subscribe_events", "event_type": "state_changed"})
                    delay = self.reconnect_delay  # connected: reset backoff
                    self.connected = True
                    self._connected_event.set()
                    self._notify()
                    LOGGER.info("Home Assistant websocket connected")
                    await self._listen(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("Home Assistant websocket error: %s", exc)
            self.connected = False
            self._connected_event.clear()
            self._ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(HAError("disconnected"))
            self._pending.clear()
            self._notify()
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _authenticate(self, ws) -> None:
        first = json.loads(await ws.recv())
        if first.get("type") != "auth_required":
            raise HAError(f"unexpected first message: {first}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "auth_ok":
            raise HAError(f"authentication failed: {reply}")

    async def _listen(self, ws) -> None:
        # the first messages (get_states / subscribe results) are handled via _request futures,
        # so this loop must run concurrently with them: start it before awaiting requests.
        async for raw in ws:
            msg = json.loads(raw)
            if "id" in msg and msg.get("type") == "result":
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif msg.get("type") == "event":
                data = msg["event"].get("data", {})
                entity_id = data.get("entity_id")
                new_state = data.get("new_state") or {}
                if entity_id in self._entity_ids:
                    self.states[entity_id] = new_state.get("state", "unavailable")
                    self._notify()

    async def _request(self, payload: dict) -> dict:
        if self._ws is None:
            raise HAError("not connected")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps({"id": msg_id, **payload}))
        # pump messages until our reply arrives (the listen loop may not be running yet)
        while not fut.done():
            raw = await self._ws.recv()
            msg = json.loads(raw)
            if msg.get("type") == "result" and msg.get("id") in self._pending:
                pending = self._pending.pop(msg["id"])
                if not pending.done():
                    pending.set_result(msg)
            elif msg.get("type") == "event":
                data = msg["event"].get("data", {})
                if data.get("entity_id") in self._entity_ids:
                    self.states[data["entity_id"]] = (data.get("new_state") or {}).get("state", "unavailable")
                    self._notify()
        result = fut.result()
        if not result.get("success", False):
            raise HAError((result.get("error") or {}).get("message", "request failed"))
        return result

    async def _load_states(self) -> None:
        result = await self._request({"type": "get_states"})
        self.states = {s["entity_id"]: s["state"] for s in result["result"] if s["entity_id"] in self._entity_ids}

    async def call_service(self, domain: str, service: str, entity_id: str) -> None:
        if not self.connected or self._ws is None:
            raise HAError("Home Assistant not connected")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps({
            "id": msg_id, "type": "call_service", "domain": domain, "service": service,
            "service_data": {"entity_id": entity_id},
        }))
        try:
            result = await asyncio.wait_for(fut, 15)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise HAError("service call timed out") from exc
        if not result.get("success", False):
            raise HAError((result.get("error") or {}).get("message", "service call failed"))
