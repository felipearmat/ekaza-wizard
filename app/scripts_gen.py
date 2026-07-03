"""Generate HA PTZ scripts YAML for a camera."""
from pathlib import Path

import yaml

from constants import PTZ_SCRIPTS
from models import CameraInfo

_SCRIPTS_DIR = Path("/config/scripts")


def _script_name(cam: CameraInfo, suffix: str) -> str:
    # zoom scripts use cam.slug directly, ptz scripts use cam.slug prefix
    return f"{cam.slug}_{suffix}"


def _build_sequence(device_id: str, move_dp: int, move_val, stop_dp: int | None) -> list:
    seq = [
        {
            "service": "localtuya.set_dp",
            "data": {"device_id": device_id, "dp": move_dp, "value": move_val},
        }
    ]
    if stop_dp is not None:
        seq += [
            {"delay": "00:00:00.250"},
            {
                "service": "localtuya.set_dp",
                "data": {"device_id": device_id, "dp": stop_dp, "value": True},
            },
        ]
    return seq


def generate_scripts(cam: CameraInfo) -> dict:
    """Return dict of script_id → script definition (ready for scripts.yaml)."""
    scripts: dict = {}
    for suffix, label, move_dp, move_val, stop_dp in PTZ_SCRIPTS:
        script_id = _script_name(cam, suffix)
        direction = "PTZ" if suffix.startswith("ptz") else "Zoom"
        scripts[script_id] = {
            "alias": f"{cam.name} {direction} — {label}",
            "sequence": _build_sequence(cam.device_id, move_dp, move_val, stop_dp),
        }
    return scripts


def write_scripts(cam: CameraInfo) -> tuple[bool, str]:
    """Write <slug>_ptz.yaml to /config/scripts/. Returns (ok, detail)."""
    try:
        _SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        target = _SCRIPTS_DIR / f"{cam.slug}_ptz.yaml"
        scripts = generate_scripts(cam)
        with open(target, "w") as f:
            yaml.dump(scripts, f, allow_unicode=True, sort_keys=False)
        return True, str(target)
    except Exception as e:
        return False, str(e)
