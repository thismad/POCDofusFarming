#!/usr/bin/env python3
"""
Moteur de combat Dofus 3 — module autonome.
Utilisé par dofus_bot.py (combat seul) et dofus_farmer.py (combat défensif).
"""

import threading, time, os

from dofus_config import (
    PLAYER_ID, MAP_WIDTH,
    END_TURN_X, END_TURN_Y, COMBAT_READY_X, COMBAT_READY_Y,
    SPELL_ROTATION, PA_DEFAULT, PM_DEFAULT,
    DELAY_SPELL_SELECT, DELAY_SPELL_CAST, DELAY_TURN_WAIT, DELAY_END_TURN,
)
from dofus_proto import parse_protobuf, gv, gb, decode_varint_list

# Grille combat — calibré avec 2 points :
#   cell 283 (r20,c3) → (770,539)   cell 341 (r24,c5) → (935,622)
COMBAT_CELL_W = 82.5
COMBAT_ROW_H = 20.75
COMBAT_GRID_OX = 522.5
COMBAT_GRID_OY = 124


# ── Grille ───────────────────────────────────

def cell_to_screen(cell_id):
    row, col = divmod(cell_id, MAP_WIDTH)
    return int(round(COMBAT_GRID_OX + col * COMBAT_CELL_W)), \
           int(round(COMBAT_GRID_OY + row * COMBAT_ROW_H))


