"""Internal HA helpers that require self-calling the HA WebSocket API."""
from __future__ import annotations

import json
import logging
import os

import aiohttp
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _ha_host(hass: HomeAssistant) -> str:
    try:
        ip = getattr(hass.config.api, "local_ip", None)
        if ip:
            return ip
    except Exception:
        pass
    return "127.0.0.1"


async def _ws_call(hass: HomeAssistant, msg: dict, token: str = "") -> dict:
    """Make a single authenticated WS call to HA from within HA Python code."""
    host = _ha_host(hass)
    if not token:
        token = os.environ.get("SUPERVISOR_TOKEN", "")
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"ws://{host}:8123/api/websocket") as ws:
            await ws.receive()  # auth_required
            await ws.send_str(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads((await ws.receive()).data)
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"WS auth failed: {auth.get('message', '?')}")
            await ws.send_str(json.dumps({**msg, "id": 1}))
            return json.loads((await ws.receive()).data)


async def _revoke_token(hass: HomeAssistant, rt) -> None:
    """Revoke a refresh token — handles both async (older HA) and sync (HA 2026+) versions."""
    try:
        result = hass.auth.async_remove_refresh_token(rt)
        import asyncio
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        _LOGGER.debug("Token revoke failed (non-critical): %s", exc)


async def _ws_call_with_temp_token(hass: HomeAssistant, msg: dict) -> dict:
    """Create a short-lived owner token, make a WS call, then revoke the token.

    Uses TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN so no client_id is required
    (HA 2026+ dropped client_id-free normal tokens).
    The refresh token is always revoked in a finally block even on failure.
    Note: async_remove_refresh_token became synchronous in HA 2026 — use _revoke_token().
    """
    users = await hass.auth.async_get_users()
    owner = next((u for u in users if not u.system_generated), None)
    if not owner:
        raise RuntimeError("Nenhum usuário proprietário encontrado em hass.auth")

    # TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN = "long_lived_access_token" — no client_id required
    rt = await hass.auth.async_create_refresh_token(
        owner,
        client_name="ekaza_wizard_tmp",
        token_type="long_lived_access_token",
    )
    try:
        token = hass.auth.async_create_access_token(rt)
        return await _ws_call(hass, msg, token=token)
    finally:
        await _revoke_token(hass, rt)


async def create_input_boolean(
    hass: HomeAssistant, object_id: str, name: str, icon: str = "mdi:motion-sensor"
) -> tuple[bool, str]:
    """Create a storage-based input_boolean helper.

    Tries three methods in order:
    1. Internal hass.data['input_boolean'] collection API
    2. WS API with a temporary owner token (hass.auth)
    3. WS API with SUPERVISOR_TOKEN (legacy fallback)
    """
    entity_id = f"input_boolean.{object_id}"
    if hass.states.get(entity_id) is not None:
        return True, f"Já existe: {entity_id}"

    # Method 1: internal storage collection
    collection = hass.data.get("input_boolean")
    if collection is None:
        _LOGGER.debug("create_input_boolean(%s): hass.data['input_boolean'] is None", object_id)
    elif not hasattr(collection, "async_create_item"):
        available = [m for m in dir(collection) if "create" in m.lower() and not m.startswith("_")]
        _LOGGER.debug(
            "create_input_boolean(%s): collection type=%s has no async_create_item; available: %s",
            object_id, type(collection).__name__, available,
        )
    else:
        try:
            item = await collection.async_create_item({"name": name, "icon": icon})
            return True, f"Criado: input_boolean.{item.get('id', '?')}"
        except Exception as exc:
            _LOGGER.warning(
                "create_input_boolean(%s) collection API failed: %s — tentando WS com token temporário",
                object_id, exc,
            )
            if hass.states.get(entity_id) is not None:
                return True, f"Criado (collection API): {entity_id}"

    # Method 2: WS with temporary owner token
    try:
        result = await _ws_call_with_temp_token(hass, {
            "type": "input_boolean/create",
            "name": name,
            "icon": icon,
        })
        if result.get("success"):
            return True, f"Criado via WS: input_boolean.{result['result']['id']}"
        msg = result.get("error", {}).get("message", "unknown error")
        _LOGGER.warning("create_input_boolean(%s) WS temp-token error: %s", object_id, msg)
    except Exception as exc:
        _LOGGER.warning(
            "create_input_boolean(%s) WS temp-token failed: %s — tentando SUPERVISOR_TOKEN",
            object_id, exc,
        )
        if hass.states.get(entity_id) is not None:
            return True, f"Criado via WS token: {entity_id}"

    # Method 3: legacy WS with SUPERVISOR_TOKEN
    try:
        result = await _ws_call(hass, {
            "type": "input_boolean/create",
            "name": name,
            "icon": icon,
        })
        if result.get("success"):
            return True, f"Criado via WS: input_boolean.{result['result']['id']}"
        msg = result.get("error", {}).get("message", "unknown error")
        return False, f"Erro WS: {msg}"
    except Exception as exc:
        _LOGGER.warning("create_input_boolean(%s) WS failed: %s", object_id, exc)
        return False, str(exc)


