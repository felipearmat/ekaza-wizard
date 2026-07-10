"""Orchestrates all provisioning steps, yielding SSE-ready progress events."""
import asyncio
import json
import os
from collections.abc import AsyncGenerator

import tinytuya

import dashboard
import frigate
import ha_client
import localtuya_flow
import scripts_gen
from constants import ONVIF_ENABLE_DP, ONVIF_SET_PWD_DP
from models import CameraInfo, TuyaCredentials

_FRIGATE_URL = f"http://localhost:{os.environ.get('FRIGATE_PORT', '5000')}"

# Cached after first successful detection; None forces re-detection
_frigate_slug: str | None = None


async def _get_frigate_slug() -> str:
    global _frigate_slug
    if _frigate_slug:
        return _frigate_slug
    detected = await ha_client.find_frigate_slug()
    slug = detected or os.environ.get("FRIGATE_SLUG", "")
    if not slug:
        raise RuntimeError(
            "Add-on Frigate não encontrado automaticamente. "
            "Configure FRIGATE_SLUG nas opções do add-on (ex: ccab4aaf_frigate)."
        )
    _frigate_slug = slug
    return _frigate_slug


def _event(camera: str, step: str, status: str, detail: str = "") -> str:
    return f"data: {json.dumps({'camera': camera, 'step': step, 'status': status, 'detail': detail})}\n\n"


def _enable_onvif(cam: CameraInfo) -> tuple[bool, str]:
    """Enable ONVIF and set RTSP password directly on the camera via tinytuya."""
    try:
        d = tinytuya.Device(dev_id=cam.device_id, address=cam.ip, local_key=cam.local_key, version=3.3)
        d.set_socketTimeout(5)
        r = d.set_value(ONVIF_ENABLE_DP, True)
        if r is None or "Error" in str(r):
            return False, f"ONVIF enable failed: {r}"
        pwd_payload = json.dumps({"pwd": cam.rtsp_password})
        r = d.set_value(ONVIF_SET_PWD_DP, pwd_payload)
        if r is None or "Error" in str(r):
            return False, f"ONVIF password set failed: {r}"
        return True, f"ONVIF enabled, password set for {cam.slug}"
    except Exception as e:
        return False, str(e)


async def provision_all(cameras: list[CameraInfo]) -> AsyncGenerator[str, None]:
    """SSE generator — one JSON event per provisioning step."""
    try:
        frigate_slug = await _get_frigate_slug()
    except RuntimeError as e:
        yield _event("global", "init", "error", str(e))
        return

    # ── Step 0: ONVIF enable on all cameras ──────────────────────────────────
    loop = asyncio.get_event_loop()
    for cam in cameras:
        yield _event(cam.slug, "onvif_setup", "running")
        ok, detail = await loop.run_in_executor(None, _enable_onvif, cam)
        yield _event(cam.slug, "onvif_setup", "ok" if ok else "warn", detail)
        # warn (not error) — camera may already have ONVIF enabled

    # ── Step 1: Frigate config for all cameras at once ────────────────────────
    yield _event("global", "frigate_config", "running")
    ok, detail = frigate.apply(frigate_slug, cameras)
    yield _event("global", "frigate_config", "ok" if ok else "error", detail)

    # ── Step 2: Per-camera scripts + LocalTuya ────────────────────────────────
    for cam in cameras:
        yield _event(cam.slug, "ptz_scripts", "running")
        ok, detail = scripts_gen.write_scripts(cam)
        yield _event(cam.slug, "ptz_scripts", "ok" if ok else "error", detail)

        yield _event(cam.slug, "localtuya", "running")
        ok, detail = await localtuya_flow.configure(cam)
        yield _event(cam.slug, "localtuya", "ok" if ok else "error", detail)

        await asyncio.sleep(0.1)

    # ── Step 3: Reload scripts, restart Frigate, update dashboard ────────────
    yield _event("global", "reload_scripts", "running")
    ok = await ha_client.reload_scripts()
    yield _event("global", "reload_scripts", "ok" if ok else "error")

    yield _event("global", "restart_frigate", "running")
    ok = await ha_client.addon_restart(frigate_slug)
    yield _event("global", "restart_frigate", "ok" if ok else "error")

    yield _event("global", "dashboard", "running")
    ok, detail = await dashboard.update_dashboard(cameras, _FRIGATE_URL)
    yield _event("global", "dashboard", "ok" if ok else "error", detail)

    yield _event("global", "done", "ok")
