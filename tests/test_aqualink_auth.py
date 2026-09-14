import json

import httpx
import pytest

from app.aqualink_auth import API_KEY, AqualinkAuth, AqualinkAuthError

LOGIN_BODY = {"userPoolOAuth": {"IdToken": "tok1", "RefreshToken": "ref1", "ExpiresIn": 3600}}


def make_auth(handler, now=1000.0):
    clock = {"now": now}
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    auth = AqualinkAuth(http, "me@example.com", "pw", clock=lambda: clock["now"])
    return auth, clock


async def test_login_posts_api_key_and_stores_token():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json=LOGIN_BODY)

    auth, _ = make_auth(handler)
    await auth.login()
    assert seen["url"] == "https://prod.zodiac-io.com/users/v1/login"
    assert seen["json"] == {"api_key": API_KEY, "email": "me@example.com", "password": "pw"}
    assert auth.id_token == "tok1" and auth.refresh_token == "ref1"
    assert auth.expires_at == 1000.0 + 3600


async def test_login_failure_raises():
    def handler(request):
        return httpx.Response(401, json={"message": "bad"})

    auth, _ = make_auth(handler)
    with pytest.raises(AqualinkAuthError):
        await auth.login()


async def test_ensure_token_refreshes_when_expiring():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json=LOGIN_BODY)
        assert json.loads(request.content) == {"email": "me@example.com", "refresh_token": "ref1"}
        return httpx.Response(200, json={"userPoolOAuth": {"IdToken": "tok2", "RefreshToken": "ref2", "ExpiresIn": 3600}})

    auth, clock = make_auth(handler)
    assert await auth.ensure_token() == "tok1"
    clock["now"] = 1000.0 + 3600 - 200  # 200 s left -> refresh
    assert await auth.ensure_token() == "tok2"
    assert calls == ["/users/v1/login", "/users/v1/refresh"]


async def test_ensure_token_falls_back_to_login_when_refresh_fails():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/refresh"):
            return httpx.Response(400, json={})
        return httpx.Response(200, json=LOGIN_BODY)

    auth, clock = make_auth(handler)
    await auth.ensure_token()
    clock["now"] = 1000.0 + 3600
    await auth.ensure_token()
    assert calls == ["/users/v1/login", "/users/v1/refresh", "/users/v1/login"]


async def test_discover_touch_link_uses_bearer_and_picks_iaqua_device():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("Authorization")))
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json=LOGIN_BODY)
        if request.url.path == "/v2/userId":
            return httpx.Response(200, json={"session_user_id": "SESS1"})
        if request.url.path == "/v2/users/SESS1/locations":
            return httpx.Response(200, json={"locations": [
                {"Name": "Robot", "device_type": "vr", "serial_number": "R1", "touchLink": "no"},
                {"Name": "Fowler Pool", "device_type": "iaqua", "serial_number": "QK1", "touchLink": "LinkA"},
                {"Name": "Other", "device_type": "iaqua", "serial_number": "QK2", "touchLink": "LinkB"},
            ]})
        return httpx.Response(404)

    auth, _ = make_auth(handler)
    assert await auth.discover_touch_link(None) == "LinkA"
    assert await auth.discover_touch_link("QK2") == "LinkB"
    with pytest.raises(AqualinkAuthError):
        await auth.discover_touch_link("missing")
    assert ("/v2/userId", "Bearer tok1") in seen
