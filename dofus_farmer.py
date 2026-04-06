#!/usr/bin/env python3
"""
Dofus 3 — Bot Farmer (recolte automatique)
Navigue entre les maps et recolte les cereales detectees.
Utilise map_view comme backend TCP + dashboard central.

Usage:
  python3 dofus_farmer.py                    # Auto-detecte et recolte
  python3 dofus_farmer.py --route rrbblltt   # Suit une route en boucle
  python3 dofus_farmer.py --learn            # Mode apprentissage
"""

import sys, os, json, time, threading, argparse, signal, functools, tempfile, random
from datetime import datetime

from dofus_config import (
    PLAYER_ID, MAP_WIDTH, CELL_W, ROW_H, GRID_OX, GRID_OY,
    NAV_CLICK, NAV_TIMEOUT, NAV_DETOUR, KNOWLEDGE_FILE, OBJECT_NAMES, CEREAL_OBJECTS,
    COORD_DELTA,
    DELAY_HARVEST_TIMEOUT, DELAY_MAP_LOAD, DELAY_AFTER_HARVEST,
    DELAY_NAV, DELAY_INTERACT_TIMEOUT,
)
from dofus_proto import parse_protobuf, gv, gb, detect_server_ip
from combat import CombatEngine
import map_view

# Forcer le flush stdout pour voir la sortie en temps reel
print = functools.partial(print, flush=True)

# Grille
MAP_ROWS = 40

# Offset de clic pour les ressources (perspective isometrique)
RESOURCE_Y_OFFSET_TOP = 30
RESOURCE_Y_OFFSET_BOTTOM = 20

# Retry harvest
MAX_HARVEST_RETRIES = 5
RANDOM_CLICK_OFFSET_PX = 15
DELAY_RANDOM_MOVE = 2.0

# Zone safe pour clic aleatoire (eviter les bords = changement de map)
SAFE_ROW_MIN = 10
SAFE_ROW_MAX = 30
SAFE_COL_MIN = 3
SAFE_COL_MAX = 10

# Limites ecran pour empecher les clics de recolte sur les bords de changement de map
HARVEST_X_MIN = 507   # left
HARVEST_X_MAX = 1654  # right
HARVEST_Y_MIN = 117   # top
HARVEST_Y_MAX = 935   # bottom


# ──────────────────────────────────────────
# GRILLE -> ECRAN
# ──────────────────────────────────────────

def cell_to_screen(cell_id, resource=False):
    """Convertit cell_id -> coordonnees ecran."""
    row = cell_id // MAP_WIDTH
    col = cell_id % MAP_WIDTH
    x = GRID_OX + col * CELL_W
    y = GRID_OY + row * ROW_H
    if resource:
        t = min(row / MAP_ROWS, 1.0)
        y_offset = RESOURCE_Y_OFFSET_TOP + (RESOURCE_Y_OFFSET_BOTTOM - RESOURCE_Y_OFFSET_TOP) * t
        y -= y_offset
    return int(round(x)), int(round(y))


def click(x, y):
    """Clic gauche via cliclick."""
    os.system(f"cliclick c:{x},{y}")


def _click_random_safe():
    """Clic aleatoire dans la zone safe pour deplacer le perso sans changer de map.
    Evite les cellules occupees par des mobs."""
    mob_cells = {mg.get('cell') for mg in map_view.mob_groups.values() if mg.get('cell') is not None}
    for _ in range(20):
        row = random.randint(SAFE_ROW_MIN, SAFE_ROW_MAX)
        col = random.randint(SAFE_COL_MIN, SAFE_COL_MAX)
        safe_cell = row * MAP_WIDTH + col
        if safe_cell not in mob_cells:
            break
    x, y = cell_to_screen(safe_cell)
    map_view.log_action(f"Deplacement aleatoire -> cell {safe_cell} (r{row},c{col})")
    click(x, y)


# ──────────────────────────────────────────
# ETAT DU FARMER (thread-safe)
# ──────────────────────────────────────────

_knowledge_lock = threading.Lock()


