"""
LocalTuya config flow automation via HA WebSocket.

The xZetsubou/hass-localtuya flow steps (as of v2025):
  Step 1 "user":    host, device_id, local_key, protocol_version, friendly_name, manual_dps
  Step 2..N "entity_X": one form per entity/DP with platform-specific fields

Entity definitions come from the camera's schema (if known) or fall back to EKAZA_ENTITIES.
"""
import json

import schema_store
from constants import EKAZA_ENTITIES, EKAZA_DPS_MANUAL
from ha_client import with_websocket, _ws_send_recv
from models import CameraInfo


async def _resolve_entities(cam: CameraInfo) -> tuple[list[dict], str]:
    """Return (entities, dps_manual) for this camera, using schema if available."""
    if cam.product_id:
        sch = schema_store._load_local(cam.product_id)
        if sch and sch.get("entities"):
            return sch["entities"], sch.get("dps_manual", EKAZA_DPS_MANUAL)
    return EKAZA_ENTITIES, EKAZA_DPS_MANUAL


def _step1_data(cam: CameraInfo, dps_manual: str) -> dict:
    return {
        "host": cam.ip,
        "device_id": cam.device_id,
        "local_key": cam.local_key,
        "protocol_version": "3.5",
        "friendly_name": cam.name,
        "manual_dps": dps_manual,
    }


def _entity_step_data(entity: dict) -> dict:
    platform = entity["platform"]
    base = {
        "platform": platform,
        "friendly_name": entity["friendly_name"],
        "id": entity["dp"],
    }
    if platform == "switch":
        base["is_passive_entity"] = entity.get("is_passive", False)
    elif platform == "select":
        base["select_options"] = entity.get("select_options", {})
    elif platform == "number":
        base["min_value"] = entity.get("min_value", 0.0)
        base["max_value"] = entity.get("max_value", 100.0)
        base["step_size"] = entity.get("step_size", 1.0)
    return base


async def configure(cam: CameraInfo) -> tuple[bool, str]:
    """Run the full LocalTuya config flow for one camera. Returns (success, detail)."""
    try:
        result = await _run_flow(cam)
        if result.get("type") == "create_entry":
            return True, f"Config entry created: {result.get('title', cam.name)}"
        return False, f"Unexpected flow result: {result.get('type')} — {result}"
    except Exception as e:
        return False, str(e)


async def _run_flow(cam: CameraInfo) -> dict:
    entities, dps_manual = await _resolve_entities(cam)

    async def _action(ws, next_id):
        # ── Step 1: device credentials ─────────────────────────────────────
        r = await _ws_send_recv(ws, next_id(), "config_entries/flow/init", handler="localtuya")
        result = r.get("result", {})
        _assert_form(result, "init")

        r = await _ws_send_recv(
            ws, next_id(),
            "config_entries/flow/progress",
            flow_id=result["flow_id"],
            user_input=_step1_data(cam, dps_manual),
        )
        result = r.get("result", {})

        # ── Steps 2..N: one entity per DP ─────────────────────────────────
        entity_iter = iter(entities)
        while result.get("type") == "form":
            step_id = result.get("step_id", "")

            if step_id.startswith("entity") or step_id == "add_entity":
                try:
                    entity = next(entity_iter)
                    user_input = _entity_step_data(entity)
                except StopIteration:
                    user_input = {}  # no more entities — signal finish

            elif step_id in ("no_more_entities", "finish"):
                user_input = {}

            else:
                user_input = {}  # unknown step — advance with empty submit

            r = await _ws_send_recv(
                ws, next_id(),
                "config_entries/flow/progress",
                flow_id=result["flow_id"],
                user_input=user_input,
            )
            result = r.get("result", {})

        return result

    return await with_websocket(_action)


def _assert_form(result: dict, ctx: str) -> None:
    if result.get("type") not in ("form", "menu", "create_entry"):
        raise RuntimeError(f"[{ctx}] unexpected flow type: {result}")
