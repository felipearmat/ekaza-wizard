"""
LocalTuya config flow automation via HA WebSocket.

The xZetsubou/hass-localtuya flow steps (as of v2025):
  Step 1 "user":    host, device_id, local_key, protocol_version, friendly_name, manual_dps
  Step 2..N "entity_X": one form per entity/DP with platform-specific fields

We fill step 1 from camera data, then auto-fill each entity step using EKAZA_ENTITIES.
If the flow schema changes in a future LocalTuya version, inspect the step's
data_schema in the result and extend the auto-fill logic below.
"""
import json

from constants import EKAZA_ENTITIES
from ha_client import with_websocket, _ws_send_recv
from models import CameraInfo


def _step1_data(cam: CameraInfo) -> dict:
    from constants import EKAZA_DPS_MANUAL
    return {
        "host": cam.ip,
        "device_id": cam.device_id,
        "local_key": cam.local_key,
        "protocol_version": "3.5",
        "friendly_name": f"eKaza {cam.name}",
        "manual_dps": EKAZA_DPS_MANUAL,
    }


def _entity_step_data(entity: dict) -> dict:
    """Build form data for one entity step from the EKAZA_ENTITIES definition."""
    platform = entity["platform"]
    base = {
        "platform": platform,
        "friendly_name": entity["friendly_name"],
        "id": entity["dp"],
    }

    if platform == "switch":
        base["is_passive_entity"] = entity.get("is_passive", False)

    elif platform == "select":
        opts = entity.get("select_options", {})
        base["select_options"] = opts

    elif platform == "number":
        base["min_value"] = entity.get("min_value", 0.0)
        base["max_value"] = entity.get("max_value", 100.0)
        base["step_size"] = entity.get("step_size", 1.0)

    return base


async def configure(cam: CameraInfo) -> tuple[bool, str]:
    """
    Run the full LocalTuya config flow for one camera.
    Returns (success, detail).
    """
    try:
        result = await _run_flow(cam)
        if result.get("type") == "create_entry":
            return True, f"Config entry created: {result.get('title', cam.name)}"
        return False, f"Unexpected flow result: {result.get('type')} — {result}"
    except Exception as e:
        return False, str(e)


async def _run_flow(cam: CameraInfo) -> dict:
    async def _action(ws, next_id):
        # ── Step 1: device credentials ─────────────────────────────────────
        r = await _ws_send_recv(ws, next_id(), "config_entries/flow/init", handler="localtuya")
        result = r.get("result", {})
        _assert_form(result, "init")

        r = await _ws_send_recv(
            ws, next_id(),
            "config_entries/flow/progress",
            flow_id=result["flow_id"],
            user_input=_step1_data(cam),
        )
        result = r.get("result", {})

        # ── Steps 2..N: one entity per DP ─────────────────────────────────
        entity_iter = iter(EKAZA_ENTITIES)
        while result.get("type") == "form":
            step_id = result.get("step_id", "")

            if step_id.startswith("entity") or step_id == "add_entity":
                try:
                    entity = next(entity_iter)
                    user_input = _entity_step_data(entity)
                except StopIteration:
                    # no more entities — signal finish (empty submit or "no_entity_id")
                    user_input = {}

            elif step_id == "no_more_entities" or step_id == "finish":
                user_input = {}

            else:
                # unknown step — submit empty to advance
                user_input = {}

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
