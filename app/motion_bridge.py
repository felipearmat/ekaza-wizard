"""Motion bridge: tinytuya DP-185 listener → Frigate event API.

For each camera where input_boolean.{slug}_motion_bridge = 'on':
  - Opens a persistent tinytuya connection
  - When DP 185 (alarm_message) fires → POST to Frigate /api/events/{slug}/motion/start
  - When DP 185 goes quiet for IDLE_S seconds → POST /motion/end

The bridge checks bridge-enable states every POLL_S seconds and restarts listeners
for cameras that were toggled on/off.
"""
import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import aiohttp
import tinytuya

_log = logging.getLogger("motion_bridge")

_CAMERAS_FILE = Path("/data/cameras.json")
_MOTION_DP    = 185
_IDLE_S       = 8     # seconds of silence → motion end
_POLL_S       = 30    # bridge-state poll interval
_RECONNECT_S  = 10    # reconnect delay on connection error


def load_cameras() -> list[dict]:
    """Load provisioned camera metadata from persistent storage."""
    try:
        return json.loads(_CAMERAS_FILE.read_text())
    except Exception:
        return []


def save_cameras(cameras: list[dict]) -> None:
    """Persist camera metadata so the bridge survives restarts."""
    try:
        _CAMERAS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CAMERAS_FILE.write_text(json.dumps(cameras, ensure_ascii=False, indent=2))
    except Exception as e:
        _log.warning("Could not save cameras: %s", e)


class _CameraListener:
    """Runs in a daemon thread; listens for DP 185 and posts Frigate events."""

    def __init__(self, cam: dict, frigate_host: str, stop_event: threading.Event):
        self._cam = cam
        self._frigate_host = frigate_host.rstrip("/")
        self._stop = stop_event
        self._motion_active = False
        self._last_motion_ts = 0.0

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect_and_listen()
            except Exception as e:
                _log.warning("[%s] listener error: %s — reconnecting", self._cam["slug"], e)
            if not self._stop.is_set():
                time.sleep(_RECONNECT_S)

    def _connect_and_listen(self) -> None:
        cam = self._cam
        d = tinytuya.Device(
            dev_id=cam["device_id"],
            address=cam["ip"],
            local_key=cam["local_key"],
            version=3.3,
        )
        d.set_socketTimeout(5)
        d.set_dpsUsed({str(_MOTION_DP): None})

        _log.info("[%s] motion bridge connected", cam["slug"])

        while not self._stop.is_set():
            # Check if idle timeout has elapsed → end motion
            if self._motion_active and (time.monotonic() - self._last_motion_ts) > _IDLE_S:
                self._post_frigate_sync("end")
                self._motion_active = False

            data = d.heartbeat(nowait=True)
            if data is None:
                data = d.receive()
            if not data or "Error" in str(data):
                break

            dps = data.get("dps", {})
            if str(_MOTION_DP) in dps:
                _log.info("[%s] DP185 fired — motion detected", cam["slug"])
                self._last_motion_ts = time.monotonic()
                if not self._motion_active:
                    self._post_frigate_sync("start")
                    self._motion_active = True

    def _post_frigate_sync(self, action: str) -> None:
        """Synchronous HTTP POST to Frigate (called from thread)."""
        import urllib.request, urllib.error
        slug = self._cam["slug"]
        url  = f"{self._frigate_host}/api/events/{slug}/motion/{action}"
        try:
            req = urllib.request.Request(url, method="POST",
                                         data=json.dumps({}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3):
                pass
            _log.info("[%s] motion %s → Frigate OK", slug, action)
        except Exception as e:
            _log.warning("[%s] motion %s → Frigate failed: %s", slug, action, e)


class BridgeManager:
    """Singleton that manages camera listener threads."""

    def __init__(self, frigate_host: str):
        self._frigate_host = frigate_host
        self._threads: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        asyncio.get_event_loop().create_task(self._poll_loop())
        _log.info("Motion bridge manager started")

    async def stop(self) -> None:
        self._running = False
        for slug, (_, ev) in list(self._threads.items()):
            ev.set()
        self._threads.clear()

    async def _poll_loop(self) -> None:
        while self._running:
            await self._sync_listeners()
            await asyncio.sleep(_POLL_S)

    async def _sync_listeners(self) -> None:
        cameras = load_cameras()
        if not cameras:
            return

        # Read bridge states from HA
        try:
            import ha_client
            states_list = await ha_client.get_states()
            states = {s["entity_id"]: s["state"] for s in states_list}
        except Exception as e:
            _log.warning("Could not read HA states: %s", e)
            return

        desired_active: set[str] = set()
        cam_by_slug: dict[str, dict] = {}
        for cam in cameras:
            slug = cam["slug"]
            cam_by_slug[slug] = cam
            bridge_entity = f"input_boolean.{slug}_motion_bridge"
            if states.get(bridge_entity) == "on":
                desired_active.add(slug)

        # Stop threads that should no longer run
        for slug in list(self._threads):
            if slug not in desired_active:
                _, ev = self._threads.pop(slug)
                ev.set()
                _log.info("[%s] motion bridge disabled", slug)

        # Start threads for newly enabled cameras
        for slug in desired_active:
            if slug not in self._threads:
                cam = cam_by_slug[slug]
                ev = threading.Event()
                listener = _CameraListener(cam, self._frigate_host, ev)
                t = threading.Thread(target=listener.run, daemon=True, name=f"bridge-{slug}")
                t.start()
                self._threads[slug] = (t, ev)
                _log.info("[%s] motion bridge started", slug)

    def notify_provisioned(self, cameras: list[dict]) -> None:
        """Called by provisioner to update camera store without restart."""
        save_cameras(cameras)
        # Sync will pick up new cameras on next poll tick
