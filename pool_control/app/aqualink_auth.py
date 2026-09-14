from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

LOGGER = logging.getLogger(__name__)

LOGIN_URL = "https://prod.zodiac-io.com/users/v1/login"
REFRESH_URL = "https://prod.zodiac-io.com/users/v1/refresh"
API_KEY = "EOOEMOW4YR6QNB07"
PORTAL_URL = "https://prm.iaqualink.net/v2"
REFRESH_MARGIN_SECONDS = 300


class AqualinkAuthError(Exception):
    pass


class AqualinkAuth:
    def __init__(self, http: httpx.AsyncClient, email: str, password: str, clock: Callable[[], float] = time.time):
        self._http = http
        self._email = email
        self._password = password
        self._clock = clock
        self.id_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0

    def invalidate(self) -> None:
        """Drop the cached tokens so the next ensure_token() logs in from scratch."""
        self.id_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0

    def _apply(self, body: dict) -> None:
        try:
            oauth = body["userPoolOAuth"]
            self.id_token = oauth["IdToken"]
            self.refresh_token = oauth.get("RefreshToken") or self.refresh_token
            self.expires_at = self._clock() + float(oauth.get("ExpiresIn", 3600))
        except (KeyError, TypeError) as exc:
            raise AqualinkAuthError(f"unexpected login response: {exc}") from exc

    async def login(self) -> None:
        payload = {"api_key": API_KEY, "email": self._email, "password": self._password}
        r = await self._http.post(LOGIN_URL, json=payload, timeout=15)
        if r.status_code != 200:
            raise AqualinkAuthError(f"login failed: HTTP {r.status_code}")
        self._apply(r.json())
        LOGGER.info("iAqualink login ok")

    async def _refresh(self) -> None:
        payload = {"email": self._email, "refresh_token": self.refresh_token}
        r = await self._http.post(REFRESH_URL, json=payload, timeout=15)
        if r.status_code != 200:
            raise AqualinkAuthError(f"refresh failed: HTTP {r.status_code}")
        self._apply(r.json())
        LOGGER.info("iAqualink token refreshed")

    async def ensure_token(self) -> str:
        if not self.id_token:
            await self.login()
        elif self.expires_at - self._clock() < REFRESH_MARGIN_SECONDS:
            try:
                await self._refresh()
            except (AqualinkAuthError, httpx.HTTPError) as exc:
                LOGGER.warning("Refresh failed (%s); logging in again", exc)
                await self.login()
        return self.id_token

    async def _portal_get(self, path: str) -> dict:
        token = await self.ensure_token()
        r = await self._http.get(f"{PORTAL_URL}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if r.status_code != 200:
            raise AqualinkAuthError(f"GET {path} failed: HTTP {r.status_code}")
        return r.json()

    async def discover_touch_link(self, serial: str | None) -> str:
        user = await self._portal_get("/userId")
        session_user_id = user.get("session_user_id")
        if not session_user_id:
            raise AqualinkAuthError("no session_user_id in /userId response")
        locations = (await self._portal_get(f"/users/{session_user_id}/locations")).get("locations", [])
        candidates = [d for d in locations if d.get("device_type") == "iaqua" and d.get("touchLink")]
        if serial:
            candidates = [d for d in candidates if d.get("serial_number") == serial]
        if not candidates:
            raise AqualinkAuthError("no iAqua device found on this account" + (f" with serial {serial}" if serial else ""))
        device = candidates[0]
        LOGGER.info("Using iAqualink device %s (%s)", device.get("Name"), device.get("serial_number"))
        return device["touchLink"]
