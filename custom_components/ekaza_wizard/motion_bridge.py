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
                dev = tinytuya.Device(cam.device_id, cam.ip, cam.local_key, version=3.4)
                dev.set_socketTimeout(10)
                dev.set_persistentConnection(True)
                _LOGGER.debug("Motion bridge connected: %s", cam.slug)
                while not self._stop.is_set():
                    data = dev.receive()
                    if not data:
                        continue
                    if _ALARM_DP in data.get("dps", {}):
                        asyncio.run_coroutine_threadsafe(self._fire_event(), self._loop)
            except Exception as exc:
                if not self._stop.is_set():
                    _LOGGER.debug(
                        "Bridge %s error: %s — reconnecting in %ds",
                        cam.slug, exc, _RECONNECT_DELAY,
                    )
                    time.sleep(_RECONNECT_DELAY)

    async def _fire_event(self) -> None:
        slug = self._cam.slug

        # Respect input_boolean.{slug}_motion_bridge toggle
        ib = self._hass.states.get(f"input_boolean.{slug}_motion_bridge")
        if ib is not None and ib.state != "on":
            _LOGGER.debug("Bridge disabled for %s (input_boolean is %s)", slug, ib.state)
            return

        url = f"{self._frigate_base}/api/events/{slug}/motion/create"
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
                    _LOGGER.debug("Motion event created for %s", slug)
        except Exception as exc:
            _LOGGER.debug("Motion bridge POST failed for %s: %s", slug, exc)


class _BridgeManager:
    def __init__(self) -> None:
        self._listeners: dict[str, tuple[_CameraListener, threading.Event]] = {}
        self._frigate_base = "http://127.0.0.1:5000"

    def start_all(self, hass: HomeAssistant, cameras: list[CameraInfo]) -> None:
        loop = hass.loop
        # Use LAN IP so the bridge reaches Frigate from HA core container
        try:
            ip = getattr(hass.config.api, "local_ip", None)
            if ip:
                self._frigate_base = f"http://{ip}:5000"
        except Exception:
            pass

        for cam in cameras:
            if cam.slug in self._listeners:
                continue
            stop = threading.Event()
            listener = _CameraListener(cam, loop, stop, self._frigate_base, hass)
            listener.start()
            self._listeners[cam.slug] = (listener, stop)
            _LOGGER.info("Motion bridge started: %s → %s", cam.slug, self._frigate_base)

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


def start(hass: HomeAssistant, cameras: list[CameraInfo]) -> None:
    _manager.start_all(hass, cameras)


def stop() -> None:
    _manager.stop_all()


def stop_for(slug: str) -> None:
    _manager.stop_for(slug)
