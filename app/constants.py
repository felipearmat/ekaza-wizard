# ONVIF provisioning DPs — standard Tuya camera codes, stable across models
ONVIF_ENABLE_DP  = 237   # bool → True to enable ONVIF
ONVIF_SET_PWD_DP = 238   # string → JSON {"pwd": "password"} to set RTSP password

# PTZ script definitions for cameras with ptz_control (DP 119) and ptz_stop (DP 116).
# Tested on EKRW-T5293; other PTZ cameras using the same Tuya DP codes should work.
# Format: (script_suffix, alias_label, move_dp, move_val, stop_dp)
# stop_dp=None means no stop command needed (e.g., home/calibrate triggers internally)
PTZ_SCRIPTS: list[tuple] = [
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
