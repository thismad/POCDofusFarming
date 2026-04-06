#!/usr/bin/env python3
"""
Dofus 3 Combat Bot
- Capture TCP via tcpdump en temps réel
- Parse le protocole protobuf Dofus (type.ankama.com/xxx)
- Automatise le combat via cliclick (sort + clic)
- Mort détectée via izl (fighter list)

Usage:
  python3 dofus_bot.py              # Mode combat (attaque auto)
  python3 dofus_bot.py --listen     # Mode écoute seule (debug)
  python3 dofus_bot.py --raw        # Mode raw (affiche tous les messages)
  python3 dofus_bot.py --nav --pos 6,-19 --goto 4,-27
"""

import sys, threading, time, os, argparse

from dofus_config import (
    PLAYER_ID, MAP_WIDTH, CELL_W, ROW_H, GRID_OX, GRID_OY,
    NAV_CLICK, NAV_TIMEOUT, NAV_DETOUR, COORD_DELTA,
    DELAY_TURN_WAIT, DELAY_ISU_WAIT,
)
from dofus_proto import (
    parse_protobuf, gv, gb, decode_varint, decode_varint_list,
    extract_messages, detect_server_ip, TcpCapture,
)
from combat import CombatEngine
import map_view

# Récolte pendant navigation (activé par --harvest)
HARVEST_ON_NAV = False

# Combat engine (partagé entre modes)
_combat = CombatEngine()


# ──────────────────────────────────────────
# NAVIGATION STATE
# ──────────────────────────────────────────

class NavigationState:
    def __init__(self):
        self.current_map = None
        self.map_ready = threading.Event()
        self.map_ready.set()
        self.history = []
        self.coords = None
        self.coords_map = {}
        self.should_pause = None  # callable -> True si la nav doit s'interrompre

    def set_coords(self, x, y):
        self.coords = (x, y)
        if self.current_map:
            self.coords_map[self.current_map] = (x, y)
        print(f"[NAV] Coordonnées : ({x}, {y})")

    def set_map(self, map_id):
        if map_id != self.current_map:
            self.current_map = map_id
            self.history.append(map_id)
            if map_id in self.coords_map:
                self.coords = self.coords_map[map_id]
            print(f"[NAV] Map : {map_id}")

    def map_loaded(self):
        self.map_ready.set()

    def detect_map(self, map_id):
        if map_id and map_id > 100000000 and self.current_map is None:
            self.current_map = map_id
            self.history.append(map_id)
            print(f"[NAV] Map detectee via iro : {map_id}")

    def move(self, direction):
        if direction not in NAV_CLICK:
            return False
        self.map_ready.clear()
        x, y = NAV_CLICK[direction]
        os.system(f"cliclick c:{x},{y}")
        time.sleep(0.3)
        os.system(f"cliclick c:{x},{y}")
        ok = self.map_ready.wait(timeout=NAV_TIMEOUT)
        if ok and self.coords:
            dx, dy = COORD_DELTA[direction]
            self.coords = (self.coords[0] + dx, self.coords[1] + dy)
            self.coords_map[self.current_map] = self.coords
        if not ok:
            self.map_ready.set()
        return ok

    def navigate(self, direction):
        if self.move(direction):
            return True
        for detour_path in NAV_DETOUR[direction]:
            success = True
            for i, step in enumerate(detour_path):
                if not self.move(step):
                    opposite = {'top': 'bottom', 'bottom': 'top', 'left': 'right', 'right': 'left'}
                    for j in range(i - 1, -1, -1):
                        self.move(opposite[detour_path[j]])
                    success = False
                    break
                time.sleep(0.3)
            if success:
                return True
        return False

    def goto(self, target_x, target_y):
        if not self.coords:
            print("[NAV] Coordonnees inconnues.")
            return False
        max_steps = 50
        for step in range(max_steps):
            if self.should_pause and self.should_pause():
                return False
            if self.coords == (target_x, target_y):
                print(f"[NAV] Arrivé ! ({target_x}, {target_y})")
                return True
            cx, cy = self.coords
            dx, dy = target_x - cx, target_y - cy
            direction = ('right' if dx > 0 else 'left') if abs(dx) >= abs(dy) else ('bottom' if dy > 0 else 'top')
            if not self.navigate(direction):
                alt = ('bottom' if dy > 0 else 'top' if dy else None) if abs(dx) >= abs(dy) else ('right' if dx > 0 else 'left' if dx else None)
                if not alt or not self.navigate(alt):
                    print(f"[NAV] Bloqué à ({cx}, {cy})")
                    return False
            _harvest_on_current_map()
            time.sleep(0.3)
        return False


