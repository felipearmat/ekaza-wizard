"""Background motion bridge: tinytuya DP 185 listener → Frigate manual events."""
import asyncio
import logging
import threading
import time

import aiohttp
import tinytuya

from homeassistant.core import HomeAssistant

from .models import CameraInfo

_LOGGER = logging.getLogger(__name__)
_ALARM_DP = 185
_RECONNECT_DELAY = 10
_EVENT_DURATION = 30  # seconds the Frigate manual event lasts after each trigger


async def fire_event_for_slug(slug: str, hass: HomeAssistant) -> None:
    """Fire a Frigate manual motion event for a camera slug.

    Shared by _CameraListener (local Tuya push) and TuyaProxy (MITM cloud MQTT).
    Respects the input_boolean.{slug}_motion_bridge toggle.
    """
    ib = hass.states.get(f"input_boolean.{slug}_motion_bridge")
    if ib is not None and ib.state != "on":
        _LOGGER.debug("Bridge disabled for %s (input_boolean is %s)", slug, ib.state)
        return

    frigate_base = await _resolve_frigate_base()
    url = f"{frigate_base}/api/events/{slug}/motion/create"
    try:
        async with aiohttp.ClientSession() as session:
            r = await session.post(
                url,
                json={"duration": _EVENT_DURATION},
                timeout=aiohttp.ClientTimeout(total=5),
            )
            if r.status != 200:
                body = await r.text()
                _LOGGER.debug("Event create failed %s (%d): %s", slug, r.status, body)
            else:
                _LOGGER.warning("Motion event created for %s", slug)
    except Exception as exc:
        _LOGGER.warning("Motion bridge POST failed for %s: %s", slug, exc)


class _CameraListener(threading.Thread):
    def __init__(
        self,
        cam: CameraInfo,
        loop: asyncio.AbstractEventLoop,
        stop: threading.Event,
        frigate_base: str,
        hass: HomeAssistant,
    ):
        super().__init__(daemon=True, name=f"ekaza_bridge_{cam.slug}")
        self._cam = cam
        self._loop = loop
        self._stop = stop
        self._frigate_base = frigate_base
        self._hass = hass

    def run(self) -> None:
        cam = self._cam
        while not self._stop.is_set():
            try:
                dev = tinytuya.Device(cam.device_id, cam.ip, cam.local_key, version=3.5)
                dev.set_socketTimeout(10)
                dev.set_socketPersistent(True)
                dev.status()  # establish connection before listening for push events
                _LOGGER.warning("Motion bridge connected: %s", cam.slug)
                heartbeat_at = time.time() + 20
                while not self._stop.is_set():
                    data = dev.receive()
                    if data:
                        dps = data.get("dps", {})
                        if _ALARM_DP in dps:
                            asyncio.run_coroutine_threadsafe(self._fire_event(), self._loop)
                        else:
                            _LOGGER.debug("Bridge %s received DPs: %s", cam.slug, dps)
                    if time.time() >= heartbeat_at:
                        dev.heartbeat(nowait=True)
                        heartbeat_at = time.time() + 20
            except Exception as exc:
                if not self._stop.is_set():
                    _LOGGER.warning(
                        "Bridge %s error: %s — reconnecting in %ds",
                        cam.slug, exc, _RECONNECT_DELAY,
                    )
                    time.sleep(_RECONNECT_DELAY)

    async def _fire_event(self) -> None:
        await fire_event_for_slug(self._cam.slug, self._hass)


async def _resolve_frigate_base() -> str:
    """Resolve Frigate container IP via Supervisor API. Falls back to 127.0.0.1."""
    import os
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return "http://127.0.0.1:5000"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get("http://supervisor/addons", headers=headers,
                            timeout=aiohttp.ClientTimeout(total=5))
            if r.status == 200:
                data = await r.json()
                for addon in data.get("data", {}).get("addons", []):
                    slug = addon.get("slug", "")
                    name = addon.get("name", "").lower()
                    if "frigate" in slug.lower() or "frigate" in name:
                        info_r = await s.get(f"http://supervisor/addons/{slug}/info",
                                             headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=5))
                        if info_r.status == 200:
                            ip = (await info_r.json()).get("data", {}).get("ip_address")
                            if ip:
                                _LOGGER.debug("Motion bridge Frigate IP: %s", ip)
                                return f"http://{ip}:5000"
    except Exception as exc:
        _LOGGER.debug("Supervisor lookup failed for motion bridge: %s", exc)
    return "http://127.0.0.1:5000"


class _BridgeManager:
    def __init__(self) -> None:
        self._listeners: dict[str, tuple[_CameraListener, threading.Event]] = {}

    async def start_all(self, hass: HomeAssistant, cameras: list[CameraInfo]) -> None:
        frigate_base = await _resolve_frigate_base()
        loop = hass.loop
        for cam in cameras:
            if cam.slug in self._listeners:
                continue
            stop = threading.Event()
            listener = _CameraListener(cam, loop, stop, frigate_base, hass)
            listener.start()
            self._listeners[cam.slug] = (listener, stop)
            _LOGGER.warning("Motion bridge started: %s → %s", cam.slug, frigate_base)

    def stop_all(self) -> None:
        for slug, (_, stop) in list(self._listeners.items()):
            stop.set()
            _LOGGER.info("Motion bridge stopped: %s", slug)
        self._listeners.clear()

    def stop_for(self, slug: str) -> None:
        if slug in self._listeners:
            _, stop = self._listeners.pop(slug)
            stop.set()
            _LOGGER.info("Motion bridge stopped for: %s", slug)


_manager = _BridgeManager()


async def start(hass: HomeAssistant, cameras: list[CameraInfo]) -> None:
    await _manager.start_all(hass, cameras)


def stop() -> None:
    _manager.stop_all()


def stop_for(slug: str) -> None:
    _manager.stop_for(slug)
