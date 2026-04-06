# Dofus Touch Protocol Research

**Built in a single afternoon as a vibe coding experiment.** This is a raw proof of concept — the goal was to show that reverse-engineering the Dofus Touch protocol and automating gameplay is achievable quickly with minimal tooling. There are known bugs, the code is rough, and this project will not be maintained.

> **Disclaimer:** This project is for educational and research purposes only. It is not intended for use on live game servers or to violate any terms of service.

## How it works

The bot operates in three layers:

1. **Protocol interception** — `tcpdump` captures TCP traffic to the game server (port 5555). Raw packets are reassembled and parsed for protobuf messages prefixed with `ankama.com/`.
2. **Game state tracking** — Decoded messages (3-letter type codes like `isu`, `ize`, `irj`...) are routed to handlers that maintain an internal model of the map, resources, mobs, and combat state.
3. **Screen automation** — `cliclick` (macOS) sends mouse clicks at coordinates computed from an isometric grid projection calibrated to the game window.

## Architecture

```
dofus_config.py         Grid calibration, spell rotation, navigation constants
dofus_proto.py          TCP capture + protobuf parser (TcpCapture)
combat.py               Combat engine (turn management, spell casting, pathfinding)
map_view.py             ANSI dashboard + resource/mob/player state tracking
dofus_farmer.py         Autonomous farming bot (zone, route, stationary modes)
dofus_bot.py            Orchestrator (combat / navigation / auto mode)
```

## Key protocol messages

| Code | Description |
|------|-------------|
| `isu` | Map state update (resources, mobs) |
| `irj` | Map change |
| `kta` | Map loaded |
| `ize` | Combat init (fighter positions) |
| `izb` | Turn start |
| `ixr` | Spell cast |
| `izl` | Alive fighters (detect kills) |
| `itk` | Interaction started (harvest) |
| `idk` | Item gained |
| `idr` | Interaction ended |
| `ibl` | XP / Kamas received |

## Requirements

- macOS (uses `tcpdump`, `cliclick`, `screencapture`)
- Python 3.10+
- Dofus Touch running on the same machine (or accessible via network)
- `sudo` access for `tcpdump` packet capture

Install cliclick:

```bash
brew install cliclick
```

## Configuration

Edit `dofus_config.py` before running:

| Constant | Description |
|----------|-------------|
| `PLAYER_ID` | Your character's in-game ID |
| `IFACE` | Network interface (`en0` for Wi-Fi, `en7` for USB tethering, etc.) |
| `GRID_OX/OY`, `CELL_W`, `ROW_H` | Isometric grid calibration for your screen resolution |
| `NAV_CLICK` | Screen coordinates for map edge clicks (top/right/bottom/left) |
| `SPELL_ROTATION` | Combat spell sequence |
| `CEREAL_OBJECTS` | Resource object IDs to harvest (default: wheat) |

## Usage

### Farming bot (`dofus_farmer.py`)

The farmer auto-detects the game server, intercepts the protocol, and harvests resources.

```bash
# Stay on current map, harvest everything that spawns
python3 dofus_farmer.py

# Follow a manual route in a loop (t=top, r=right, b=bottom, l=left)
python3 dofus_farmer.py --route rrbblltt

# Farm a rectangular zone with serpentine path
# The bot navigates to the top-left corner first, harvesting along the way
python3 dofus_farmer.py --pos 5,-22 --zone "5,-22 7,-20"
```

**Zone mode** generates a serpentine (zigzag) path covering the rectangle defined by two world coordinates. The bot:

1. Navigates from `--pos` to the top-left corner of the zone (harvesting on each map)
2. Runs the serpentine loop: harvest all resources on each map, then move to the next
3. Returns to the start via the rectangle border (not diagonally) and repeats
4. Handles blocked paths with detour navigation (e.g. top -> left -> bottom)
5. Auto-fights mobs if aggro'd, then resumes farming

### Combat / navigation bot (`dofus_bot.py`)

```bash
# Combat auto-attack (sit on a map, auto-fight when combat starts)
python3 dofus_bot.py

# Navigate to a target coordinate
python3 dofus_bot.py --pos 6,-19 --goto 4,-27

# Navigate along a direction sequence
python3 dofus_bot.py --go rrbb

# Navigate + harvest along the way
python3 dofus_bot.py --pos 6,-19 --goto 4,-27 --harvest

# Auto mode: zigzag zone with combat + harvest (delegates to farmer)
python3 dofus_bot.py --pos 5,-22 --auto "5,-22 7,-20"

# Interactive navigation shell
python3 dofus_bot.py --nav --pos 6,-19

# Raw protocol dump (debug)
python3 dofus_bot.py --raw
```

### Overriding server IP

If the game server isn't auto-detected:

```bash
python3 dofus_farmer.py --ip 54.195.36.37
python3 dofus_bot.py --ip 54.195.36.37
```

## License

MIT
