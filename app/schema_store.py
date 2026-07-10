"""Offline-first camera schema store.

Resolution order:
  1. /data/schemas/{model_slug}.json  — cached on first cloud fetch (persists across restarts)
  2. /schemas/{model_slug}.json       — bundled in image (read-only fallback)
  3. /schemas/{product_id}.json       — legacy bundled naming (backward-compat)
  4. Tuya cloud — /v1.1/specifications (writable DPs) + /v2.0/shadow/properties (read-only DPs)
"""
import asyncio
import json
import re
import time
from pathlib import Path

import tinytuya

_BUNDLED = Path("/schemas")
_DATA    = Path("/data/schemas")

# Codes excluded from LocalTuya entities (read-only, binary payloads, or write-only specials)
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


def _model_slug(model_name: str) -> str:
    """'EKRW-T5293' → 'ekrw_t5293'"""
    return re.sub(r"[^a-z0-9]+", "_", model_name.lower()).strip("_") or "unknown"


def _load_local(product_id: str, model_name: str = "") -> dict | None:
    """Try model-slug first (stable), then product_id (legacy bundled schemas)."""
    candidates: list[Path] = []
    if model_name:
        slug = _model_slug(model_name)
        candidates += [_DATA / f"{slug}.json", _BUNDLED / f"{slug}.json"]
    if product_id:
        candidates += [_DATA / f"{product_id}.json", _BUNDLED / f"{product_id}.json"]

    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
    return None


def _persist(schema: dict) -> None:
    """Save under model-slug key (stable across product_id batch changes)."""
    model_name = schema.get("model_name", "")
    product_id = schema.get("product_id", "")
    key = _model_slug(model_name) if model_name else product_id
    if not key:
        return
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        (_DATA / f"{key}.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2)
        )
    except Exception:
        pass


def _build_from_cloud(device_id: str, creds: dict) -> dict | None:
    """Fetch schema using Tuya cloud APIs.

    Uses two endpoints:
    - /v1.1/specifications → writable DPs with dp_ids (functions)
    - /v2.0/shadow/properties → ALL DPs including read-only (alarm_message, onvif_change_pwd, etc.)
    """
    try:
        cloud = tinytuya.Cloud(
            apiRegion=creds["region"],
            apiKey=creds["access_id"],
            apiSecret=creds["access_secret"],
            apiDeviceID=device_id,
        )

        spec_r = cloud.cloudrequest(f"/v1.1/devices/{device_id}/specifications", "GET")
        spec_fns = spec_r.get("result", {}).get("functions", [])
        if not spec_fns:
            return None

        # Shadow properties cover read-only DPs not in /specifications (alarm_message=185, etc.)
        sh_r = cloud.cloudrequest(f"/v2.0/cloud/thing/{device_id}/shadow/properties", "GET")
        shadow_props = sh_r.get("result", {}).get("properties", [])

        dev_r = cloud.cloudrequest(f"/v1.0/devices/{device_id}", "GET")
        dev = dev_r.get("result", {})

        return _assemble(dev, spec_fns, shadow_props)
    except Exception:
        return None


def _assemble(dev: dict, spec_fns: list[dict], shadow_props: list[dict] | None = None) -> dict:
    """Build a wizard schema from device info, writable specs, and shadow properties."""
    entities: list[dict] = []
    dp_map:   list[dict] = []
    dps_list: list[str]  = []

    # Writable DPs from /v1.1/specifications
    for f in spec_fns:
        dp    = f.get("dp_id")
        code  = f.get("code", "")
        ftype = f.get("type", "")
        try:
            values = json.loads(f.get("values", "{}") or "{}")
        except Exception:
            values = {}

        dp_map.append({"dp": dp, "code": code, "type": ftype, "writable": True, "values": values})

        if code in _SKIP_ENTITY or not ftype or not dp:
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

    # Merge read-only DPs from shadow/properties (alarm_message, onvif_change_pwd, etc.)
    # These are not in /specifications but LocalTuya needs them in dps_manual for polling
    existing_codes = {e["code"] for e in dp_map}
    for p in (shadow_props or []):
        dp   = p.get("dp_id")
        code = p.get("code", "")
        if code and code not in existing_codes and dp:
            dp_map.append({"dp": dp, "code": code, "type": p.get("type", ""),
                            "writable": False, "values": {}})
            dps_list.append(str(dp))
            existing_codes.add(code)

    codes = {e["code"] for e in dp_map}

    def _find_dp(code: str) -> int | None:
        return next((e["dp"] for e in dp_map if e["code"] == code), None)

    caps = {
        "ptz":             "ptz_control" in codes,
        "zoom":            "zoom_control" in codes,
        "audio":           "decibel_switch" in codes,
        "onvif":           "onvif_switch" in codes,
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
        "product_id":   dev.get("product_id", ""),
        "model_name":   dev.get("model", dev.get("product_name", "")),
        "category":     dev.get("category", ""),
        "fetched_at":   int(time.time()),
        "capabilities": caps,
        "dps_manual":   ",".join(dps_list),
        "entities":     entities,
        "dp_map":       dp_map,
    }


async def get(
    product_id: str,
    device_id: str | None = None,
    creds: dict | None = None,
    model_name: str = "",
) -> dict | None:
    """Return schema for a camera, fetching from cloud if needed."""
    schema = _load_local(product_id, model_name)
    if schema:
        return schema

    if device_id and creds:
        loop = asyncio.get_event_loop()
        schema = await loop.run_in_executor(None, _build_from_cloud, device_id, creds)
        if schema:
            _persist(schema)

    return schema


def is_camera(schema: dict | None, dev: dict) -> bool:
    """Return True if device should be included as a supported camera."""
    if schema:
        caps = schema.get("capabilities", {})
        return caps.get("ptz", False) or dev.get("category") == "sp"
    if dev.get("category") in ("sp", "ipc"):
        return True
    name = dev.get("name", "").lower()
    return any(k in name for k in ("ekaza", "camera", "câmera", "cam", "cctv"))