def cell_distance(a, b):
    r1, c1 = divmod(a, MAP_WIDTH)
    r2, c2 = divmod(b, MAP_WIDTH)
    dq = (c2 - r2 // 2) - (c1 - r1 // 2)
    dr = r2 - r1
    return max(abs(dq), abs(dr), abs(dq + dr))


# ── Helpers ──────────────────────────────────

def _tag(fid):
    if fid == PLAYER_ID: return "\033[92mMOI\033[0m"
    if fid and fid > 2**63: return f"\033[91mENM({fid & 0xFFFF})\033[0m"
    return f"ID({fid})"


# ── Moteur de combat ─────────────────────────

class CombatEngine:
    def __init__(self, on_combat_start=None, on_combat_end=None, log_fn=print, passive=False):
        self._on_combat_start = on_combat_start
        self._on_combat_end = on_combat_end
        self._log = log_fn
        self._passive = passive

        # État
        self.active = False
        self.my_turn = False
        self.turn_number = 0
        self.fighters = {}        # {id: {"cell": int, "enemy": bool, "alive": bool}}
        self._played_turn = 0
        self._ready_timer = None

        self._handlers = {
            'ize': self._on_ize, 'izb': self._on_izb, 'isj': self._on_isj,
            'ixr': self._on_ixr, 'jco': self._on_jco, 'iuv': self._on_iuv,
            'izl': self._on_izl, 'ibi': self._on_end, 'hwa': self._on_end,
            'iyg': self._on_iyg, 'ibl': self._on_ibl,
        }

    def handle_message(self, msg_type, value):
        h = self._handlers.get(msg_type)
        if not h: return False
        try: h(value)
        except Exception as e: self._log(f"[COMBAT ERR] {msg_type}: {e}")
        return True

    # ── État helpers ─────────────────────────

    def _reset(self):
        self.active = False
        self.my_turn = False
        self.turn_number = 0
        self.fighters.clear()
        self._played_turn = 0

    def _fighter(self, fid):
        if fid not in self.fighters:
            self.fighters[fid] = {"cell": None, "enemy": fid != PLAYER_ID, "alive": True}
        return self.fighters[fid]

    def _enemies(self):
        return {fid: f for fid, f in self.fighters.items()
                if f["enemy"] and f["alive"] and f["cell"] is not None}

    def _me(self):
        return self.fighters.get(PLAYER_ID)

    # ── Handlers ─────────────────────────────

    def _on_ize(self, value):
        """Initialisation du combat — positions des combattants."""
        first = not self.active
        if first:
            self.fighters.clear()
            self.my_turn = False
            self.turn_number = 0
            self._played_turn = 0
            if self._on_combat_start:
                self._on_combat_start()
        self.active = True

        for sub in parse_protobuf(value).get(2, []):
            if not isinstance(sub, bytes): continue
            f2 = parse_protobuf(sub)
            fid = gv(f2, 2)
            pos = gb(f2, 1)
            if not fid or not pos: continue
            cell = gv(parse_protobuf(pos), 1)
            if cell is None: continue
            fi = self._fighter(fid)
            fi["cell"] = cell
            fi["alive"] = True
            self._log(f"[INIT] {_tag(fid)} cell={cell}")

        if first and not self._passive:
            self._start_watcher()
            # Clic "Prêt" après le dernier ize
            if self._ready_timer:
                self._ready_timer.cancel()
            t = threading.Timer(0.8, self._click_ready)
            t.daemon = True
            t.start()
            self._ready_timer = t

    def _on_izb(self, value):
        """Début de tour."""
        f = parse_protobuf(value)
        fid = gv(f, 3)
        rnd = gv(f, 4)
        if fid is None: return

        if fid == PLAYER_ID:
            self.turn_number = rnd or (self.turn_number + 1)
            self.my_turn = True
            enemies = self._enemies()
            me = self._me()
            self._log(f"\n{'='*40} MON TOUR {self.turn_number} — {len(enemies)} ennemi(s) {'='*10}")
            if me: self._log(f"  Moi: cell={me['cell']}")
        else:
            self.my_turn = False

    def _on_isj(self, value):
        """Mouvement / placement."""
        f = parse_protobuf(value)
        fid = gv(f, 4)
        raw = gb(f, 3)
        if not raw: return
        cells = decode_varint_list(raw)
        if not cells: return
        # Mettre à jour la position du combattant
        if fid:
            fi = self._fighter(fid)
            if cells:
                fi["cell"] = cells[-1]

    def _on_ixr(self, value):
        """Résolution de sort — mise à jour des positions."""
        f = parse_protobuf(value)
        # Sort lancé (f31) — position cible
        sub = gb(f, 31)
        if sub:
            fs = parse_protobuf(sub)
            target, cell = gv(fs, 1), gv(fs, 8)
            if target and cell and cell > 50:
                self._fighter(target)["cell"] = cell
            return
        # Poussée / téléport (f5)
        sub = gb(f, 5)
        if sub:
            fs = parse_protobuf(sub)
            fid, to_cell = gv(fs, 3), gv(fs, 4)
            if fid and to_cell:
                self._fighter(fid)["cell"] = to_cell

    def _on_jco(self, value):
        """Confirmation de mouvement."""
        f = parse_protobuf(value)
        cell, fid = gv(f, 1), gv(f, 2)
        if cell and fid:
            self._fighter(fid)["cell"] = cell

    def _on_iuv(self, value):
        """Mise à jour PA/PM (informatif)."""
        pass  # On utilise PA_DEFAULT/PM_DEFAULT

    def _on_izl(self, value):
        """Liste des combattants vivants — marquer les morts."""
        alive_ids = set()
        def _extract(fields, depth=0):
            if depth > 5: return
            for vals in fields.values():
                for v in vals:
                    if isinstance(v, int) and v > 1000:
                        alive_ids.add(v)
                    elif isinstance(v, bytes) and len(v) > 2:
                        try: _extract(parse_protobuf(v), depth + 1)
                        except: pass
        _extract(parse_protobuf(value))

        # Ignorer si aucun ennemi connu dans la liste (update d'équipe partiel)
        known_enemies = {fid for fid, f in self.fighters.items() if f["enemy"]}
        if not alive_ids & known_enemies:
            return
        for fid, f in self.fighters.items():
            if f["enemy"] and f["alive"] and fid not in alive_ids:
                f["alive"] = False
                self._log(f"\033[91m[MORT]\033[0m {_tag(fid)}")

    def _on_end(self, value):
        """Fin du combat."""
        if not self.active: return
        self._log(f"\n{'#'*40} FIN DU COMBAT {'#'*10}\n")
        self._reset()
        # Fermer la fenêtre de résultats
        time.sleep(3.0)
        os.system("cliclick c:1080,933")
        self._log("[COMBAT] Fenêtre de résultats fermée")
        if self._on_combat_end:
            self._on_combat_end()

    def _on_iyg(self, value):
        """Détail des dégâts."""
        for sub in parse_protobuf(value).get(2, []):
            if not isinstance(sub, bytes): continue
            fs = parse_protobuf(sub)
            caster = gv(fs, 3)
            for sub2 in fs.get(1, []):
                if not isinstance(sub2, bytes): continue
                fs2 = parse_protobuf(sub2)
                target, dmg = gv(fs2, 13), gv(fs2, 10)
                if dmg:
                    self._log(f"\033[95m[HIT]\033[0m {_tag(caster)} → {_tag(target)} dmg={dmg}")

    def _on_ibl(self, value):
        """Loot fin de combat."""
        f = parse_protobuf(value)
        self._log(f"\033[93m[LOOT]\033[0m XP={gv(f, 2)} Kamas={gv(f, 3)}")

    # ── Actions ──────────────────────────────

    def _click_ready(self):
        self._log(f"[COMBAT] Clic prêt")
        os.system(f"cliclick c:{COMBAT_READY_X},{COMBAT_READY_Y}")
        time.sleep(1.0)
        os.system(f"cliclick c:{COMBAT_READY_X},{COMBAT_READY_Y}")

    def _cast(self, key, cell_id):
        sx, sy = cell_to_screen(cell_id)
        os.system(f"cliclick t:{key}")
        time.sleep(DELAY_SPELL_SELECT)
        os.system(f"cliclick c:{sx},{sy}")
        time.sleep(DELAY_SPELL_CAST)

    def _end_turn(self):
        os.system(f"cliclick c:{END_TURN_X},{END_TURN_Y}")
        time.sleep(DELAY_END_TURN)

    def _in_range(self, my_cell, target_cell, spell):
        """Vérifie si la cible est dans la portée min-max du sort."""
        dist = cell_distance(my_cell, target_cell)
        return spell.get('min_range', 0) <= dist <= spell['range']

    def _move_towards(self, my_cell, target_cell, pm):
        """Calcule une cellule de déplacement pour être à portée de la cible.
        Gère la portée min (trop près) et max (trop loin)."""
        dist = cell_distance(my_cell, target_cell)
        spell = SPELL_ROTATION[0]
        min_r = spell.get('min_range', 0)
        max_r = spell['range']
        if min_r <= dist <= max_r:
            return None
        my_row, my_col = divmod(my_cell, MAP_WIDTH)
        t_row, t_col = divmod(target_cell, MAP_WIDTH)
        dr, dc = t_row - my_row, t_col - my_col
        if dist > max_r:
            # Trop loin — avancer vers la cible
            steps = min(dist - max_r, pm)
            sign_r = 1 if dr > 0 else -1
            sign_c = 1 if dc > 0 else -1
        else:
            # Trop près — reculer
            steps = min(min_r - dist, pm)
            sign_r = -1 if dr > 0 else 1
            sign_c = -1 if dc > 0 else 1
        total = abs(dr) + abs(dc)
        if total == 0: return None
        row_steps = round(steps * abs(dr) / total)
        col_steps = steps - row_steps
        new_row = max(0, min(39, my_row + sign_r * row_steps))
        new_col = max(0, min(MAP_WIDTH - 1, my_col + sign_c * col_steps))
        return new_row * MAP_WIDTH + new_col

    # ── Tour de combat ───────────────────────

    def _do_turn(self):
        enemies = self._enemies()
        if not enemies:
            self._end_turn()
            return

        me = self._me()
        my_cell = me["cell"] if me else None
        pa, pm = PA_DEFAULT, PM_DEFAULT
        max_range = max(s['range'] for s in SPELL_ROTATION)

        # Trier par distance
        targets = sorted(enemies.values(), key=lambda e: cell_distance(my_cell, e["cell"]) if my_cell else 0)
        spell = SPELL_ROTATION[0]

        # Si personne à portée, avancer/reculer pour être en portée du plus proche
        if my_cell and not any(self._in_range(my_cell, e["cell"], spell) for e in targets):
            move = self._move_towards(my_cell, targets[0]["cell"], pm)
            if move:
                dist = cell_distance(my_cell, targets[0]["cell"])
                sx, sy = cell_to_screen(move)
                self._log(f"  >> {'Recul' if dist < spell.get('min_range', 0) else 'Avance'} → cell {move} ({sx},{sy})")
                os.system(f"cliclick c:{sx},{sy}")
                time.sleep(1.5)
                me["cell"] = move
                my_cell = move

        # Lancer les sorts
        attacks = 0
        for enemy in targets:
            if not self.active or pa < 2:
                break
            if not enemy["alive"]:
                continue
            if my_cell and not self._in_range(my_cell, enemy["cell"], spell):
                continue

            casts = 0
            while pa >= spell['pa'] and casts < spell['max_per_target'] and self.active and enemy["alive"]:
                self._log(f"  [CAST] {spell['name']} → {_tag(next(fid for fid, f in self.fighters.items() if f is enemy))} (pa={pa}, dist={cell_distance(my_cell, enemy['cell'])})")
                self._cast(spell['key'], enemy["cell"])
                pa -= spell['pa']
                attacks += 1
                casts += 1
                time.sleep(0.5)

        # Avancer vers les survivants après les attaques
        if self.active and my_cell:
            remaining = [f for f in self.fighters.values() if f["enemy"] and f["alive"] and f["cell"]]
            if remaining:
                closest = min(remaining, key=lambda e: cell_distance(my_cell, e["cell"]))
                if not self._in_range(my_cell, closest["cell"], spell):
                    move = self._move_towards(my_cell, closest["cell"], pm)
                    if move:
                        sx, sy = cell_to_screen(move)
                        self._log(f"  >> Avance → cell {move}")
                        os.system(f"cliclick c:{sx},{sy}")
                        time.sleep(1.0)
                        me["cell"] = move

        self._log(f"  >> {attacks} attaque(s), ~{pa} PA restants")
        self._end_turn()

    # ── Watcher ──────────────────────────────

    def _start_watcher(self):
        def _loop():
            while self.active:
                if self.my_turn and self.turn_number > self._played_turn:
                    enemies = self._enemies()
                    if enemies:
                        time.sleep(DELAY_TURN_WAIT)
                        if self.my_turn and self.active:
                            self._do_turn()
                            self._played_turn = self.turn_number
                    elif self.active:
                        self._end_turn()
                        self._played_turn = self.turn_number
                time.sleep(0.15)
        threading.Thread(target=_loop, daemon=True).start()
