"""Frigate config management via Frigate native API (/api/config/raw + /api/config/save)."""
import copy
import json
import logging

import aiohttp
import yaml

from .models import CameraInfo

_LOGGER = logging.getLogger(__name__)
_FRIGATE_PORT = 5000


def _frigate_base(hass=None) -> str:
    try:
        if hass is not None:
            ip = getattr(hass.config.api, "local_ip", None)
            if ip:
                return f"http://{ip}:{_FRIGATE_PORT}"
    except Exception:
        pass
    return f"http://127.0.0.1:{_FRIGATE_PORT}"


def _camera_block(cam: CameraInfo) -> dict:
    return {
        "ffmpeg": {
            "hwaccel_args": [],
            "inputs": [{"path": f"rtsp://127.0.0.1:8554/{cam.slug}", "roles": ["record", "detect"]}],
        },
        "detect": {"width": 640, "height": 360, "fps": 5},
        "record": {"enabled": True},
    }


def _stream_source(cam: CameraInfo) -> list[str]:
    # ffmpeg: prefix needed — some models send malformed SDP that go2rtc rejects natively
    return [f"ffmpeg:rtsp://{cam.rtsp_username}:{cam.rtsp_password}@{cam.ip}:{cam.rtsp_port}/stream0#video=copy"]


async def _fetch_raw_config(base: str) -> dict:
    """Fetch current Frigate config as a parsed dict via /api/config/raw."""
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"{base}/api/config/raw", timeout=aiohttp.ClientTimeout(total=5))
        r.raise_for_status()
        text = await r.text()
        try:
            yaml_str = json.loads(text)
        except Exception:
            yaml_str = text
        return yaml.safe_load(yaml_str) or {}


async def _save_config(base: str, config: dict, option: str = "restart") -> tuple[bool, str]:
    """POST /api/config/save with the full YAML.

    option values:
      'restart' — save + restart Frigate (use for structural changes: add/remove cameras)
      'silent'  — save only, no restart (use for runtime flag changes; MQTT applied already)
    """
    new_yaml = yaml.dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False)
    async with aiohttp.ClientSession() as s:
        r = await s.post(
            f"{base}/api/config/save?save_option={option}",
            data=new_yaml.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=aiohttp.ClientTimeout(total=15),
        )
        body = await r.json()
        if body.get("success"):
            return True, body.get("message", "ok")
        return False, body.get("message", f"HTTP {r.status}")


def _merge_cameras(existing: dict, cameras: list[CameraInfo]) -> dict:
    merged = copy.deepcopy(existing)
    merged.setdefault("go2rtc", {}).setdefault("streams", {})
    merged.setdefault("cameras", {})
    for cam in cameras:
        merged["go2rtc"]["streams"][cam.slug] = _stream_source(cam)
        merged["cameras"][cam.slug] = _camera_block(cam)
    for group in merged.get("camera_groups", {}).values():
        cams = group.setdefault("cameras", [])
        for cam in cameras:
            if cam.slug not in cams:
                cams.append(cam.slug)
    return merged


def _remove_from_config_dict(cfg: dict, slug: str) -> tuple[dict, bool]:
    changed = False
    if slug in cfg.get("go2rtc", {}).get("streams", {}):
        del cfg["go2rtc"]["streams"][slug]
        changed = True
    if slug in cfg.get("cameras", {}):
        del cfg["cameras"][slug]
        changed = True
    for group in cfg.get("camera_groups", {}).values():
        cams = group.get("cameras", [])
        if slug in cams:
            cams.remove(slug)
            changed = True
    return cfg, changed


async def apply(hass, cameras: list[CameraInfo]) -> tuple[bool, str]:
    """Add cameras to Frigate config and restart Frigate. Returns (ok, message)."""
    base = _frigate_base(hass)
    try:
        existing = await _fetch_raw_config(base)
        merged = _merge_cameras(existing, cameras)
        ok, msg = await _save_config(base, merged, option="restart")
        if ok:
            return True, f"Config salvo — Frigate reiniciando ({len(cameras)} câmera(s))"
        return False, f"Frigate rejeitou o config: {msg}"
    except Exception as exc:
        _LOGGER.error("Frigate apply failed: %s", exc)
        return False, f"Erro ao atualizar config Frigate: {exc}"


async def remove_camera(slug: str, hass=None) -> tuple[bool, str]:
    """Remove camera from Frigate config and restart Frigate. Returns (ok, message)."""
    base = _frigate_base(hass)
    try:
        existing = await _fetch_raw_config(base)
        updated, changed = _remove_from_config_dict(existing, slug)
        if not changed:
            return True, "Câmera não estava na config Frigate (nada a remover)"
        ok, msg = await _save_config(base, updated, option="restart")
        if ok:
            return True, "Câmera removida e Frigate reiniciando"
        return False, f"Frigate rejeitou o config: {msg}"
    except Exception as exc:
        _LOGGER.error("Frigate remove_camera(%s) failed: %s", slug, exc)
        return False, f"Erro ao remover câmera do config Frigate: {exc}"


async def restart_frigate(hass=None) -> bool:
    """Restart Frigate by re-saving current config with save_option=restart."""
    base = _frigate_base(hass)
    try:
        existing = await _fetch_raw_config(base)
        ok, _ = await _save_config(base, existing, option="restart")
        return ok
    except Exception as exc:
        _LOGGER.warning("Frigate restart failed: %s", exc)
        return False


async def get_camera_slugs(hass=None) -> list[str]:
    """Return list of camera slugs currently in Frigate config."""
    base = _frigate_base(hass)
    try:
        cfg = await _fetch_raw_config(base)
        return list(cfg.get("cameras", {}).keys())
    except Exception:
        return []
