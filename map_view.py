#!/usr/bin/env python3
"""
map_view.py — Backend TCP + dashboard central.

Standalone : sudo python3 map_view.py
Backend    : import map_view ; map_view.start(ip) ; map_view.register_callback(fn)

Expose :
  all_resources  — {iid: {object_id, cell, up}}
  player_cell    — int | None
  current_map    — int | None
  log_action()   — ajoute une action au log (visible sur le dashboard)
"""

import functools, os, signal, sys, time
from collections import deque
from dofus_config import MAP_WIDTH, PLAYER_ID
from dofus_proto import TcpCapture, detect_server_ip, parse_protobuf, gv, gb, decode_varint_list

print = functools.partial(print, flush=True)

# -- Couleurs ANSI -------------------------------------------------------------
R    = "\033[0m"
BOLD = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
GRAY   = "\033[90m"
BG_GREEN  = "\033[42m"
BG_MAGENTA = "\033[45m"
BG_BLUE = "\033[44m"
WHITE = "\033[97m"

# -- Etat global ---------------------------------------------------------------
current_map = None
all_resources = {}   # iid -> {object_id, cell, up}
mob_groups = {}      # entity_id -> {cell, monsters: [(type, level), ...]}
player_cell = None

# -- Action log ----------------------------------------------------------------
action_log = deque(maxlen=25)
_last_render_t = 0.0
_RENDER_THROTTLE = 0.3  # secondes min entre deux renders

_LOG_ICONS  = {"info": "  ", "ok": "✓ ", "warn": "⚠ ", "err": "✗ "}
_LOG_COLORS = {"info": GRAY, "ok": GREEN, "warn": YELLOW, "err": RED}


def log_action(text, level="info"):
    """Ajoute une ligne au log d'actions. Rafraichit le dashboard (throttle)."""
    global _last_render_t
    ts = time.strftime("%H:%M:%S")
    action_log.append((ts, level, text))
    now = time.monotonic()
    if now - _last_render_t >= _RENDER_THROTTLE:
        _last_render_t = now
        render()


# -- Backend API ---------------------------------------------------------------
_external_callbacks = []
_standalone = True


def register_callback(fn):
    """Enregistre un callback fn(msg_type, value) appele apres chaque message."""
    _external_callbacks.append(fn)


def start(server_ip):
    """Demarre la capture TCP en mode backend. Retourne le TcpCapture."""
    global _standalone
    _standalone = False
    capture = TcpCapture(server_ip, on_message)
    capture.start()
    return capture


# -- Render --------------------------------------------------------------------

def clear():
    os.system("clear")


def render():
    """Affiche la grille + log d'actions."""
    clear()

    if current_map is None:
        print(f"\n  {CYAN}En attente d'un ISU... Change de map.{R}\n")
        _render_log()
        return

    # Index cell -> ressource
    cell_map = {}
    for iid, info in all_resources.items():
        cell = info['cell']
        if cell is not None:
            cell_map.setdefault(cell, []).append(info)

    # Index cell -> mob
    mob_cell_map = {}
    for eid, mg in mob_groups.items():
        cell = mg.get('cell')
        if cell is not None:
            mob_cell_map[cell] = mg

    n_up = sum(1 for info in all_resources.values() if info.get('up'))
    n_down = len(all_resources) - n_up
    col_range = range(MAP_WIDTH)

    # -- En-tete ----------------------------------------------------------------
    print(f"\n  {BOLD}{CYAN}Map {current_map}{R}  ---  {BOLD}{len(all_resources)} bles{R}  "
          f"{BOLD}{len(mob_groups)} mobs{R}")
    print(f"    {GREEN}{n_up} up{R}  |  {RED}{n_down} down{R}")

    # -- Grille -----------------------------------------------------------------
    print()
    print(f"  {GRAY}     " + "".join(f"{c:4d}" for c in col_range) + f"{R}")
    print(f"  {GRAY}     " + "----" * MAP_WIDTH + f"{R}")

    for row in range(40):
        offset = "  " if row % 2 else ""
        line = f"  {GRAY}{row:3d} |{R}{offset}"
        res_on_row = []
        for col in col_range:
            cell = row * MAP_WIDTH + col
            items = cell_map.get(cell)
            mob = mob_cell_map.get(cell)
            if cell == player_cell:
                line += f"  {BG_MAGENTA}{WHITE}{BOLD}P{R} "
                res_on_row.append(f"{cell}[P]")
            elif mob:
                n = len(mob['monsters'])
                line += f"  {BG_BLUE}{WHITE}{BOLD}{n}{R} "
                res_on_row.append(f"{cell}[M]")
            elif not items:
                line += f"  {GRAY}.{R} "
            else:
                info = items[0]
                if info.get('up'):
                    line += f"  {BG_GREEN}{WHITE}{BOLD}B{R} "
                else:
                    line += f"  {RED}{BOLD}b{R} "
                res_on_row.extend(str(cell) for _ in items)
        if res_on_row:
            line += f"  {GRAY}<- cell {', '.join(res_on_row)}{R}"
        print(line)

    # -- Legende ----------------------------------------------------------------
    print()
    print(f"  {GRAY}Legende :  {BG_MAGENTA}{WHITE}{BOLD} P {R} perso   "
          f"{BG_BLUE}{WHITE}{BOLD} N {R} mob(N)   "
          f"{BG_GREEN}{WHITE}{BOLD} B {R} up   {RED}{BOLD}b{R} down   {GRAY}.{R} vide")

    # -- Detail mobs -------------------------------------------------------------
    if mob_groups:
        print(f"\n  {BOLD}Mobs ({len(mob_groups)} groupes) :{R}")
        for eid in sorted(mob_groups, key=lambda x: mob_groups[x].get('cell') or 9999):
            mg = mob_groups[eid]
            cell = mg.get('cell')
            monsters = mg['monsters']
            if cell is not None:
                row, col = cell // MAP_WIDTH, cell % MAP_WIDTH
                print(f"    {RED}{len(monsters)}x{R}  cell={cell:4d}  (r{row:2d}, c{col:2d})")
            else:
                print(f"    {RED}{len(monsters)}x{R}  [pas de position]")

    # -- Detail ressources ------------------------------------------------------
    print(f"\n  {BOLD}Detail ({len(all_resources)} bles) :{R}")
    for iid in sorted(all_resources, key=lambda x: all_resources[x].get('cell') or 9999):
        info = all_resources[iid]
        cell = info['cell']
        up = info.get('up', False)
        tag = f"{GREEN}up{R}" if up else f"{RED}down{R}"
        if cell is not None:
            row, col = cell // MAP_WIDTH, cell % MAP_WIDTH
            print(f"    {tag}  id={iid}  cell={cell:4d}  (r{row:2d}, c{col:2d})")
        else:
            print(f"    {tag}  id={iid}  [pas de position]")

    # -- Log d'actions ----------------------------------------------------------
    _render_log()