class FarmerState:
    def __init__(self):
        self._lock = threading.Lock()

        # Map
        self.current_map = None
        self.map_ready = threading.Event()
        self.map_ready.set()

        # Recolte tracking (donnees de ressources dans map_view)
        self._harvested = set()

        # Recolte
        self.harvesting = False
        self.harvest_done = threading.Event()
        self.interact_started = threading.Event()  # itk recu
        self.harvest_target = None
        self.isu_received = threading.Event()
        self.map_changed = threading.Event()

        # Combat (aggro)
        self.in_combat = False
        self.combat_done = threading.Event()
        self.combat_done.set()

        # Types de ressources appris
        self.learned_types = {}
        self.cereal_objects = set(CEREAL_OBJECTS)

        # Position monde
        self.coords = None

        # Stats
        self.harvests_done = 0
        self.total_xp = 0
        self.total_kamas = 0

        self._load_knowledge()

    def _load_knowledge(self):
        if KNOWLEDGE_FILE.exists():
            try:
                with open(KNOWLEDGE_FILE) as f:
                    data = json.load(f)
                for k, v in data.get('monsters', {}).items():
                    if k.startswith('harvest_type_') and 'raw_fields' in v:
                        rf = v['raw_fields']
                        elem_type = rf.get('elem_type')
                        obj_id = rf.get('object_id')
                        if elem_type is not None and obj_id is not None:
                            self.learned_types[elem_type] = obj_id
            except (json.JSONDecodeError, KeyError):
                pass

    def get_harvestable_interactives(self):
        """Retourne les cereales disponibles (non coupees) sur la map courante."""
        with self._lock:
            harvested = set(self._harvested)
        result = []
        for iid, info in map_view.all_resources.items():
            obj_id = info.get('object_id')
            cell = info.get('cell')
            up = info.get('up', False)
            if cell is not None and up and obj_id in self.cereal_objects and iid not in harvested:
                result.append((iid, cell, obj_id))
        return result

    # --- Gestion de map ---

    def clear_for_new_map(self, map_id):
        with self._lock:
            self.current_map = map_id
            self._harvested.clear()
            self.harvesting = False
            self.harvest_target = None
        self.map_changed.set()
        self.interact_started.set()
        self.harvest_done.set()
        self.isu_received.set()
        self.map_ready.clear()

    def mark_harvested(self, interactive_id):
        with self._lock:
            self._harvested.add(interactive_id)

    def is_harvested(self, interactive_id):
        with self._lock:
            return interactive_id in self._harvested

    # --- Combat (aggro) ---

    def enter_combat(self):
        with self._lock:
            self.in_combat = True
            self.combat_done.clear()
        map_view.log_action("Combat detecte ! Farming en pause.", "warn")

    def leave_combat(self):
        with self._lock:
            self.in_combat = False
            self.combat_done.set()
        map_view.log_action("Combat termine. Reprise du farming.", "ok")

    def wait_combat_over(self):
        if self.in_combat:
            map_view.log_action("Attente fin de combat...")
            self.combat_done.wait()


state = FarmerState()


def _on_combat_end_farmer():
    state.leave_combat()
    state.harvest_done.set()


_combat_engine = CombatEngine(
    on_combat_start=state.enter_combat,
    on_combat_end=_on_combat_end_farmer,
    log_fn=map_view.log_action,
)


# ──────────────────────────────────────────
# CALLBACK MAP_VIEW (reactions farmer)
# ──────────────────────────────────────────

def _on_message(msg_type, value):
    if msg_type == 'irj':
        _on_irj()
    elif msg_type == 'kta':
        _on_kta()
    elif msg_type == 'isu':
        _on_isu()
    elif msg_type == 'idq':
        _on_idq(value)
    else:
        _combat_engine.handle_message(msg_type, value)
        handler = _FARMER_HANDLERS.get(msg_type)
        if handler:
            handler(value)


def _on_irj():
    new_map = map_view.current_map
    if new_map and new_map != state.current_map:
        state.clear_for_new_map(new_map)
        map_view.log_action(f"Changement de map -> {new_map}")


def _on_kta():
    state.current_map = map_view.current_map
    state.map_ready.set()
    map_view.log_action(f"Map chargee : {state.current_map}", "ok")


def _on_isu():
    map_id = map_view.current_map
    if map_id:
        if not state.current_map:
            state.current_map = map_id
        state.map_ready.set()
    elif not state.current_map:
        return

    global _isu_timer
    harvestable = state.get_harvestable_interactives()
    n_res = len(map_view.all_resources)
    map_view.log_action(
        f"ISU map {state.current_map} : {n_res} bles, {len(harvestable)} a recolter",
        "ok" if harvestable else "info",
    )

    # Retarder le signal pour laisser les IDQ de correction arriver
    if _isu_timer:
        _isu_timer.cancel()
    _isu_timer = threading.Timer(1.5, state.isu_received.set)
    _isu_timer.daemon = True
    _isu_timer.start()


