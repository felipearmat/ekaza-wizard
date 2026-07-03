"""Orchestrates all provisioning steps, yielding SSE-ready progress events."""
import asyncio
import json
import os
from collections.abc import AsyncGenerator

import dashboard
import frigate
import ha_client
import localtuya_flow
import scripts_gen
from models import CameraInfo, TuyaCredentials

FRIGATE_SLUG = os.environ.get("FRIGATE_SLUG", "ccab4aaf_frigate-fa")
HA_HOST = os.environ.get("HA_HOST", "192.168.15.35")
FRIGATE_PORT = os.environ.get("FRIGATE_PORT", "5000")
FRIGATE_HOST = f"http://{HA_HOST}:{FRIGATE_PORT}"


def _event(camera: str, step: str, status: str, detail: str = "") -> str:
    payload = json.dumps({"camera": camera, "step": step, "status": status, "detail": detail})
    return f"data: {payload}\n\n"


async def provision_all(cameras: list[CameraInfo]) -> AsyncGenerator[str, None]:
    """
    Generator that provisions all cameras sequentially, yielding SSE events.
    Each event is a JSON object: {camera, step, status, detail}
    """
    # ── Frigate: apply all cameras at once ────────────────────────────────
    yield _event("global", "frigate_config", "running")
    ok, detail = frigate.apply(FRIGATE_SLUG, cameras)
    if ok:
        yield _event("global", "frigate_config", "ok", detail)
    else:
        yield _event("global", "frigate_config", "error", detail)

    # ── Per-camera steps ──────────────────────────────────────────────────
    for cam in cameras:
        # Scripts PTZ
        yield _event(cam.slug, "ptz_scripts", "running")
        ok, detail = scripts_gen.write_scripts(cam)
        yield _event(cam.slug, "ptz_scripts", "ok" if ok else "error", detail)

        # LocalTuya config flow
        yield _event(cam.slug, "localtuya", "running")
        ok, detail = await localtuya_flow.configure(cam)
        yield _event(cam.slug, "localtuya", "ok" if ok else "error", detail)

        await asyncio.sleep(0.1)  # brief pause between cameras

    # ── Global: reload scripts, restart Frigate, update dashboard ─────────
    yield _event("global", "reload_scripts", "running")
    ok = await ha_client.reload_scripts()
    yield _event("global", "reload_scripts", "ok" if ok else "error")

    yield _event("global", "restart_frigate", "running")
    ok = await ha_client.addon_restart(FRIGATE_SLUG)
    yield _event("global", "restart_frigate", "ok" if ok else "error")

    yield _event("global", "dashboard", "running")
    ok, detail = await dashboard.update_dashboard(cameras, FRIGATE_HOST)
    yield _event("global", "dashboard", "ok" if ok else "error", detail)

    yield _event("global", "done", "ok")
