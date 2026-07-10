# product_id is NOT a reliable primary identifier — the same physical model
# can ship with different product_ids between production batches.
# Camera detection uses category ("sp"/"ipc") + DP capability heuristics instead.
# This set is kept only as a supplementary hint for bundled schema lookup.
EKAZA_PRODUCT_IDS: set[str] = {
    "wg808xnwx1zeavq2",  # EKRW-T5293 (dome PTZ) — confirmed, batch 1
}

EKAZA_DPS_MANUAL = (
    "101,103,104,105,106,107,110,111,116,119,120,124,"
    "127,132,134,138,139,140,150,151,160,161,162,163,"
    "164,168,170,178,188,190,198,199,231,236,237,239"
)

# Full entity map for eKaza EKRW-T5293 — sourced from Tuya cloud shadow properties
EKAZA_ENTITIES: list[dict] = [
    # ── Switches ──────────────────────────────────────────────────
    {"dp": 101, "platform": "switch", "friendly_name": "LED Indicador",           "is_passive": False},
    {"dp": 103, "platform": "switch", "friendly_name": "Imagem Espelhada",        "is_passive": False},
    {"dp": 104, "platform": "switch", "friendly_name": "OSD",                     "is_passive": False},
    {"dp": 105, "platform": "switch", "friendly_name": "Modo Privacidade",        "is_passive": False},
    {"dp": 107, "platform": "switch", "friendly_name": "WDR (Contraste)",         "is_passive": False},
    {"dp": 111, "platform": "switch", "friendly_name": "Formatar SD",             "is_passive": False},
    {"dp": 116, "platform": "switch", "friendly_name": "PTZ Parar",               "is_passive": True},
    {"dp": 120, "platform": "switch", "friendly_name": "Sirene Automática",       "is_passive": False},
    {"dp": 132, "platform": "switch", "friendly_name": "PTZ Home",                "is_passive": True},
    {"dp": 134, "platform": "switch", "friendly_name": "Detecção de Movimento",   "is_passive": False},
    {"dp": 138, "platform": "switch", "friendly_name": "Luz de Iluminação",       "is_passive": False},
    {"dp": 139, "platform": "switch", "friendly_name": "Detecção de Áudio",       "is_passive": False},
    {"dp": 150, "platform": "switch", "friendly_name": "Gravação SD",             "is_passive": False},
    {"dp": 161, "platform": "switch", "friendly_name": "Rastreamento Automático", "is_passive": False},
    {"dp": 162, "platform": "switch", "friendly_name": "Reiniciar Câmera",        "is_passive": False},
    {"dp": 164, "platform": "switch", "friendly_name": "Zoom Parar",              "is_passive": True},
    {"dp": 178, "platform": "select", "friendly_name": "Ponto de Memória PTZ",
     "select_options": {"0": "Ponto 1", "1": "Ponto 2", "2": "Ponto 3", "3": "Ponto 4"}},
    {"dp": 168, "platform": "switch", "friendly_name": "Zona de Movimento",       "is_passive": False},
    {"dp": 170, "platform": "switch", "friendly_name": "Filtro Humano",           "is_passive": False},
    {"dp": 198, "platform": "switch", "friendly_name": "Contorno de Objetos",     "is_passive": False},
    {"dp": 236, "platform": "switch", "friendly_name": "Alarme Sonoro",           "is_passive": False},
    {"dp": 237, "platform": "switch", "friendly_name": "ONVIF",                   "is_passive": False},
    # ── Selects ───────────────────────────────────────────────────
    {"dp": 106, "platform": "select", "friendly_name": "Sensibilidade Movimento",
     "select_options": {"0": "Baixa", "1": "Média", "2": "Alta"}},
    {"dp": 119, "platform": "select", "friendly_name": "Controle PTZ",
     "select_options": {"0": "Cima", "1": "Cima B", "2": "Direita", "3": "Esquerda",
                        "4": "Baixo", "5": "Baixo B", "6": "Esquerda B", "7": "Direita B"}},
    {"dp": 124, "platform": "select", "friendly_name": "Visão Noturna",
     "select_options": {"auto": "Auto", "ir_mode": "IR", "color_mode": "Cor"}},
    {"dp": 127, "platform": "select", "friendly_name": "Tipo de Evento",
     "select_options": {"motion": "Movimento", "decibel": "Áudio", "humanoid": "Humano"}},
    {"dp": 140, "platform": "select", "friendly_name": "Sensibilidade de Áudio",
     "select_options": {"0": "Baixa", "1": "Média", "2": "Alta"}},
    {"dp": 151, "platform": "select", "friendly_name": "Modo de Gravação",
     "select_options": {"1": "Evento", "2": "Contínuo"}},
    {"dp": 163, "platform": "select", "friendly_name": "Zoom",
     "select_options": {"0": "Zoom Out", "1": "Zoom In"}},
    {"dp": 188, "platform": "select", "friendly_name": "Anti-Oscilação",
     "select_options": {"0": "Auto", "1": "50 Hz", "2": "60 Hz"}},
    {"dp": 190, "platform": "select", "friendly_name": "Ir para Preset",
     "select_options": {"1": "Preset 1", "2": "Preset 2", "3": "Preset 3", "4": "Preset 4"}},
    {"dp": 199, "platform": "select", "friendly_name": "Salvar Preset",
     "select_options": {"1": "Preset 1", "2": "Preset 2", "3": "Preset 3", "4": "Preset 4"}},
    # ── Numbers ───────────────────────────────────────────────────
    {"dp": 160, "platform": "number", "friendly_name": "Volume",
     "min_value": 1.0, "max_value": 10.0, "step_size": 1.0},
    {"dp": 231, "platform": "number", "friendly_name": "Nível de Zoom",
     "min_value": 0.0, "max_value": 10.0, "step_size": 1.0},
]

# DPs that are read-only or binary payloads — not added as LocalTuya entities
# DP 109: sd_storge (string, ro)       DP 110: sd_status (value, ro)
# DP 115: movement_detect_pic (raw)    DP 117: sd_format_state (value, ro)
# DP 136: doorbell_active (string, ro) DP 141: decibel_upload (string, ro)
# DP 169: motion_area (string, complex JSON)
# DP 185: alarm_message (raw) — MOTION ALARM EVENT — monitored by wizard bridge service
# DP 212: initiative_message (raw)     DP 238: onvif_change_pwd (string, write-only)
# DP 239: onvif_pw_changed (bool, ro)  DP 240: onvif_ip_addr (string, ro)
# DP 241: onvif_iptype_config (enum)

# ONVIF provisioning DPs (used by wizard, not LocalTuya entities)
ONVIF_ENABLE_DP  = 237   # bool → True to enable ONVIF
ONVIF_SET_PWD_DP = 238   # string → JSON {"pwd": "password"} to set RTSP password
MOTION_ALARM_DP  = 185   # raw → fires when camera detects alarm (motion/sound/etc)

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
