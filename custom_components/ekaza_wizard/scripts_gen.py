"""Generate and write PTZ scripts for LocalTuya cameras."""
from pathlib import Path

import yaml

from .models import CameraInfo

_PTZ: list[tuple] = [
    ("ptz_up",      "Cima",         119, "0",  116),
    ("ptz_up_b",    "Cima (B)",     119, "1",  116),
    ("ptz_right",   "Direita",      119, "2",  116),
    ("ptz_left",    "Esquerda",     119, "3",  116),
    ("ptz_down",    "Baixo",        119, "4",  116),
    ("ptz_down_b",  "Baixo (B)",    119, "5",  116),
    ("ptz_left_b",  "Esquerda (B)", 119, "6",  116),
    ("ptz_right_b", "Direita (B)",  119, "7",  116),
    ("ptz_home",    "Recalibrar",   132, True,  None),
    ("zoom_in",     "Zoom In",      163, "1",  164),
    ("zoom_out",    "Zoom Out",     163, "0",  164),
]


def generate_scripts(cam: CameraInfo) -> dict:
    scripts = {}
    for suffix, alias, move_dp, move_val, stop_dp in _PTZ:
        key = f"{cam.slug}_{suffix}"
        sequence = [{"service": "localtuya.set_dp",
                     "data": {"device_id": cam.device_id, "dp": move_dp, "value": move_val}}]
        if stop_dp:
            sequence += [
                {"delay": "00:00:00.25"},
                {"service": "localtuya.set_dp",
                 "data": {"device_id": cam.device_id, "dp": stop_dp, "value": True}},
            ]
        scripts[key] = {"alias": f"{cam.name} PTZ — {alias}", "sequence": sequence}
    return scripts


def write_scripts(cam: CameraInfo) -> tuple[bool, str]:
    path = Path("/config/scripts") / f"{cam.slug}_ptz.yaml"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(generate_scripts(cam), allow_unicode=True, sort_keys=False))
        return True, str(path)
    except Exception as e:
        return False, str(e)
