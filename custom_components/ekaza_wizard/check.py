"""Pre-flight dependency checks for eKaza Wizard."""
from __future__ import annotations

import asyncio
import logging
import os
import socket

import aiohttp
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_SUPERVISOR = "http://supervisor"


def _supervisor_headers() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"}


async def _addon_state(slug: str) -> tuple[str, str]:
    """Return (state, version) for a Supervisor add-on slug."""
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(
                f"{_SUPERVISOR}/addons/{slug}/info",
                headers=_supervisor_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            )
            d = (await r.json()).get("data", {})
            return d.get("state", "unknown"), d.get("version", "?")
    except Exception:
        return "unknown", "?"


async def _find_frigate_slug() -> str | None:
    """Locate the Frigate add-on slug dynamically via Supervisor API."""
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(
                f"{_SUPERVISOR}/addons",
                headers=_supervisor_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            )
            data = await r.json()
        for addon in data.get("data", {}).get("addons", []):
            slug = addon.get("slug", "")
            if "frigate" in slug.lower():
                return slug
    except Exception:
        pass
    return None


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except Exception:
        return False


async def run(hass: HomeAssistant) -> dict:
    """Return {check_key: {ok, label, msg}} for all dependencies."""
    loop = asyncio.get_running_loop()
    results: dict = {}

    # --- LocalTuya ---
    lt = hass.config_entries.async_entries("localtuya")
    results["localtuya"] = {
        "ok": bool(lt),
        "label": "LocalTuya",
        "msg": f"{len(lt)} entrada(s) configurada(s)" if lt else "Não instalado — instale via HACS",
    }

    # --- Frigate add-on (auto-detect slug via Supervisor API) ---
    frigate_slug = await _find_frigate_slug()
    if frigate_slug:
        state, ver = await _addon_state(frigate_slug)
    else:
        state, ver = "not_found", "?"
    frigate_ok = state == "started"
    results["frigate_addon"] = {
        "ok": frigate_ok,
        "label": "Frigate add-on",
        "msg": f"Rodando (v{ver})" if frigate_ok else f"Estado: {state} — inicie o add-on Frigate",
    }

    # --- Host IP for port checks ---
    host = "127.0.0.1"
    try:
        if hass.config.api and hass.config.api.local_ip:
            host = hass.config.api.local_ip
    except Exception:
        pass

    # --- go2rtc RTSP :8554 ---
    rtsp_ok = await loop.run_in_executor(None, _tcp_open, host, 8554)
    results["go2rtc_rtsp"] = {
        "ok": rtsp_ok,
        "label": "go2rtc RTSP :8554",
        "msg": "Porta acessível" if rtsp_ok else "Porta não mapeada — adicione 8554/tcp nas Network Options do Frigate",
    }

    # --- Frigate API :5000 ---
    api_ok = await loop.run_in_executor(None, _tcp_open, host, 5000)
    results["frigate_api"] = {
        "ok": api_ok,
        "label": "Frigate API :5000",
        "msg": "API acessível" if api_ok else "API não responde — Frigate pode estar inicializando",
    }

    return results