nav = NavigationState()


# ──────────────────────────────────────────
# HARVEST — réutilise dofus_farmer
# ──────────────────────────────────────────

import dofus_farmer


def _harvest_on_current_map():
    """Attend l'ISU puis récolte si --harvest est actif."""
    if not HARVEST_ON_NAV:
        return
    dofus_farmer.state.isu_received.wait(timeout=DELAY_ISU_WAIT)
    dofus_farmer.harvest_all_on_map()


# ──────────────────────────────────────────
# DISPATCH
# ──────────────────────────────────────────

def _wait_combat_over():
    """Bloque tant qu'un combat est actif."""
    if not _combat.active:
        return
    print("[AUTO] Combat en cours, pause...")
    while _combat.active:
        time.sleep(0.5)
    time.sleep(1.5)
    print("[AUTO] Reprise")


def mode_auto(points, server_ip):
    """Mode automatique : délègue au farmer (zone serpentine + récolte + combat)."""
    global HARVEST_ON_NAV
    HARVEST_ON_NAV = True

    _combat._on_combat_start = dofus_farmer.state.enter_combat
    _combat._on_combat_end = dofus_farmer._on_combat_end_farmer

    c1, c2 = points[0], points[1]
    dofus_farmer.state.coords = nav.coords

    # Démarrer map_view comme backend TCP pour le farmer
    map_view.register_callback(dofus_farmer._on_message)
    map_view.start(server_ip)

    route, w, h = dofus_farmer._zone_to_route(c1, c2)

    print(f"\n{'=' * 50}")
    print(f"  MODE AUTO (via farmer)")
    print(f"  Zone : {c1} -> {c2} [{w}x{h} = {w*h} maps]")
    print(f"  Combat auto + Récolte activés")
    print(f"{'=' * 50}\n")

    dofus_farmer.mode_farm_route(route)


def _handle_irj(v):
    nav.set_map(gv(parse_protobuf(v), 1))
    if HARVEST_ON_NAV:
        dofus_farmer._on_irj()
        dofus_farmer.state.isu_received.clear()


def _handle_ibi(v):
    _combat.handle_message('ibi', v)
    if HARVEST_ON_NAV:
        dofus_farmer.handle_ibi(v)


def _handle_isu(v):
    if HARVEST_ON_NAV:
        dofus_farmer._on_isu()


def _handle_ibl(v):
    f = parse_protobuf(v)
    xp = gv(f, 2)
    kamas = gv(f, 3)
    print(f"[LOOT] XP={xp} Kamas={kamas}")


def _dispatch_message(msg_type, value):
    """Callback pour TcpCapture."""
    if _raw_mode:
        f = parse_protobuf(value)
        summary = {k: [x if isinstance(x, int) else f"b({len(x)})" for x in v] for k, v in f.items()}
        print(f"[{msg_type:4s}] {summary}")
    else:
        handler = HANDLERS.get(msg_type)
        if handler:
            try:
                handler(value)
            except Exception as e:
                print(f"[ERR] {msg_type}: {e}")


_raw_mode = False

HANDLERS = {
    'ize': lambda v: _combat.handle_message('ize', v),
    'izb': lambda v: _combat.handle_message('izb', v),
    'isj': lambda v: _combat.handle_message('isj', v),
    'ixr': lambda v: _combat.handle_message('ixr', v),
    'jco': lambda v: _combat.handle_message('jco', v),
    'iuv': lambda v: _combat.handle_message('iuv', v),
    'izl': lambda v: _combat.handle_message('izl', v),
    'hwa': lambda v: _combat.handle_message('hwa', v),
    'iyg': lambda v: _combat.handle_message('iyg', v),
    'ibl': lambda v: _combat.handle_message('ibl', v),
    'ibi': _handle_ibi,
    'irj': _handle_irj,
    'kta': lambda v: nav.map_loaded(),
    'iro': lambda v: nav.detect_map(gv(parse_protobuf(v), 2)),
    'isu': _handle_isu,
    'itk': lambda v: dofus_farmer.handle_itk(v) if HARVEST_ON_NAV else None,
    'idk': lambda v: dofus_farmer.handle_idk(v) if HARVEST_ON_NAV else None,
    'idq': lambda v: dofus_farmer._on_idq(v) if HARVEST_ON_NAV else None,
    'idr': lambda v: dofus_farmer.handle_idr(v) if HARVEST_ON_NAV else None,
}