def _render_log():
    """Affiche le log d'actions en bas du dashboard."""
    if not action_log:
        return
    print(f"\n  {BOLD}Actions :{R}")
    for ts, level, text in action_log:
        icon = _LOG_ICONS.get(level, "  ")
        color = _LOG_COLORS.get(level, "")
        print(f"    {GRAY}{ts}{R} {color}{icon}{text}{R}")
    print()


# -- Parsing -------------------------------------------------------------------

def parse_mob_groups(f):
    """Extrait les groupes de mobs depuis ISU f11 (entity_id > 2^63).
    Structure: f1.f1=cell, f3.f5={f5=species, f2=level}, f3.f1.f7.f4=monster count."""
    groups = {}
    for sub_bytes in f.get(11, []):
        if not isinstance(sub_bytes, bytes):
            continue
        sub = parse_protobuf(sub_bytes)
        entity_id = gv(sub, 2)
        if entity_id is None or entity_id < (1 << 63):
            continue
        # f1.f1 = cell
        pos_data = gb(sub, 1)
        cell = None
        if pos_data:
            cell = gv(parse_protobuf(pos_data), 1)
        # f3 = groupe info
        monsters = []
        f3_bytes = gb(sub, 3)
        if f3_bytes:
            f3 = parse_protobuf(f3_bytes)
            # f3.f5 = {f5=species, f2=group_level} (fallback)
            fallback_species, fallback_level = 0, 0
            f5_bytes = gb(f3, 5)
            if f5_bytes:
                f5 = parse_protobuf(f5_bytes)
                fallback_species = gv(f5, 5) or 0
                fallback_level = gv(f5, 2) or 0
            # f3.f1.f7.f4 = conteneur monstres individuels (f1=leader, f3=compagnons)
            f1_bytes = gb(f3, 1)
            if f1_bytes:
                f1 = parse_protobuf(f1_bytes)
                f7_bytes = gb(f1, 7)
                if f7_bytes:
                    f7 = parse_protobuf(f7_bytes)
                    f4_bytes = gb(f7, 4)
                    if f4_bytes:
                        f4 = parse_protobuf(f4_bytes)
                        # Chaque entrée (f1 et f3) est un monstre individuel
                        for fn in (1, 3):
                            for m_bytes in f4.get(fn, []):
                                if not isinstance(m_bytes, bytes):
                                    continue
                                m = parse_protobuf(m_bytes)
                                # Chercher type/level à plusieurs profondeurs
                                mt = gv(m, 1) or gv(m, 2) or fallback_species
                                ml = gv(m, 2) or gv(m, 3) or fallback_level
                                # Si f1 ressemble à un species (>1000), l'utiliser
                                f1v = gv(m, 1)
                                f2v = gv(m, 2)
                                f3v = gv(m, 3)
                                if f1v and f1v > 100:
                                    monsters.append((f1v, f2v or f3v or fallback_level))
                                elif f2v and f2v > 100:
                                    monsters.append((f2v, f1v or f3v or fallback_level))
                                else:
                                    monsters.append((fallback_species, fallback_level))
            if not monsters:
                monsters = [(fallback_species, fallback_level)]
        groups[entity_id] = {'cell': cell, 'monsters': monsters}
    return groups


