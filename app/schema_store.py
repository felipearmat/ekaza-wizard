"""Offline-first camera schema store.

Resolution order:
  1. /data/schemas/{product_id}.json  — user-fetched, persisted across restarts
  2. /schemas/{product_id}.json       — bundled in image (read-only)
  3. Tuya cloud (device functions + shadow properties) — fetched on demand
"""
import asyncio
import json
from pathlib import Path
from typing import Any

import tinytuya

_BUNDLED = Path("/schemas")
_DATA = Path("/data/schemas")

# Codes that should never become LocalTuya entities (read-only or write-only specials)
_SKIP_ENTITY = {
    "sd_storge", "sd_status", "movement_detect_pic", "sd_format_state",
    "doorbell_active", "decibel_upload", "alarm_message", "initiative_message",
    "onvif_pw_changed", "onvif_ip_addr", "onvif_change_pwd",
    "motion_area", "zoom_value", "onvif_iptype_config",
}

_PASSIVE = {"ptz_stop", "zoom_stop", "ptz_calibration"}

_LABELS: dict[str, str] = {
    "basic_indicator":     "LED Indicador",
    "basic_flip":          "Imagem Espelhada",
    "basic_osd":           "OSD",
    "basic_private":       "Modo Privacidade",
    "motion_sensitivity":  "Sensibilidade Movimento",
    "basic_wdr":           "WDR (Contraste)",
    "sd_format":           "Formatar SD",
    "ptz_stop":            "PTZ Parar",
    "ptz_control":         "Controle PTZ",
    "ipc_auto_siren":      "Sirene Automática",
    "nightvision_mode":    "Visão Noturna",
    "ptz_calibration":     "PTZ Home",
    "motion_switch":       "Detecção de Movimento",
    "floodlight_switch":   "Luz de Iluminação",
    "decibel_switch":      "Detecção de Áudio",
    "decibel_sensitivity": "Sensibilidade de Áudio",
    "record_switch":       "Gravação SD",
    "record_mode":         "Modo de Gravação",
    "basic_device_volume": "Volume",
    "motion_tracking":     "Rastreamento Automático",
    "device_restart":      "Reiniciar Câmera",
    "zoom_control":        "Zoom",
    "zoom_stop":           "Zoom Parar",
    "motion_area_switch":  "Zona de Movimento",
    "humanoid_filter":     "Filtro Humano",
    "basic_anti_flicker":  "Anti-Oscilação",
    "ipc_preset_action":   "Ir para Preset",
    "ipc_object_outline":  "Contorno de Objetos",
    "ipc_preset_set":      "Salvar Preset",
    "zoom_value":          "Nível de Zoom",
    "ipc_audible_alarm":   "Alarme Sonoro",
    "onvif_switch":        "ONVIF",
    "event_linkage":       "Tipo de Evento",
    "memory_point_set":    "Ponto de Memória PTZ",
}


def _label(code: str) -> str:
    return _LABELS.get(code, code.replace("_", " ").title())


def _load_local(product_id: str) -> dict | None:
    for base in (_DATA, _BUNDLED):
        p = base / f"{product_id}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
    return None


def _persist(product_id: str, schema: dict) -> None:
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        (_DATA / f"{product_id}.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2)
        )
    except Exception:
        pass


def _build_from_cloud(device_id: str, creds: dict) -> dict | None:
    """Fetch and build a schema for device_id using Tuya cloud APIs."""
    try:
        cloud = tinytuya.Cloud(
            apiRegion=creds["region"],
            apiKey=creds["access_id"],
            apiSecret=creds["access_secret"],
            apiDeviceID=device_id,
        )

        fn_r = cloud.cloudrequest(f"/v1.0/iot-03/devices/{device_id}/functions")
        functions: dict[str, dict] = {
            f["code"]: f for f in fn_r.get("result", {}).get("functions", [])
        }

        sh_r = cloud.cloudrequest(f"/v2.0/cloud/thing/{device_id}/shadow/properties")
        props: list[dict] = sh_r.get("result", {}).get("properties", [])

        dev_r = cloud.cloudrequest(f"/v1.0/iot-03/devices/{device_id}")
        dev: dict = dev_r.get("result", {})

        if not props:
            return None

        return _assemble(dev, functions, props)
    except Exception:
        return None


def _assemble(dev: dict, functions: dict, props: list[dict]) -> dict:
    entities: list[dict] = []
    dp_map:   list[dict] = []
    dps_list: list[str]  = []

    for p in props:
        dp   = p.get("dp_id")
        code = p.get("code", "")
        fn   = functions.get(code, {})
        ftype = fn.get("type", "")
        try:
            values = json.loads(fn.get("values", "{}") or "{}")
        except Exception:
            values = {}

        dp_map.append({"dp": dp, "code": code, "type": ftype,
                        "writable": bool(fn), "values": values})

        if code in _SKIP_ENTITY or not ftype:
            continue

        dps_list.append(str(dp))

        if ftype == "Boolean":
            entities.append({
                "dp": dp, "platform": "switch",
                "friendly_name": _label(code),
                "is_passive": code in _PASSIVE,
            })
        elif ftype == "Enum":
            opts = {v: v for v in values.get("range", [])}
            entities.append({
                "dp": dp, "platform": "select",
                "friendly_name": _label(code),
                "select_options": opts,
            })
        elif ftype == "Integer":
            entities.append({
                "dp": dp, "platform": "number",
                "friendly_name": _label(code),
                "min_value": float(values.get("min", 0)),
                "max_value": float(values.get("max", 100)),
                "step_size": float(values.get("step", 1)),
            })

    codes = {e["code"] for e in dp_map}

    def _find_dp(code: str) -> int | None:
        return next((e["dp"] for e in dp_map if e["code"] == code), None)

    caps = {
        "ptz":           "ptz_control" in codes,
        "zoom":          "zoom_control" in codes,
        "audio":         "decibel_switch" in codes,
        "onvif":         "onvif_switch" in codes,
        "onvif_enable_dp": _find_dp("onvif_switch"),
        "onvif_pwd_dp":    _find_dp("onvif_change_pwd"),
        "alarm_dp":        _find_dp("alarm_message"),
        "motion_dp":       _find_dp("motion_switch"),
        "memory_points":   "memory_point_set" in codes,
        "presets":         "ipc_preset_action" in codes,
        "humanoid_filter": "humanoid_filter" in codes,
        "auto_tracking":   "motion_tracking" in codes,
    }

    return {
        "product_id": dev.get("product_id", ""),
        "model_name": dev.get("model", dev.get("product_name", "")),
        "category":   dev.get("category", ""),
        "capabilities": caps,
        "dps_manual": ",".join(dps_list),
        "entities":   entities,
        "dp_map":     dp_map,
    }


async def get(
    product_id: str,
    device_id: str | None = None,
    creds: dict | None = None,
) -> dict | None:
    """Return schema for product_id, or None if unavailable."""
    schema = _load_local(product_id)
    if schema:
        return schema

    if device_id and creds:
        loop = asyncio.get_event_loop()
        schema = await loop.run_in_executor(None, _build_from_cloud, device_id, creds)
        if schema:
            _persist(product_id, schema)

    return schema


def is_camera(schema: dict | None, dev: dict) -> bool:
    """Return True if device should be included as a supported camera."""
    if schema:
        caps = schema.get("capabilities", {})
        return caps.get("ptz", False) or dev.get("category") == "sp"
    # Fallback: category or name heuristic
    if dev.get("category") in ("sp", "ipc"):
        return True
    name = dev.get("name", "").lower()
    return any(k in name for k in ("ekaza", "camera", "câmera", "cam", "cctv"))
