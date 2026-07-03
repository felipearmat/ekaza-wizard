"""Frigate config management — read, merge new cameras, write back."""
import os
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from models import CameraInfo

_SEARCH_PATHS = [
    "/addon_configs/{slug}/config.yaml",
    "/addon_configs/{slug}/config.yml",
    "/config/addons/{slug}/config.yaml",
]


def find_config(frigate_slug: str) -> Path | None:
    for pattern in _SEARCH_PATHS:
        p = Path(pattern.format(slug=frigate_slug))
        if p.exists():
            return p
    return None


def _read(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _write(path: Path, config: dict) -> None:
    backup = path.with_suffix(f".bak.{datetime.now().strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, backup)
    with open(path, "w") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)


def _camera_block(cam: CameraInfo) -> dict:
    return {
        "ffmpeg": {
            "hwaccel_args": [],
            "inputs": [
                {
                    "path": f"rtsp://127.0.0.1:8554/{cam.slug}",
                    "roles": ["record", "detect"],
                }
            ],
        },
        "detect": {"width": 640, "height": 360, "fps": 5},
        "record": {
            "enabled": True,
            "events": {"retain": {"default": 10}},
        },
    }


def _stream_source(cam: CameraInfo) -> list[str]:
    url = f"rtsp://{cam.rtsp_username}:{cam.rtsp_password}@{cam.ip}:8554/stream0"
    return [f"ffmpeg:{url}#video=copy"]


def merge_cameras(config: dict, cameras: list[CameraInfo]) -> dict:
    config.setdefault("go2rtc", {}).setdefault("streams", {})
    config.setdefault("cameras", {})

    for cam in cameras:
        config["go2rtc"]["streams"][cam.slug] = _stream_source(cam)
        config["cameras"][cam.slug] = _camera_block(cam)

    return config


def apply(frigate_slug: str, cameras: list[CameraInfo]) -> tuple[bool, str]:
    """
    Find the Frigate config, merge cameras in, write back.
    Returns (success, detail_message).
    """
    path = find_config(frigate_slug)
    if path is None:
        snippet = _generate_snippet(cameras)
        return False, f"Config not found. Add manually:\n\n{snippet}"

    try:
        config = _read(path)
        config = merge_cameras(config, cameras)
        _write(path, config)
        return True, str(path)
    except Exception as e:
        return False, str(e)


def _generate_snippet(cameras: list[CameraInfo]) -> str:
    """Fallback: return YAML snippet the user can paste manually."""
    go2rtc_streams = {cam.slug: _stream_source(cam) for cam in cameras}
    camera_blocks = {cam.slug: _camera_block(cam) for cam in cameras}
    snippet = {
        "go2rtc": {"streams": go2rtc_streams},
        "cameras": camera_blocks,
    }
    return yaml.dump(snippet, allow_unicode=True, sort_keys=False)


def get_snippet(cameras: list[CameraInfo]) -> str:
    return _generate_snippet(cameras)
