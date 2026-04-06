#!/usr/bin/env python3
"""
Configuration centralisée du bot Dofus 3.
Toutes les constantes partagées entre les modules.
"""

from pathlib import Path

# ──────────────────────────────────────────
# JOUEUR
# ──────────────────────────────────────────

PLAYER_ID = 875844665634

# ──────────────────────────────────────────
# RÉSEAU
# ──────────────────────────────────────────

IFACE = "en0"
SERVER_PORT = "5555"

# ──────────────────────────────────────────
# GRILLE ISOMÉTRIQUE (calibration)
# ──────────────────────────────────────────

MAP_WIDTH = 14
CELL_W = 85        # px par colonne
ROW_H = 19.5       # px par rangée
GRID_OX = 507.5    # origine X calibrée
GRID_OY = 144.5    # origine Y calibrée

# ──────────────────────────────────────────
# UI / NAVIGATION
# ──────────────────────────────────────────

NAV_CLICK = {
    'top':    (1058, 114),
    'right':  (1678, 492),
    'bottom': (998, 973),
    'left':   (476, 476),
}

NAV_TIMEOUT = 12.0

# Offsets de map_id par direction
MAP_OFFSET = {'top': -2, 'bottom': +2, 'right': -1024, 'left': +1024}

# Delta de coordonnées monde par direction
COORD_DELTA = {'right': (1, 0), 'left': (-1, 0), 'bottom': (0, 1), 'top': (0, -1)}

# Détours de navigation quand une direction est bloquée
NAV_DETOUR = {
    'left':   [['top', 'left', 'bottom'], ['bottom', 'left', 'top']],
    'right':  [['top', 'right', 'bottom'], ['bottom', 'right', 'top']],
    'top':    [['left', 'top', 'right'], ['right', 'top', 'left']],
    'bottom': [['left', 'bottom', 'right'], ['right', 'bottom', 'left']],
}

# Bouton fin de tour (combat)
END_TURN_X, END_TURN_Y = 1452, 1054

# Bouton "Prêt" (début combat)
COMBAT_READY_X, COMBAT_READY_Y = 1461, 1055

# ──────────────────────────────────────────
# COMBAT
# ──────────────────────────────────────────

SPELL_ROTATION = [
    {"key": "4", "name": "Shovel Kiss", "pa": 3, "min_range": 1, "range": 10, "max_per_target": 2},
]
PA_DEFAULT = 10
PM_DEFAULT = 4

DELAY_SPELL_SELECT = 0.3
DELAY_SPELL_CAST = 0.5
DELAY_TURN_WAIT = 2.0
DELAY_END_TURN = 0.3

# ──────────────────────────────────────────
# FARMING
# ──────────────────────────────────────────

DELAY_CLICK = 0.4
DELAY_HARVEST_TIMEOUT = 15
DELAY_MAP_LOAD = 3
DELAY_AFTER_HARVEST = 0.5
DELAY_NAV = 0.5
DELAY_INTERACT_TIMEOUT = 4
DELAY_ISU_WAIT = 3

# Noms des objets connus
OBJECT_NAMES = {45: 'Blé', 68: 'Orge'}

# Objets céréales confirmés
CEREAL_OBJECTS = {45}  # 45=blé (68=orge, 124=houblon désactivés)

# ──────────────────────────────────────────
# FICHIERS
# ──────────────────────────────────────────

BASE_DIR = Path(__file__).parent
KNOWLEDGE_FILE = BASE_DIR / "discoveries.json"