def _on_idq(value):
    f = parse_protobuf(value)
    f1_bytes = gb(f, 1)
    if not f1_bytes:
        return
    f1 = parse_protobuf(f1_bytes)
    interactive_id = gv(f1, 1)
    sub_state = gv(f1, 5)
    if interactive_id and sub_state == 0:
        state.mark_harvested(interactive_id)


# ──────────────────────────────────────────
# HANDLERS FARMER (messages non-map)
# ──────────────────────────────────────────

def handle_itk(value):
    if state.harvesting:
        state.interact_started.set()


def handle_idr(value):
    if state.harvesting:
        state.harvesting = False
        state.harvest_done.set()


def handle_ibi(value):
    state.harvest_done.set()


def handle_idk(value):
    f = parse_protobuf(value)
    obj_id = gv(f, 1)
    qty = gv(f, 3)
    player_id = gv(f, 4)
    interactive_id = gv(f, 5)

    if interactive_id and map_view.all_resources.get(interactive_id):
        state.mark_harvested(interactive_id)

    if interactive_id and obj_id:
        info = map_view.all_resources.get(interactive_id, {})
        elem_type = info.get('type')
        if elem_type is not None and elem_type not in state.learned_types:
            state.learned_types[elem_type] = obj_id
            is_cereal = obj_id in state.cereal_objects
            tag = "CEREALE" if is_cereal else "autre"
            map_view.log_action(f"Appris : type {elem_type} -> objet {obj_id} ({tag})", "ok")
            _save_learned_type(elem_type, obj_id)

    is_me = player_id == PLAYER_ID
    name = OBJECT_NAMES.get(obj_id, f'?{obj_id}')
    if is_me:
        map_view.log_action(f"Recolte {name} x{qty}", "ok")
        state.harvests_done += 1


def handle_ibl(value):
    f = parse_protobuf(value)
    xp = gv(f, 2) or 0
    kamas = gv(f, 3) or 0
    state.total_xp += xp
    state.total_kamas += kamas
    map_view.log_action(f"+{xp} XP | +{kamas} K (total: {state.total_xp} XP, {state.total_kamas} K)")


