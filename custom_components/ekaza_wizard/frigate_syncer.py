"""Persists Frigate switch state changes to the config file via Frigate's native API.

Flow:
  1. HA Frigate integration sends MQTT → Frigate applies change immediately (runtime).
  2. This syncer calls GET /api/config/raw → modifies the field → POST /api/config/save
     with save_option=silent, persisting the change so it survives a Frigate restart.

No companion or restart required.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
import yaml
from homeassistant.core import Event, HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

# Map entity_id suffix → Frigate config key path (cam_block key, nested key)
_SWITCH_MAP: dict[str, tuple[str, str]] = {
    "_detect":     ("detect",    "enabled"),
    "_snapshots":  ("snapshots", "enabled"),
    "_recordings": ("record",    "enabled"),
    "_motion":     ("motion",    "enabled"),
}

_debounce_tasks: dict[str, asyncio.Task] = {}


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
                                return f"http://{ip}:5000"
    except Exception as exc:
        _LOGGER.debug("Supervisor lookup failed for syncer: %s", exc)
    return "http://127.0.0.1:5000"


def _parse_switch(entity_id: str) -> tuple[str, str, str] | None:
    """Return (slug, config_block, config_key) if entity_id is a tracked Frigate switch."""
    if not entity_id.startswith("switch."):
        return None
    name = entity_id[len("switch."):]
    for suffix, (block, key) in _SWITCH_MAP.items():
        if name.endswith(suffix):
            return name[: -len(suffix)], block, key
    return None


async def _persist(base: str, slug: str, block: str, key: str, enabled: bool) -> None:
    try:
        async with aiohttp.ClientSession() as s:
            # 1. Fetch current raw config (JSON-encoded YAML string)
            r = await s.get(f"{base}/api/config/raw", timeout=aiohttp.ClientTimeout(total=5))
            if r.status != 200:
                _LOGGER.debug("Frigate config/raw returned %d — skipping persist", r.status)
                return
            raw_yaml_str = await r.json()

            # 2. Parse and modify
            config = yaml.safe_load(raw_yaml_str) or {}
            cam_block = config.get("cameras", {}).get(slug)
            if cam_block is None:
                _LOGGER.debug("Camera %s not in Frigate config — skipping persist", slug)
                return
            cam_block.setdefault(block, {})[key] = enabled

            # 3. Serialize and save (silent = no Frigate restart; MQTT already applied runtime)
            new_yaml = yaml.dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False)
            r2 = await s.post(
                f"{base}/api/config/save?save_option=silent",
                data=new_yaml.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            body = await r2.json()
            if body.get("success"):
                _LOGGER.debug("Persisted %s.%s.%s=%s to Frigate config", slug, block, key, enabled)
            else:
                _LOGGER.warning("Frigate config save rejected for %s.%s: %s", slug, block, body.get("message"))
    except Exception as exc:
        _LOGGER.debug("Frigate config persist failed for %s.%s: %s", slug, block, exc)


async def setup(hass: HomeAssistant) -> None:
    """Register Frigate switch state change listeners for config persistence."""
    base = await _resolve_frigate_base()
    _LOGGER.debug("Frigate syncer base URL: %s", base)

    @callback
    def _on_state_changed(event: Event) -> None:
        entity_id: str = event.data.get("entity_id", "")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if new_state is None:
            return
        parsed = _parse_switch(entity_id)
        if parsed is None:
            return
        if old_state and old_state.state == new_state.state:
            return

        slug, block, key = parsed
        enabled = new_state.state == "on"

        # Debounce — cancel pending write for the same entity
        if entity_id in _debounce_tasks and not _debounce_tasks[entity_id].done():
            _debounce_tasks[entity_id].cancel()

        async def _delayed() -> None:
            await asyncio.sleep(2)
            await _persist(base, slug, block, key, enabled)

        _debounce_tasks[entity_id] = hass.loop.create_task(_delayed())

    hass.bus.async_listen("state_changed", _on_state_changed)
    _LOGGER.info("Frigate config syncer active — switch changes will be persisted via API")
