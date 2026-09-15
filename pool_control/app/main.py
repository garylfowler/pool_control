from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.aqualink_auth import AqualinkAuth, AqualinkAuthError
from app.aqualink_client import AqualinkClient, AqualinkCommandError
from app.config import Config
from app.entities import ALL_ENTITY_IDS, CHEMISTRY, CHLORINATOR, CHLORINATOR_OUTPUT_OPTIONS, LIGHTS, SENSORS, SWITCHES
from app.ha_client import HAClient, HAError
from app.notifier import Notifier
from app.settings import SettingsStore
from app.spa import SpaSession
from app.thermostat_runner import ThermostatRunner

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
RPM_MIN, RPM_MAX = 600, 3450


def asset_version() -> str:
    newest = max((STATIC_DIR / name).stat().st_mtime for name in ("app.js", "style.css", "index.html"))
    return str(int(newest))


def chlorinator_state(ha) -> dict:
    """Salt chlorinator readings from Tuya Local. The cell has no explicit "producing" flag:
    it chlorinates whenever water flows and the efficiency setting is above zero."""
    flow_text = ha.states.get(CHLORINATOR["flow"])
    flow = None if flow_text in (None, "unknown", "unavailable") else flow_text.strip().lower() == "flow"
    efficiency = ha.number(CHLORINATOR["efficiency"])
    salt_text = ha.states.get(CHLORINATOR["salt"])
    salt = None if salt_text in (None, "unknown", "unavailable") else salt_text
    return {
        "available": flow is not None or efficiency is not None or salt is not None,
        "flow": flow,
        "salt": salt,
        "efficiency": efficiency,
        "boost": ha.is_on(CHLORINATOR["boost"]),
        "producing": bool(flow) and (efficiency or 0) > 0,
        "output_options": CHLORINATOR_OUTPUT_OPTIONS,
    }


def _text(ha, entity_id: str) -> str | None:
    value = ha.states.get(entity_id)
    return None if value in (None, "unknown", "unavailable") else value


def chemistry_state(ha) -> dict:
    """WaterGuru readings. Values are numbers; each alert is the WaterGuru verdict text."""
    readings = {}
    for key in ("free_chlorine", "ph", "alkalinity", "cya"):
        readings[key] = ha.number(CHEMISTRY[key])
        readings[key + "_alert"] = _text(ha, CHEMISTRY[key + "_alert"])
    readings["cassette_days"] = ha.number(CHEMISTRY["cassette_days"])
    readings["last_measurement"] = _text(ha, CHEMISTRY["last_measurement"])
    readings["available"] = readings["free_chlorine"] is not None or readings["ph"] is not None
    return readings


def snapshot(ha, aqualink, runner, spa) -> dict:
    return {
        "ha": {
            "connected": ha.connected,
            "switches": {name: ha.is_on(eid) for name, eid in SWITCHES.items()},
            "lights": {name: ha.is_on(eid) for name, eid in LIGHTS.items()},
            "pool_temp": ha.number(SENSORS["pool_temp"]),
            "spa_temp": ha.number(SENSORS["spa_temp"]),
            "air_temp": ha.number(SENSORS["air_temp"]),
            "chlorinator": chlorinator_state(ha),
            "chemistry": chemistry_state(ha),
        },
        "aqualink": aqualink.state.to_dict(),
        "thermostat": runner.to_dict(),
        "spa": {"label": spa.label(), "cooling_down": spa.cooling_down},
    }


async def sse_stream(snap, notifier: Notifier):
    """Yields one SSE frame now, then one per change, and at least one every 15 s."""
    while True:
        yield f"data: {json.dumps(snap())}\n\n"
        await notifier.wait(15)


