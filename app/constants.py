EKAZA_PRODUCT_IDS: set[str] = {
    # Known eKaza EKRW-T5293 product IDs — expand as new hardware is confirmed
    "keHRKNhbN2LNnTEP",
}

FRIGATE_SLUG_DEFAULT = "ccab4aaf_frigate-fa"

EKAZA_DPS_MANUAL = "101,103,104,105,106,116,119,124,132,134,138,139,150,160,161,163,164,190,199"

# Entity definitions for eKaza EKRW-T5293 (fixed model, all cameras share these DPs)
EKAZA_ENTITIES: list[dict] = [
    # ── Switches ──────────────────────────────────────────────────
    {"dp": 101, "platform": "switch", "friendly_name": "LED Indicador",           "is_passive": False},
    {"dp": 103, "platform": "switch", "friendly_name": "Imagem Espelhada",        "is_passive": False},
    {"dp": 104, "platform": "switch", "friendly_name": "OSD",                     "is_passive": False},
    {"dp": 105, "platform": "switch", "friendly_name": "Modo Privacidade",        "is_passive": False},
    {"dp": 116, "platform": "switch", "friendly_name": "PTZ Parar",               "is_passive": True},
    {"dp": 132, "platform": "switch", "friendly_name": "PTZ Home",                "is_passive": True},
    {"dp": 134, "platform": "switch", "friendly_name": "Detecção de Movimento",   "is_passive": False},
    {"dp": 138, "platform": "switch", "friendly_name": "Luz de Iluminação",       "is_passive": False},
    {"dp": 139, "platform": "switch", "friendly_name": "Detecção de Áudio",       "is_passive": False},
    {"dp": 150, "platform": "switch", "friendly_name": "Gravação SD",             "is_passive": False},
    {"dp": 161, "platform": "switch", "friendly_name": "Rastreamento Automático", "is_passive": False},
    {"dp": 164, "platform": "switch", "friendly_name": "Zoom Parar",              "is_passive": True},
    # ── Selects ───────────────────────────────────────────────────
    {"dp": 106, "platform": "select", "friendly_name": "Sensibilidade Movimento",
     "select_options": {"0": "Baixa", "1": "Média", "2": "Alta"}},
    {"dp": 119, "platform": "select", "friendly_name": "Controle PTZ",
     "select_options": {"0": "Cima", "1": "Cima B", "2": "Direita", "3": "Esquerda",
                        "4": "Baixo", "5": "Baixo B", "6": "Esquerda B", "7": "Direita B"}},
    {"dp": 124, "platform": "select", "friendly_name": "Visão Noturna",
     "select_options": {"auto": "Auto", "ir_mode": "IR", "color_mode": "Cor"}},
    {"dp": 163, "platform": "select", "friendly_name": "Zoom",
     "select_options": {"0": "Zoom Out", "1": "Zoom In"}},
    {"dp": 190, "platform": "select", "friendly_name": "Ir para Preset",
     "select_options": {"1": "Preset 1", "2": "Preset 2", "3": "Preset 3", "4": "Preset 4"}},
    {"dp": 199, "platform": "select", "friendly_name": "Salvar Preset",
     "select_options": {"1": "Preset 1", "2": "Preset 2", "3": "Preset 3", "4": "Preset 4"}},
    # ── Numbers ───────────────────────────────────────────────────
    {"dp": 160, "platform": "number", "friendly_name": "Volume",
     "min_value": 1.0, "max_value": 10.0, "step_size": 1.0},
]

# PTZ script definitions: (script_suffix, alias_label, move_dp, move_val, stop_dp)
# stop_dp=None means no stop command (e.g., home/calibrate)
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