async def hide_entity(hass: HomeAssistant, entity_id: str) -> None:
    """Hide an entity from the Overview tab via the entity registry."""
    try:
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers.entity_registry import RegistryEntryHider
        registry = er.async_get(hass)
        entry = registry.async_get(entity_id)
        if entry is None:
            _LOGGER.debug("hide_entity: %s not in registry yet, skipping", entity_id)
            return
        registry.async_update_entity(entity_id, hidden_by=RegistryEntryHider.INTEGRATION)
        _LOGGER.debug("hide_entity: hidden %s", entity_id)
    except Exception as exc:
        _LOGGER.warning("hide_entity(%s) failed (non-critical): %s", entity_id, exc)


async def ensure_area(hass: HomeAssistant, name: str) -> str | None:
    """Return the area_id for 'name', creating it if needed. Returns None on failure."""
    try:
        from homeassistant.helpers import area_registry as ar
        registry = ar.async_get(hass)
        existing = next((a for a in registry.areas.values() if a.name == name), None)
        area = existing or registry.async_create(name)
        return area.id
    except Exception as exc:
        _LOGGER.warning("ensure_area(%r) failed (non-critical): %s", name, exc)
        return None


async def assign_entity_to_area(hass: HomeAssistant, entity_id: str, area_id: str) -> None:
    """Assign an entity to an area (silently skips if not in registry)."""
    try:
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
        entry = registry.async_get(entity_id)
        if entry is None:
            _LOGGER.debug("assign_entity_to_area: %s not in registry yet, skipping", entity_id)
            return
        registry.async_update_entity(entity_id, area_id=area_id)
        _LOGGER.debug("assign_entity_to_area: %s → area %s", entity_id, area_id)
    except Exception as exc:
        _LOGGER.warning("assign_entity_to_area(%s) failed (non-critical): %s", entity_id, exc)


_CAMERA_STORE_KEY = "ekaza_wizard.cameras"
_CAMERA_STORE_VERSION = 1


async def save_cameras(hass: HomeAssistant, cameras: list) -> None:
    """Persist camera list to HA storage (merge with existing by slug)."""
    from homeassistant.helpers.storage import Store
    store = Store(hass, _CAMERA_STORE_VERSION, _CAMERA_STORE_KEY)
    existing = await store.async_load() or {}
    by_slug: dict = {c["slug"]: c for c in existing.get("cameras", [])}
    for cam in cameras:
        by_slug[cam.slug] = cam.model_dump()
    await store.async_save({"cameras": list(by_slug.values())})


async def load_cameras(hass: HomeAssistant) -> list:
    """Load persisted camera list from HA storage."""
    from homeassistant.helpers.storage import Store
    from .models import CameraInfo
    store = Store(hass, _CAMERA_STORE_VERSION, _CAMERA_STORE_KEY)
    data = await store.async_load()
    if not data:
        return []
    try:
        return [CameraInfo(**c) for c in data.get("cameras", [])]
    except Exception as exc:
        _LOGGER.warning("load_cameras: failed to parse stored cameras: %s", exc)
        return []


async def remove_camera_from_store(hass: HomeAssistant, slug: str) -> None:
    """Remove a single camera from persisted storage by slug."""
    from homeassistant.helpers.storage import Store
    store = Store(hass, _CAMERA_STORE_VERSION, _CAMERA_STORE_KEY)
    data = await store.async_load() or {}
    cameras = [c for c in data.get("cameras", []) if c.get("slug") != slug]
    await store.async_save({"cameras": cameras})


async def delete_input_boolean(
    hass: HomeAssistant, object_id: str
) -> tuple[bool, str]:
    """Delete a storage-based input_boolean helper.

    Tries three methods in order (same pattern as create).
    """
    entity_id = f"input_boolean.{object_id}"
    if hass.states.get(entity_id) is None:
        return True, f"Não encontrado: {entity_id} (ok)"

    # Method 1: internal storage collection
    collection = hass.data.get("input_boolean")
    if collection is not None and hasattr(collection, "async_delete_item"):
        try:
            await collection.async_delete_item(object_id)
            return True, f"Removido: {entity_id}"
        except Exception as exc:
            _LOGGER.warning(
                "delete_input_boolean(%s) collection API failed: %s — tentando WS",
                object_id, exc,
            )

    # Method 2: WS with temporary owner token
    try:
        result = await _ws_call_with_temp_token(hass, {
            "type": "input_boolean/delete",
            "input_boolean_id": object_id,
        })
        if result.get("success"):
            return True, f"Removido via WS: {entity_id}"
        msg = result.get("error", {}).get("message", "unknown error")
        _LOGGER.warning("delete_input_boolean(%s) WS temp-token error: %s", object_id, msg)
    except Exception as exc:
        _LOGGER.warning(
            "delete_input_boolean(%s) WS temp-token failed: %s — tentando SUPERVISOR_TOKEN",
            object_id, exc,
        )

    # Method 3: legacy WS with SUPERVISOR_TOKEN
    try:
        result = await _ws_call(hass, {
            "type": "input_boolean/delete",
            "input_boolean_id": object_id,
        })
        if result.get("success"):
            return True, f"Removido via WS: {entity_id}"
        msg = result.get("error", {}).get("message", "unknown error")
        _LOGGER.warning("delete_input_boolean(%s) WS error: %s", object_id, msg)
        return False, f"Não removido — {msg}"
    except Exception as exc:
        _LOGGER.warning("delete_input_boolean(%s) WS failed: %s", object_id, exc)
        return False, str(exc)
