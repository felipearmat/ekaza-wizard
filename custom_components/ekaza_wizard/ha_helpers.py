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


async def _ws_call(hass: HomeAssistant, msg: dict) -> dict:
    """Make a single authenticated WS call to HA from within HA Python code."""
    host = _ha_host(hass)
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


async def create_input_boolean(
    hass: HomeAssistant, object_id: str, name: str, icon: str = "mdi:motion-sensor"
) -> tuple[bool, str]:
    """Create a storage-based input_boolean helper. No-op if already exists."""
    entity_id = f"input_boolean.{object_id}"
    if hass.states.get(entity_id) is not None:
        return True, f"Já existe: {entity_id}"
    try:
        result = await _ws_call(hass, {
            "type": "input_boolean/create",
            "name": name,
            "icon": icon,
        })
        if result.get("success"):
            created_id = result["result"]["id"]
            return True, f"Criado: input_boolean.{created_id}"
        msg = result.get("error", {}).get("message", "unknown error")
        return False, f"Erro WS: {msg}"
    except Exception as exc:
        _LOGGER.warning("create_input_boolean(%s) failed: %s", object_id, exc)
        return False, str(exc)


async def delete_input_boolean(
    hass: HomeAssistant, object_id: str
) -> tuple[bool, str]:
    """Delete a storage-based input_boolean helper. No-op if not found."""
    entity_id = f"input_boolean.{object_id}"
    if hass.states.get(entity_id) is None:
        return True, f"Não encontrado: {entity_id} (ok)"
    try:
        result = await _ws_call(hass, {
            "type": "input_boolean/delete",
            "input_boolean_id": object_id,
        })
        if result.get("success"):
            return True, f"Removido: {entity_id}"
        msg = result.get("error", {}).get("message", "unknown error")
        # Treat "not found" errors as ok — entity was already removed
        if "not found" in msg.lower() or "unknown" in msg.lower():
            return True, f"Já removido: {entity_id} (ok)"
        _LOGGER.warning("delete_input_boolean(%s) WS error: %s", object_id, msg)
        return True, f"Aviso — WS: {msg} (boolean pode já estar removido)"
    except Exception as exc:
        _LOGGER.warning("delete_input_boolean(%s) failed: %s", object_id, exc)
        return True, f"Aviso — {exc} (boolean pode já estar removido)"