def build_app(ha, aqualink, runner, spa, notifier: Notifier, lifespan=None) -> FastAPI:
    app = FastAPI(title="Pool Control", lifespan=lifespan)

    def snap() -> dict:
        return snapshot(ha, aqualink, runner, spa)

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/state")
    async def state():
        return snap()

    @app.get("/api/events")
    async def events():
        return StreamingResponse(sse_stream(snap, notifier), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def ha_switch(domain: str, entity_id: str, on: bool) -> None:
        try:
            await ha.call_service(domain, "turn_on" if on else "turn_off", entity_id)
        except HAError as exc:
            raise HTTPException(503, detail=str(exc))

    async def aqualink_call(coro) -> None:
        try:
            await coro
        except (AqualinkCommandError, AqualinkAuthError, httpx.HTTPError) as exc:
            raise HTTPException(503, detail=str(exc))

    @app.post("/api/switch/{name}")
    async def switch(name: str, body: dict):
        on = bool(body.get("on"))
        if name in SWITCHES:
            await ha_switch("switch", SWITCHES[name], on)
        elif name in LIGHTS:
            await ha_switch("light", LIGHTS[name], on)
        elif name == "boost":
            await ha_switch("switch", CHLORINATOR["boost"], on)
        elif name == "lights_all":
            for entity_id in LIGHTS.values():
                await ha_switch("light", entity_id, on)
        elif name == "waterfall":
            await aqualink_call(aqualink.set_waterfall(on))
        else:
            raise HTTPException(404, detail=f"unknown switch {name}")
        notifier.notify()
        runner.wake()
        return snap()

    @app.post("/api/pump/preset/{index}")
    async def pump_preset(index: int):
        await aqualink_call(aqualink.set_preset(index))
        notifier.notify()
        return snap()

    @app.post("/api/pump/rpm")
    async def pump_rpm(body: dict):
        try:
            rpm = int(body.get("rpm"))
        except (TypeError, ValueError):
            raise HTTPException(400, detail="rpm must be a number")
        if not (RPM_MIN <= rpm <= RPM_MAX):
            raise HTTPException(400, detail=f"rpm must be between {RPM_MIN} and {RPM_MAX}")
        await aqualink_call(aqualink.set_custom_rpm(rpm))
        notifier.notify()
        return snap()

    @app.post("/api/chlorinator/output")
    async def chlorinator_output(body: dict):
        try:
            percent = int(body.get("percent"))
        except (TypeError, ValueError):
            raise HTTPException(400, detail="percent must be a number")
        if percent not in CHLORINATOR_OUTPUT_OPTIONS:
            raise HTTPException(400, detail=f"percent must be one of {CHLORINATOR_OUTPUT_OPTIONS}")
        try:
            await ha.call_service("select", "select_option", CHLORINATOR["efficiency"], option=str(percent))
        except HAError as exc:
            raise HTTPException(503, detail=str(exc))
        notifier.notify()
        return snap()

    @app.post("/api/thermostat")
    async def thermostat(body: dict):
        try:
            await runner.update_settings(**body)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        notifier.notify()
        return runner.to_dict()

    @app.post("/api/spa/start")
    async def spa_start():
        try:
            await spa.start()
        except HAError as exc:
            raise HTTPException(503, detail=str(exc))
        notifier.notify()
        return snap()

    @app.post("/api/spa/end")
    async def spa_end():
        try:
            await spa.end()
        except HAError as exc:
            raise HTTPException(503, detail=str(exc))
        notifier.notify()
        return snap()

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.get("/")
    async def index():
        # stamp the asset links with the newest asset mtime so phones never keep an old app.js
        html = (STATIC_DIR / "index.html").read_text().replace("__ASSET_V__", asset_version())
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _build_production_app() -> FastAPI:
    config = Config.from_env()
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    notifier = Notifier()
    # A browser-like User-Agent: the iAqualink cloud sits behind bot protection that can
    # answer non-browser clients with a challenge page instead of the WebTouch stream.
    http = httpx.AsyncClient(follow_redirects=True, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
        "Accept": "*/*",
        "Origin": "https://webtouch.iaqualink.net",
        "Referer": "https://webtouch.iaqualink.net/",
    })
    auth = AqualinkAuth(http, config.iaqualink_email, config.iaqualink_password)
    touch_link_cache: dict[str, str] = {}

    async def touch_link() -> str:
        if "link" not in touch_link_cache:
            touch_link_cache["link"] = await auth.discover_touch_link(config.iaqualink_serial)
        return touch_link_cache["link"]

    runner_holder: list[ThermostatRunner] = []

    def on_ha_change() -> None:
        notifier.notify()
        if runner_holder:
            runner_holder[0].wake()  # re-evaluate the thermostat on any state change

    ha = HAClient(config.ha_ws_url, config.ha_token, ALL_ENTITY_IDS, on_change=on_ha_change)
    aqualink = AqualinkClient(auth, http, touch_link, on_change=notifier.notify)
    runner = ThermostatRunner(ha, SettingsStore(config.data_dir / "settings.json"))
    runner_holder.append(runner)
    spa = SpaSession(ha, runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ha.start()
        if config.iaqualink_email and config.iaqualink_password:
            await aqualink.start()
        else:
            LOGGER.warning("iAqualink email/password not set; pump speed and waterfall disabled")
        thermostat_task = asyncio.create_task(runner.run(60), name="thermostat")
        try:
            yield
        finally:
            thermostat_task.cancel()
            try:
                await thermostat_task
            except (asyncio.CancelledError, Exception):
                pass
            await aqualink.stop()
            await ha.stop()
            await http.aclose()

    return build_app(ha, aqualink, runner, spa, notifier, lifespan=lifespan)


if Config.from_env().testing:
    app = FastAPI()  # tests build their own app via build_app()
else:
    app = _build_production_app()
