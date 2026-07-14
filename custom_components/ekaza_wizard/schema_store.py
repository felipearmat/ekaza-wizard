"""Offline-first camera schema store (same logic as add-on, adapted for integration paths)."""
import asyncio
import json
from pathlib import Path

import tinytuya

_BUNDLED = Path(__file__).parent / "schemas"
_DATA = Path("/config/.storage/ekaza_wizard_schemas")
_SCHEMA_CACHE: dict[str, dict] = {}

_SKIP_ENTITY = {
    "sd_storge", "sd_status", "movement_detect_pic", "sd_format_state",
    "doorbell_active", "decibel_upload", "alarm_message", "initiative_message",
    "onvif_pw_changed", "onvif_ip_addr", "onvif_change_pwd",
    "motion_area", "zoom_value", "onvif_iptype_config",
}
_PASSIVE = {"ptz_stop", "zoom_stop", "ptz_calibration"}
_LABELS: dict[str, str] = {
    "basic_indicator": "LED Indicador", "basic_flip": "Imagem Espelhada",
    "basic_osd": "OSD", "basic_private": "Modo Privacidade",
    "motion_sensitivity": "Sensibilidade Movimento", "basic_wdr": "WDR (Contraste)",
    "sd_format": "Formatar SD", "ptz_stop": "PTZ Parar",
    "ptz_control": "Controle PTZ", "ipc_auto_siren": "Sirene Automática",
    "nightvision_mode": "Visão Noturna", "ptz_calibration": "PTZ Home",
    "motion_switch": "Detecção de Movimento", "floodlight_switch": "Luz de Iluminação",
    "decibel_switch": "Detecção de Áudio", "decibel_sensitivity": "Sensibilidade de Áudio",
    "record_switch": "Gravação SD", "record_mode": "Modo de Gravação",
    "basic_device_volume": "Volume", "motion_tracking": "Rastreamento Automático",
    "device_restart": "Reiniciar Câmera", "zoom_control": "Zoom",
    "zoom_stop": "Zoom Parar", "motion_area_switch": "Zona de Movimento",
    "humanoid_filter": "Filtro Humano", "basic_anti_flicker": "Anti-Oscilação",
    "ipc_preset_action": "Ir para Preset", "ipc_object_outline": "Contorno de Objetos",
    "ipc_preset_set": "Salvar Preset", "zoom_value": "Nível de Zoom",
    "ipc_audible_alarm": "Alarme Sonoro", "onvif_switch": "ONVIF",
    "event_linkage": "Tipo de Evento", "memory_point_set": "Ponto de Memória PTZ",
}


def _label(code: str) -> str:
    return _LABELS.get(code, code.replace("_", " ").title())


def load_local(product_id: str) -> dict | None:
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


def _fetch_cloud(device_id: str, creds: dict) -> dict | None:
    try:
        cloud = tinytuya.Cloud(
            apiRegion=creds["region"], apiKey=creds["access_id"],
            apiSecret=creds["access_secret"], apiDeviceID=device_id,
        )
        fn_r = cloud.cloudrequest(f"/v1.0/iot-03/devices/{device_id}/functions")
        functions = {f["code"]: f for f in fn_r.get("result", {}).get("functions", [])}
        sh_r = cloud.cloudrequest(f"/v2.0/cloud/thing/{device_id}/shadow/properties")
        props = sh_r.get("result", {}).get("properties", [])
        dev_r = cloud.cloudrequest(f"/v1.0/iot-03/devices/{device_id}")
        dev = dev_r.get("result", {})
        return _assemble(dev, functions, props) if props else None
    except Exception:
        return None


def _assemble(dev: dict, functions: dict, props: list) -> dict:
    entities, dp_map, dps = [], [], []
    for p in props:
        dp, code = p.get("dp_id"), p.get("code", "")
        fn = functions.get(code, {})
        ftype = fn.get("type", "")
        try:
            values = json.loads(fn.get("values", "{}") or "{}")
        except Exception:
            values = {}
        dp_map.append({"dp": dp, "code": code, "type": ftype, "writable": bool(fn), "values": values})
        if code in _SKIP_ENTITY or not ftype:
            continue
        dps.append(str(dp))
        if ftype == "Boolean":
            entities.append({"dp": dp, "platform": "switch", "friendly_name": _label(code), "is_passive": code in _PASSIVE})
        elif ftype == "Enum":
            entities.append({"dp": dp, "platform": "select", "friendly_name": _label(code), "select_options": {v: v for v in values.get("range", [])}})
        elif ftype == "Integer":
            entities.append({"dp": dp, "platform": "number", "friendly_name": _label(code), "min_value": float(values.get("min", 0)), "max_value": float(values.get("max", 100)), "step_size": float(values.get("step", 1))})

    codes = {e["code"] for e in dp_map}
    def _dp(c): return next((e["dp"] for e in dp_map if e["code"] == c), None)
    caps = {"ptz": "ptz_control" in codes, "zoom": "zoom_control" in codes, "onvif": "onvif_switch" in codes,
            "onvif_enable_dp": _dp("onvif_switch"), "onvif_pwd_dp": _dp("onvif_change_pwd"),
            "alarm_dp": _dp("alarm_message"), "motion_dp": _dp("motion_switch")}
    return {"product_id": dev.get("product_id", ""), "model_name": dev.get("model", ""),
            "category": dev.get("category", ""), "capabilities": caps,
            "dps_manual": ",".join(dps), "entities": entities, "dp_map": dp_map}


async def get(product_id: str, device_id: str | None = None, creds: dict | None = None) -> dict | None:
    if product_id in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[product_id]
    loop = asyncio.get_running_loop()
    schema = await loop.run_in_executor(None, load_local, product_id)
    if schema:
        _SCHEMA_CACHE[product_id] = schema
        return schema
    if device_id and creds:
        schema = await loop.run_in_executor(None, _fetch_cloud, device_id, creds)
        if schema:
            _persist(product_id, schema)
            _SCHEMA_CACHE[product_id] = schema
    return schema


_CAMERA_CODES = {"basic_flip", "nightvision_mode", "record_switch", "motion_switch", "basic_indicator"}
_DOORBELL_CODES = {"doorbell_active", "doorbell_call_countdown", "doorbell_volume"}


def is_camera(schema: dict | None, dev: dict) -> bool:
    if schema:
        caps = schema.get("capabilities", {})
        dp_codes = {e.get("code", "") for e in schema.get("dp_map", [])}
        # PTZ/ONVIF is definitive proof of a camera
        if caps.get("ptz") or caps.get("onvif"):
            return True
        # Has doorbell-specific DPs but no PTZ/ONVIF → not a camera
        if dp_codes & _DOORBELL_CODES:
            return False
        # Has camera-specific DPs → camera
        return bool(dp_codes & _CAMERA_CODES) and dev.get("category") in ("sp", "ipc")

    # No schema: fall back to category heuristics
    if dev.get("category") in ("sp", "ipc"):
        return True
    name_lower = dev.get("name", "").lower()
    return any(k in name_lower for k in ("ekaza", "camera", "câmera", "cam", "cctv"))