def parse_player_cell(f):
    """Extrait la cellule du joueur depuis le champ 11 de l'ISU."""
    for sub_bytes in f.get(11, []):
        if not isinstance(sub_bytes, bytes):
            continue
        sub = parse_protobuf(sub_bytes)
        if gv(sub, 2) == PLAYER_ID:
            pos_data = gb(sub, 1)
            if pos_data:
                return gv(parse_protobuf(pos_data), 1)
    return None


def handle_isu(value):
    global current_map, all_resources, mob_groups, player_cell

    f = parse_protobuf(value)
    map_id = gv(f, 14)
    if map_id and map_id > 100_000_000:
        current_map = map_id

    # Definitions (f2)
    defs = {}
    for sub_bytes in f.get(2, []):
        if not isinstance(sub_bytes, bytes):
            continue
        sub = parse_protobuf(sub_bytes)
        iid = gv(sub, 1)
        if iid is None:
            continue
        object_id = None
        for fn in (6, 2):
            inner = gb(sub, fn)
            if inner:
                p = parse_protobuf(inner)
                oid = gv(p, 4)
                if oid is not None:
                    object_id = oid
                    break
        if object_id == 45:
            has_f3 = gv(sub, 3) is not None
            f5 = gv(sub, 5)
            if has_f3:
                defs[iid] = {'object_id': object_id, 'cell': None, 'up': f5 is not None}

    # Positions (f6)
    for sub_bytes in f.get(6, []):
        if not isinstance(sub_bytes, bytes):
            continue
        sub = parse_protobuf(sub_bytes)
        iid = gv(sub, 3)
        cell = gv(sub, 2)
        if iid in defs and cell is not None:
            defs[iid]['cell'] = cell

    all_resources = defs
    mob_groups = parse_mob_groups(f)
    player_cell = parse_player_cell(f)

    if _standalone:
        render()


def handle_iro(value):
    """Mouvement d'entité — met à jour player_cell si c'est le joueur."""
    global player_cell
    f = parse_protobuf(value)
    entity_id = gv(f, 1)
    if entity_id != PLAYER_ID:
        return
    # Destination = dernière cellule du path (f3 packed varints)
    path_bytes = gb(f, 3)
    if path_bytes:
        cells = decode_varint_list(path_bytes)
        if cells:
            new_cell = cells[-1]
            if new_cell < MAP_WIDTH * 40:
                player_cell = new_cell
                if _standalone:
                    render()
                return
    # Fallback : f2 comme cellule directe
    cell = gv(f, 2)
    if cell is not None and cell < MAP_WIDTH * 40:
        player_cell = cell
        if _standalone:
            render()


def handle_idq(value):
    """Mise a jour en temps reel d'une ressource (coupee ou repop)."""
    f = parse_protobuf(value)
    f1_bytes = gb(f, 1)
    if not f1_bytes:
        return
    f1 = parse_protobuf(f1_bytes)
    interactive_id = gv(f1, 1)
    if interactive_id is None or interactive_id not in all_resources:
        return
    new_up = gv(f1, 5) is not None
    old_up = all_resources[interactive_id].get('up')
    if new_up != old_up:
        all_resources[interactive_id]['up'] = new_up
        if _standalone:
            tag = "up" if new_up else "down"
            render()
            print(f"  [IDQ] id={interactive_id} -> {tag}")


def on_message(msg_type, value):
    global current_map, player_cell
    if msg_type == 'irj':
        f = parse_protobuf(value)
        new_map = gv(f, 1)
        if new_map:
            current_map = new_map
        all_resources.clear()
        mob_groups.clear()
        player_cell = None
        if _standalone:
            render()
    elif msg_type == 'isu':
        handle_isu(value)
    elif msg_type == 'idq':
        handle_idq(value)
    elif msg_type == 'iro':
        handle_iro(value)

    # Dispatch aux callbacks externes
    for cb in _external_callbacks:
        cb(msg_type, value)


# -- Main (standalone) ----------------------------------------------------------

def main():
    server_ip = detect_server_ip()
    if not server_ip:
        print("[!] Dofus non detecte.")
        sys.exit(1)

    print(f"[*] Serveur : {server_ip}")
    print(f"[*] Change de map pour afficher la grille.\n")

    capture = TcpCapture(server_ip, on_message)
    capture.start()

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
