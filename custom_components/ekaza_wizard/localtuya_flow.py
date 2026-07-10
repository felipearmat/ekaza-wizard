"""Add a device to the existing LocalTuya config entry directly (no config flow)."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from . import schema_store
from .models import CameraInfo

_LOGGER = logging.getLogger(__name__)

_DPS_MANUAL = (
    "101,103,104,105,106,107,110,111,116,119,120,124,"
    "127,132,134,138,139,140,150,151,160,161,162,163,"
    "164,168,170,178,188,190,198,199,231,236,237,239"
)


async def _resolve_entities(cam: CameraInfo) -> tuple[list[dict], str]:
    if cam.product_id:
        sch = await schema_store.get(cam.product_id)
        if sch and sch.get("entities"):
            return sch["entities"], sch.get("dps_manual", _DPS_MANUAL)
    from .constants import EKAZA_ENTITIES
    return EKAZA_ENTITIES, _DPS_MANUAL


def _build_entity(e: dict) -> dict:
    """Convert schema entity → LocalTuya entity format."""
    ent: dict = {
        "platform": e["platform"],
        "friendly_name": e["friendly_name"],
        "id": str(e["dp"]),
    }
    if e["platform"] == "switch":
        ent["is_passive_entity"] = e.get("is_passive", False)
    elif e["platform"] == "select":
        ent["select_options"] = e.get("select_options", {})
    elif e["platform"] == "number":
        ent["min_value"]  = e.get("min_value", 0.0)
        ent["max_value"]  = e.get("max_value", 100.0)
        ent["step_size"]  = e.get("step_size", 1.0)
    return ent


def _build_device(cam: CameraInfo, entities: list[dict], dps_manual: str) -> dict:
    return {
        "friendly_name": cam.name,
        "host": cam.ip,
        "device_id": cam.device_id,
        "local_key": cam.local_key,
        "protocol_version": "3.5",
        "manual_dps_strings": dps_manual,
        "entities": [_build_entity(e) for e in entities],
        "node_id": None,
    }


async def configure(hass: HomeAssistant, cam: CameraInfo) -> tuple[bool, str]:
    """Add camera to LocalTuya by directly updating the config entry."""
    entities, dps_manual = await _resolve_entities(cam)

    # Find the existing LocalTuya config entry
    lt_entries = [e for e in hass.config_entries.async_entries("localtuya")]
    if not lt_entries:
        return False, "LocalTuya não instalado — instale primeiro via HACS"

    entry = lt_entries[0]
    devices: dict = dict(entry.data.get("devices", {}))

    if cam.device_id in devices:
        return True, f"Device {cam.device_id[:8]}… já existe no LocalTuya"

    devices[cam.device_id] = _build_device(cam, entities, dps_manual)

    new_data = {**entry.data, "devices": devices}
    hass.config_entries.async_update_entry(entry, data=new_data)

    try:
        await hass.config_entries.async_reload(entry.entry_id)
        return True, f"Adicionado ao LocalTuya: {cam.name}"
    except Exception as exc:
        _LOGGER.warning("LocalTuya reload failed: %s", exc)
        return True, f"Adicionado (reload manual pode ser necessário): {cam.name}"
