"""Provisioning pipeline: ONVIF → Frigate → LocalTuya → Scripts → Dashboard → Bridge."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator

import tinytuya
from homeassistant.core import HomeAssistant

from . import frigate as frigate_mod
from . import motion_bridge
from .dashboard import update_dashboard
from .ha_helpers import assign_entity_to_area, create_input_boolean, ensure_area, hide_entity
from .localtuya_flow import configure as configure_localtuya
from .models import CameraInfo, TuyaCredentials
from .scripts_gen import write_scripts

_LOGGER = logging.getLogger(__name__)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _probe_rtsp_port(ip: str) -> int:
    """Return the first open RTSP port — 8554 or 554. Default 554."""
    import socket
    for port in (8554, 554):
        try:
            with socket.create_connection((ip, port), timeout=3):
                return port
        except OSError:
            pass
    return 554


def _set_onvif(cam: CameraInfo) -> tuple[bool, str]:
    try:
        dev = tinytuya.Device(cam.device_id, cam.ip, cam.local_key, version=3.5)
        dev.set_socketTimeout(8)
        dev.set_value(237, True)
        time.sleep(0.5)
        # Try empty old password first (factory default), then current password (re-provision)
        for old_pwd in ("", cam.rtsp_password):
            pwd_json = json.dumps({"old": old_pwd, "new": cam.rtsp_password})
            dev.set_value(238, pwd_json)
            time.sleep(0.3)
        return True, "ONVIF habilitado"
    except Exception as exc:
        return False, str(exc)


async def _reload_frigate_integration(hass: HomeAssistant) -> bool:
    """Reload the Frigate HA integration so camera entities are created/removed."""
    for entry in hass.config_entries.async_entries():
        if entry.domain == "frigate":
            try:
                await hass.config_entries.async_reload(entry.entry_id)
                return True
            except Exception as exc:
                _LOGGER.warning("Frigate integration reload failed: %s", exc)
    return False


async def run(
    hass: HomeAssistant,
    creds: TuyaCredentials,
    cameras: list[CameraInfo],
    dashboard_path: str | None = None,
) -> AsyncGenerator[str, None]:
    # Total steps = cameras * 4 (onvif + localtuya + scripts + motion_bridge_boolean)
    #             + 5 (frigate + reload_scripts + reload_frigate_integration + dashboard + bridge)
    yield _sse("start", {"cameras": len(cameras), "total": len(cameras) * 4 + 5})

    # Step 0: Probe RTSP port + enable ONVIF + set password per camera
    for cam in cameras:
        cam.rtsp_port = await hass.async_add_executor_job(_probe_rtsp_port, cam.ip)
        ok, msg = await hass.async_add_executor_job(_set_onvif, cam)
        yield _sse("step", {"camera": cam.slug, "step": "onvif", "ok": ok, "detail": f"{msg} (porta {cam.rtsp_port})"})

    # Step 1: Update Frigate config via API — saves and triggers Frigate restart automatically
    ok, msg = await frigate_mod.apply(hass, cameras)
    yield _sse("step", {"step": "frigate", "ok": ok, "detail": msg})

    # Step 2: LocalTuya config entry + PTZ scripts + motion bridge boolean per camera
    for cam in cameras:
        ok, msg = await configure_localtuya(hass, cam)
        yield _sse("step", {"camera": cam.slug, "step": "localtuya", "ok": ok, "detail": msg})

        ok2, msg2 = await hass.async_add_executor_job(write_scripts, cam)
        yield _sse("step", {"camera": cam.slug, "step": "scripts", "ok": ok2, "detail": msg2})

        ok3, msg3 = await create_input_boolean(
            hass,
            f"{cam.slug}_motion_bridge",
            f"{cam.name} Motion Bridge",
        )
        yield _sse("step", {"camera": cam.slug, "step": "motion_bridge_boolean", "ok": ok3, "detail": msg3})

        cam_area_id = await ensure_area(hass, cam.name)
        bool_entity = f"input_boolean.{cam.slug}_motion_bridge"
        await hide_entity(hass, bool_entity)
        if cam_area_id:
            await assign_entity_to_area(hass, bool_entity, cam_area_id)

    # Step 3: Reload scripts via HA service
    try:
        await hass.services.async_call("script", "reload", blocking=True)
        yield _sse("step", {"step": "reload_scripts", "ok": True, "detail": "Scripts recarregados"})
    except Exception as exc:
        yield _sse("step", {"step": "reload_scripts", "ok": False, "detail": str(exc)})

    # Step 4: Wait for Frigate to finish restarting, then reload HA integration
    await asyncio.sleep(15)
    reloaded = await _reload_frigate_integration(hass)
    yield _sse("step", {
        "step": "reload_frigate_integration",
        "ok": reloaded,
        "detail": "Integração Frigate recarregada — entidades de câmera criadas" if reloaded
                  else "Recarregue a integração Frigate manualmente (Settings → Integrations → Frigate → Reload)",
    })

    # Assign Frigate camera entities to each camera's own area
    for cam in cameras:
        cam_area_id = await ensure_area(hass, cam.name)
        if cam_area_id:
            await assign_entity_to_area(hass, f"camera.{cam.slug}", cam_area_id)

    # Step 5: Add cards to Lovelace dashboard
    ok, msg = await update_dashboard(hass, cameras, target_path=dashboard_path)
    yield _sse("step", {"step": "dashboard", "ok": ok, "detail": msg})

    # Step 6: Start motion bridge
    motion_bridge.start(hass, cameras)
    yield _sse("step", {"step": "motion_bridge", "ok": True, "detail": "Bridge de movimento ativo"})

    yield _sse("done", {"cameras": [c.slug for c in cameras]})