def _save_learned_type(elem_type, obj_id):
    with _knowledge_lock:
        try:
            data = {}
            if KNOWLEDGE_FILE.exists():
                with open(KNOWLEDGE_FILE) as f:
                    data = json.load(f)
            if 'monsters' not in data:
                data['monsters'] = {}
            key = f"harvest_type_{elem_type}"
            data['monsters'][key] = {
                'species': obj_id,
                'raw_fields': {'elem_type': elem_type, 'object_id': obj_id},
                'first_seen': datetime.now().isoformat(),
            }
            fd, tmp_path = tempfile.mkstemp(dir=KNOWLEDGE_FILE.parent, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as tmp_f:
                    json.dump(data, tmp_f, indent=2, default=str)
                os.replace(tmp_path, KNOWLEDGE_FILE)
            except Exception:
                os.unlink(tmp_path)
                raise
        except Exception as e:
            map_view.log_action(f"Sauvegarde echouee : {e}", "err")


_isu_timer = None


_FARMER_HANDLERS = {
    'itk': handle_itk,
    'idk': handle_idk,
    'idr': handle_idr,
    'ibi': handle_ibi,
    'ibl': handle_ibl,
}


# ──────────────────────────────────────────
# ACTIONS
# ──────────────────────────────────────────

def _end_harvest(mark_iid=None):
    """Reset l'etat de recolte. Marque l'iid comme recolte si fourni."""
    if mark_iid:
        state.mark_harvested(mark_iid)
    state.harvesting = False
    state.harvest_target = None


def harvest_resource(interactive_id, cell):
    """Recolte une ressource avec retry intelligent :
    1. Clic sur la ressource -> attend itk
    2. Si pas de itk : clic aleatoire pour bouger, puis re-clic avec offset
    3. Repete jusqu'a itk ou max retries
    4. Une fois itk recu : attend idr/ibi (recolte terminee)
    """
    state.wait_combat_over()

    if state.is_harvested(interactive_id):
        return False

    sx, sy = cell_to_screen(cell, resource=True)
    info = map_view.all_resources.get(interactive_id, {})
    if not info:
        return False

    name = OBJECT_NAMES.get(info.get('object_id', 0), f'?{info.get("object_id")}')
    harvest_map = state.current_map
    state.harvesting = True
    state.harvest_target = interactive_id
    state.harvest_done.clear()
    state.map_changed.clear()

    def _map_changed():
        return state.current_map != harvest_map

    # -- Etape 1 : obtenir le itk (avec retries) --
    got_itk = False
    for attempt in range(MAX_HARVEST_RETRIES):
        if attempt == 0:
            cx, cy = sx, sy
        else:
            cx = sx + random.randint(-RANDOM_CLICK_OFFSET_PX, RANDOM_CLICK_OFFSET_PX)
            cy = sy + random.randint(-RANDOM_CLICK_OFFSET_PX, RANDOM_CLICK_OFFSET_PX)

        # Clamper dans la zone safe (eviter les bords de changement de map)
        cx = max(HARVEST_X_MIN, min(HARVEST_X_MAX, cx))
        cy = max(HARVEST_Y_MIN, min(HARVEST_Y_MAX, cy))

        map_view.log_action(
            f"Clic {name} cell={cell} ({cx},{cy})"
            + (f" [tentative {attempt + 1}]" if attempt > 0 else ""),
        )

        state.interact_started.clear()
        click(cx, cy)
        got_itk = state.interact_started.wait(timeout=DELAY_INTERACT_TIMEOUT)

        if _map_changed():
            map_view.log_action(f"Map changee, abandon {name}", "warn")
            _end_harvest()
            return False

        if got_itk:
            break

        # Ressource coupee entre-temps par un autre joueur
        if not map_view.all_resources.get(interactive_id, {}).get('up', False):
            map_view.log_action(f"{name} coupee par un autre joueur", "warn")
            _end_harvest(interactive_id)
            return False

        # Bouger le perso avant le prochain essai
        if attempt < MAX_HARVEST_RETRIES - 1:
            map_view.log_action("Pas de itk, deplacement pour debloquer...", "warn")
            _click_random_safe()
            time.sleep(DELAY_RANDOM_MOVE)
            if _map_changed():
                map_view.log_action("Map changee apres deplacement", "warn")
                _end_harvest()
                return False

    if not got_itk:
        map_view.log_action(f"{name} inatteignable apres {MAX_HARVEST_RETRIES} essais", "err")
        _end_harvest(interactive_id)
        return False

    # -- Etape 2 : itk recu -> attente fin de recolte --
    map_view.log_action(f"Recolte {name} en cours...")
    ok = state.harvest_done.wait(timeout=DELAY_HARVEST_TIMEOUT)

    if _map_changed():
        map_view.log_action("Map changee pendant recolte", "warn")
        _end_harvest()
        return False

    if ok:
        map_view.log_action(f"{name} recolte terminee", "ok")
    else:
        map_view.log_action(f"{name} timeout recolte", "err")
        state.mark_harvested(interactive_id)

    _end_harvest()
    time.sleep(DELAY_AFTER_HARVEST)
    return ok


def harvest_all_on_map():
    """Recolte toutes les cereales sur la map courante."""
    state.wait_combat_over()

    current_map = state.current_map
    harvestable = state.get_harvestable_interactives()
    if not harvestable:
        map_view.log_action("Aucune cereale sur cette map")
        return 0

    map_view.log_action(f"{len(harvestable)} cereale(s) a recolter")
    count = 0
    for iid, cell, etype in harvestable:
        if state.in_combat:
            map_view.log_action("Combat en cours, arret recolte", "warn")
            break
        if state.current_map != current_map:
            map_view.log_action("Map changee, arret recolte", "warn")
            break
        harvest_resource(iid, cell)
        count += 1
    return count


def _nav_step(direction):
    """Tente un seul deplacement dans une direction. Retourne True si succes."""
    if direction not in NAV_CLICK:
        return False

    state.wait_combat_over()
    state.map_ready.clear()
    state.isu_received.clear()

    pos = f" depuis {state.coords}" if state.coords else ""
    map_view.log_action(f"Navigation -> {direction}{pos}")
    x, y = NAV_CLICK[direction]
    click(x, y)
    time.sleep(0.3)
    click(x, y)

    ok = state.map_ready.wait(timeout=NAV_TIMEOUT)
    if ok:
        if state.coords:
            dx, dy = COORD_DELTA[direction]
            state.coords = (state.coords[0] + dx, state.coords[1] + dy)
        state.isu_received.wait(timeout=DELAY_MAP_LOAD + 2)
        time.sleep(1.5)
        pos = f" {state.coords}" if state.coords else ""
        map_view.log_action(f"Arrive sur map {state.current_map}{pos}", "ok")
        return True
    else:
        map_view.log_action(f"Navigation {direction} bloquee (timeout)", "err")
        state.map_ready.set()
        return False


def nav_move(direction):
    """Deplace vers une direction avec detours si bloque. Retourne True si succes."""
    if _nav_step(direction):
        return True

    # Essayer les detours
    opposite = {'top': 'bottom', 'bottom': 'top', 'left': 'right', 'right': 'left'}
    for detour_path in NAV_DETOUR.get(direction, []):
        map_view.log_action(f"Detour {' -> '.join(detour_path)} pour contourner", "warn")
        success = True
        for i, step in enumerate(detour_path):
            if not _nav_step(step):
                # Revenir en arriere
                for j in range(i - 1, -1, -1):
                    _nav_step(opposite[detour_path[j]])
                success = False
                break
            time.sleep(0.3)
        if success:
            return True

    return False


# ──────────────────────────────────────────
# MODES
# ──────────────────────────────────────────

def wait_first_map():
    map_view.log_action("Attente de la map initiale...")
    while not state.current_map:
        time.sleep(0.5)
    map_view.log_action(f"Map detectee : {state.current_map}", "ok")
    state.isu_received.wait(timeout=10)
    return True


def mode_farm_route(route):
    shortcuts = {'t': 'top', 'r': 'right', 'b': 'bottom', 'l': 'left'}

    map_view.log_action(f"Demarrage route : {route}", "ok")

    loop = 0
    while True:
        loop += 1
        map_view.log_action(f"--- Boucle #{loop} ---")

        aborted = False
        for i, c in enumerate(route):
            if c not in shortcuts:
                continue
            direction = shortcuts[c]
            harvest_all_on_map()
            if not nav_move(direction):
                map_view.log_action(
                    f"Navigation {direction} impossible meme avec detours, "
                    f"relance de la boucle", "err"
                )
                aborted = True
                break

        if aborted:
            time.sleep(DELAY_NAV)
            continue

        harvest_all_on_map()

        map_view.log_action(
            f"Boucle #{loop} terminee | {state.harvests_done} recoltes | "
            f"{state.total_xp} XP | {state.total_kamas} K",
            "ok",
        )
        time.sleep(DELAY_NAV)


def mode_farm_stay():
    wait_first_map()
    map_view.log_action("Mode stationnaire demarre", "ok")

    while True:
        state.wait_combat_over()

        if state.get_harvestable_interactives():
            harvest_all_on_map()
            state.isu_received.clear()
        else:
            state.isu_received.clear()
            map_view.log_action("Attente de nouvelles ressources...")
            state.isu_received.wait(timeout=30)



def _zone_to_route(coord1, coord2):
    """Génère une route serpentine à partir de 2 coins en coordonnées monde (x,y).
    Le serpentin commence toujours depuis le coin haut-gauche (min_x, min_y).
    Retourne (route_str, width, height)."""
    x1, y1 = coord1
    x2, y2 = coord2
    min_x, max_x = min(x1, x2), max(x1, x2)
    min_y, max_y = min(y1, y2), max(y1, y2)
    width = max_x - min_x + 1
    height = max_y - min_y + 1

    # Construire le chemin serpentin en coordonnées monde
    path = []
    for row_idx in range(height):
        y = min_y + row_idx
        if row_idx % 2 == 0:
            xs = range(min_x, max_x + 1)
        else:
            xs = range(max_x, min_x - 1, -1)
        for x in xs:
            path.append((x, y))

    # Convertir en directions (right/left = x, bottom/top = y)
    route = []
    for (ax, ay), (bx, by) in zip(path, path[1:]):
        dx, dy = bx - ax, by - ay
        if dx > 0: route.extend(['r'] * dx)
        elif dx < 0: route.extend(['l'] * abs(dx))
        if dy > 0: route.extend(['b'] * dy)
        elif dy < 0: route.extend(['t'] * abs(dy))

    # Retour au départ en longeant le bord (vertical puis horizontal)
    last_x, last_y = path[-1]
    first_x, first_y = path[0]
    dy = first_y - last_y
    dx = first_x - last_x
    if dy > 0: route.extend(['b'] * dy)
    elif dy < 0: route.extend(['t'] * abs(dy))
    if dx > 0: route.extend(['r'] * dx)
    elif dx < 0: route.extend(['l'] * abs(dx))

    return ''.join(route), width, height



# ──────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Dofus Farmer Bot')
    parser.add_argument('--route', default=None,
                        help='Route de navigation (t/r/b/l), ex: --route rrbblltt')
    parser.add_argument('--zone', default=None,
                        help='Zone en coordonnees monde (2 coins), ex: --zone "5,-22 7,-20"')
    parser.add_argument('--pos', default=None,
                        help='Position de depart en coordonnees monde, ex: --pos 5,-22')
    parser.add_argument('--ip', default=None, help='IP serveur')
    args = parser.parse_args()

    if args.pos:
        px, py = args.pos.split(',')
        state.coords = (int(px), int(py))

    server_ip = args.ip or detect_server_ip()
    if not server_ip:
        print("[FARM] Dofus non detecte. Lance le jeu d'abord.")
        print("[FARM] Ou utilise --ip X.X.X.X")
        return

    # Demarrer map_view comme backend TCP + enregistrer le callback farmer
    map_view.register_callback(_on_message)
    capture = map_view.start(server_ip)
    time.sleep(1)

    map_view.log_action(f"Serveur : {server_ip}", "ok")
    map_view.log_action(f"Cereales : {state.cereal_objects}")

    def cleanup(sig=None, frame=None):
        capture.stop()
        map_view.log_action(
            f"SESSION TERMINEE | {state.harvests_done} recoltes | "
            f"{state.total_xp} XP | {state.total_kamas} K",
            "ok",
        )
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)

    try:
        if args.zone:
            if not state.coords:
                print("[FARM] --pos requis avec --zone : --pos 5,-22 --zone \"5,-22 7,-20\"")
                return
            parts = args.zone.split()
            if len(parts) < 2:
                print("[FARM] --zone necessite 2 points : --zone \"5,-22 7,-20\"")
                return
            c1 = tuple(int(x) for x in parts[0].split(','))
            c2 = tuple(int(x) for x in parts[1].split(','))
            top_left = (min(c1[0], c2[0]), min(c1[1], c2[1]))
            route, w, h = _zone_to_route(c1, c2)
            map_view.log_action(f"Zone ({c1[0]},{c1[1]}) -> ({c2[0]},{c2[1]}) [{w}x{h} = {w*h} maps]", "ok")
            map_view.log_action(f"Point de depart du serpentin : {top_left}")

            # Navigation vers le coin haut-gauche avant de lancer le serpentin
            if state.coords != top_left:
                map_view.log_action(
                    f"Navigation vers le point de depart ({top_left[0]},{top_left[1]})...",
                    "info",
                )
                wait_first_map()

                MAX_PREFARM_RETRIES = 5

                # D'abord aller horizontalement (gauche/droite)
                retries = 0
                while state.coords[0] != top_left[0]:
                    harvest_all_on_map()
                    direction = 'left' if state.coords[0] > top_left[0] else 'right'
                    if nav_move(direction):
                        retries = 0
                    else:
                        retries += 1
                        if retries >= MAX_PREFARM_RETRIES:
                            map_view.log_action(
                                f"Pre-farm: bloque apres {MAX_PREFARM_RETRIES} echecs horizontaux, "
                                f"lancement du serpentin depuis {state.coords}", "err"
                            )
                            break

                # Puis aller verticalement (haut/bas)
                retries = 0
                while state.coords[1] != top_left[1]:
                    harvest_all_on_map()
                    direction = 'top' if state.coords[1] > top_left[1] else 'bottom'
                    if nav_move(direction):
                        retries = 0
                    else:
                        retries += 1
                        if retries >= MAX_PREFARM_RETRIES:
                            map_view.log_action(
                                f"Pre-farm: bloque apres {MAX_PREFARM_RETRIES} echecs verticaux, "
                                f"lancement du serpentin depuis {state.coords}", "err"
                            )
                            break

                map_view.log_action(
                    f"Arrive au point de depart ({top_left[0]},{top_left[1]})", "ok"
                )
            else:
                wait_first_map()
                map_view.log_action("Deja au point de depart, lancement du serpentin", "ok")

            map_view.log_action(f"Route generee : {route}")
            mode_farm_route(route)
        elif args.route:
            mode_farm_route(args.route)
        else:
            mode_farm_stay()
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    main()