# ──────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Dofus 3 Bot')
    parser.add_argument('--listen', action='store_true')
    parser.add_argument('--raw', action='store_true')
    parser.add_argument('--nav', action='store_true')
    parser.add_argument('--go', default=None)
    parser.add_argument('--goto', default=None)
    parser.add_argument('--pos', default=None)
    parser.add_argument('--ip', default=None)
    parser.add_argument('--harvest', action='store_true',
                        help='Récolte les céréales (blé, orge) sur chaque map pendant la navigation')
    parser.add_argument('--auto', default=None,
                        help='Mode auto : 4 points de zone "x1,y1 x2,y2 x3,y3 x4,y4"')
    args = parser.parse_args()

    global HARVEST_ON_NAV, _raw_mode
    if args.harvest or args.auto:
        HARVEST_ON_NAV = True
    if args.raw:
        _raw_mode = True

    server_ip = args.ip or detect_server_ip()
    if not server_ip:
        server_ip = "54.195.36.37"
        print(f"[WARN] Dofus non détecté, IP par défaut : {server_ip}")

    is_nav = args.nav or args.go or args.goto or args.auto

    if is_nav and args.pos:
        px, py = args.pos.split(',')
        nav.set_coords(int(px), int(py))
    elif is_nav:
        pass

    harvest_tag = "+HARVEST" if HARVEST_ON_NAV else ""
    print(f"[BOT] Serveur={server_ip} Mode={'NAV' if is_nav else 'RAW' if args.raw else 'COMBAT'}{harvest_tag}")

    # TCP capture avec reconnexion automatique
    capture = TcpCapture(server_ip, _dispatch_message)
    capture.start()
    time.sleep(1)

    if args.auto:
        if not nav.coords:
            print("[AUTO] --pos requis avec --auto")
            return
        # Parse "x1,y1 x2,y2 x3,y3 x4,y4"
        points = []
        for part in args.auto.split():
            xy = part.split(',')
            points.append((int(xy[0]), int(xy[1])))
        if len(points) < 2:
            print("[AUTO] Au moins 2 points requis")
            return
        for _ in range(20):
            if nav.current_map: break
            time.sleep(0.5)
        try:
            mode_auto(points, server_ip)
        except KeyboardInterrupt:
            print(f"\n[AUTO] Arrêt | {dofus_farmer.state.harvests_done} récoltes")

    elif args.goto:
        tx, ty = args.goto.split(',')
        if not nav.coords:
            print("[NAV] --pos requis avec --goto")
            return
        for _ in range(20):
            if nav.current_map: break
            time.sleep(0.5)
        _harvest_on_current_map()
        nav.goto(int(tx), int(ty))
        _harvest_on_current_map()

    elif args.go:
        shortcuts = {'t': 'top', 'r': 'right', 'b': 'bottom', 'l': 'left'}
        for _ in range(20):
            if nav.current_map: break
            time.sleep(0.5)
        _harvest_on_current_map()
        for c in args.go:
            d = shortcuts.get(c)
            if d and not nav.navigate(d):
                break
            _harvest_on_current_map()
            time.sleep(0.5)

    elif is_nav:
        shortcuts = {'t': 'top', 'r': 'right', 'b': 'bottom', 'l': 'left'}
        for _ in range(20):
            if nav.current_map: break
            time.sleep(0.5)
        _harvest_on_current_map()
        while True:
            try:
                c = f" ({nav.coords[0]},{nav.coords[1]})" if nav.coords else ""
                h = " [H]" if HARVEST_ON_NAV else ""
                cmd = input(f"[{nav.current_map or '?'}{c}{h}] > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd in ('q', 'quit'): break
            elif cmd == 'harvest':
                dofus_farmer.state.isu_received.wait(timeout=DELAY_ISU_WAIT)
                dofus_farmer.harvest_all_on_map()
            elif cmd.startswith('pos '):
                p = cmd.split()
                if len(p) == 3: nav.set_coords(int(p[1]), int(p[2]))
            elif cmd.startswith('goto '):
                p = cmd.split()
                if len(p) == 3:
                    _harvest_on_current_map()
                    nav.goto(int(p[1]), int(p[2]))
                    _harvest_on_current_map()
            elif all(c in shortcuts for c in cmd) and cmd:
                for c in cmd:
                    if not nav.navigate(shortcuts[c]): break
                    _harvest_on_current_map()
                    time.sleep(0.5)
    else:
        auto = not args.listen and not args.raw
        if auto:
            print("[BOT] Auto-attaque activée\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[STOP]")


if __name__ == "__main__":
    main()
