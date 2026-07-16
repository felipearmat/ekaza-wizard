"""Client for the Tuya Proxy Companion add-on.

The companion runs as a privileged HAOS add-on (NET_ADMIN + host_network=true)
and exposes an HTTP API on localhost:8765. It owns the MITM proxy and iptables
rules so the HA Core integration doesn't need those OS-level capabilities.

Slug detection is auto-discovered via the Supervisor API — never hardcoded.
"""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Port the companion add-on listens on (matches companion config.yaml default)
_COMPANION_PORT = 8765
# Companion is identified by this string appearing anywhere in the add-on slug
_COMPANION_SLUG_FRAGMENT = "tuya_proxy_companion"
_COMPANION_NAME_FRAGMENT = "tuya proxy companion"


async def _supervisor_headers() -> dict[str, str]:
    import os

    token = os.environ.get("SUPERVISOR_TOKEN", "")
    return {"Authorization": f"Bearer {token}"}


async def find_companion_slug(hass: HomeAssistant) -> str | None:
    """Auto-detect the companion add-on slug via the Supervisor API."""
    headers = await _supervisor_headers()
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(
                "http://supervisor/addons",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            )
            if r.status != 200:
                return None
            data = await r.json()
            for addon in data.get("data", {}).get("addons", []):
                slug: str = addon.get("slug", "")
                name: str = addon.get("name", "").lower()
                if _COMPANION_SLUG_FRAGMENT in slug or _COMPANION_NAME_FRAGMENT in name:
                    return slug
    except Exception as exc:
        _LOGGER.debug("companion: supervisor lookup failed: %s", exc)
    return None


async def is_installed(hass: HomeAssistant) -> bool:
    """Return True if the companion add-on is installed (any state)."""
    return await find_companion_slug(hass) is not None


async def is_running(hass: HomeAssistant) -> bool:
    """Return True if companion is installed AND its API responds."""
    status = await get_status(hass)
    return status is not None


async def get_status(hass: HomeAssistant) -> dict | None:
    """GET /status from companion. Returns None if unreachable."""
    url = f"http://localhost:{_COMPANION_PORT}/status"
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(url, timeout=aiohttp.ClientTimeout(total=3))
            if r.status == 200:
                return await r.json()
    except Exception as exc:
        _LOGGER.debug("companion: /status unreachable: %s", exc)
    return None


async def update_cameras(hass: HomeAssistant, cameras: list[dict]) -> bool:
    """POST /cameras to sync camera list and iptables rules in the companion."""
    url = f"http://localhost:{_COMPANION_PORT}/cameras"
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(
                url,
                json={"cameras": cameras},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if r.status == 200:
                data = await r.json()
                return bool(data.get("ok"))
            _LOGGER.warning("companion: /cameras returned %d", r.status)
    except Exception as exc:
        _LOGGER.warning("companion: /cameras failed: %s", exc)
    return False


async def stop_proxy(hass: HomeAssistant) -> bool:
    """POST /proxy/stop — stops proxy and flushes iptables rules."""
    url = f"http://localhost:{_COMPANION_PORT}/proxy/stop"
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(url, timeout=aiohttp.ClientTimeout(total=10))
            return r.status == 200
    except Exception as exc:
        _LOGGER.warning("companion: /proxy/stop failed: %s", exc)
    return False
