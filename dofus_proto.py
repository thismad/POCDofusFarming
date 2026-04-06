#!/usr/bin/env python3
"""
Dofus 3 — Module protobuf et capture TCP partagé.
Fournit le parsing protobuf, l'extraction de messages Dofus,
et la capture tcpdump en temps réel avec réassemblage TCP.
"""

import subprocess
import time
import threading
from collections import defaultdict

from dofus_config import IFACE, SERVER_PORT


# ──────────────────────────────────────────
# PROTOBUF PARSER
# ──────────────────────────────────────────

def decode_varint(data, pos):
    """Décode un varint protobuf à la position donnée. Retourne (valeur, nouvelle_position)."""
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def parse_protobuf(data):
    """Parse des données protobuf brutes en un dict {field_number: [valeurs]}."""
    fields = defaultdict(list)
    pos = 0
    while pos < len(data):
        try:
            tag, pos = decode_varint(data, pos)
            fn = tag >> 3
            wt = tag & 7
            if fn == 0 or fn > 500:
                break
            if wt == 0:
                val, pos = decode_varint(data, pos)
                fields[fn].append(val)
            elif wt == 2:
                length, pos = decode_varint(data, pos)
                if pos + length > len(data):
                    break
                fields[fn].append(data[pos:pos + length])
                pos += length
            elif wt == 5:
                if pos + 4 > len(data):
                    break
                pos += 4
            elif wt == 1:
                if pos + 8 > len(data):
                    break
                pos += 8
            else:
                break
        except (IndexError, ValueError):
            break
    return dict(fields)


def gv(fields, num, default=None):
    """Récupère la première valeur entière d'un champ protobuf."""
    for v in fields.get(num, []):
        if isinstance(v, int):
            return v
    return default


def gb(fields, num, default=None):
    """Récupère la première valeur bytes d'un champ protobuf."""
    for v in fields.get(num, []):
        if isinstance(v, bytes):
            return v
    return default


def decode_varint_list(data):
    """Décode une suite de varints packés."""
    vals = []
    pos = 0
    while pos < len(data):
        try:
            v, pos = decode_varint(data, pos)
            vals.append(v)
        except (IndexError, ValueError):
            break
    return vals


# ──────────────────────────────────────────
# EXTRACTION DE MESSAGES DOFUS
# ──────────────────────────────────────────

def extract_messages(data):
    """Extrait les messages Dofus (type_url ankama + payload) depuis des données brutes.
    Utilisé pour le parsing rapide paquet-par-paquet (legacy)."""
    results = []
    prefix = b"ankama.com/"
    pos = 0
    while True:
        idx = data.find(prefix, pos)
        if idx == -1:
            break
        end = idx + len(prefix)
        while end < len(data) and 97 <= data[end] <= 122:
            end += 1
        msg_type = data[idx + len(prefix):end].decode(errors='ignore')
        if end < len(data) and data[end] == 0x12:
            try:
                length, vpos = decode_varint(data, end + 1)
                value = data[vpos:vpos + length]
                results.append((msg_type, value))
            except (IndexError, ValueError):
                pass
        else:
            results.append((msg_type, b''))
        pos = end if end > pos else pos + 1
    return results


def extract_messages_buffered(data):
    """Extrait les messages COMPLETS depuis un buffer TCP réassemblé.
    Retourne (messages, bytes_consumed) — les messages incomplets restent dans le buffer.
    """
    results = []
    prefix = b"ankama.com/"
    pos = 0
    last_consumed = 0
    while True:
        idx = data.find(prefix, pos)
        if idx == -1:
            break
        end = idx + len(prefix)
        while end < len(data) and 97 <= data[end] <= 122:
            end += 1
        msg_type = data[idx + len(prefix):end].decode(errors='ignore')
        if end < len(data) and data[end] == 0x12:
            try:
                length, vpos = decode_varint(data, end + 1)
                msg_end = vpos + length
                if msg_end <= len(data):
                    # Message complet
                    value = data[vpos:msg_end]
                    results.append((msg_type, value))
                    last_consumed = msg_end
                    pos = msg_end
                    continue
                else:
                    # Message incomplet — on s'arrête, il faut plus de données
                    break
            except (IndexError, ValueError):
                pass
        else:
            results.append((msg_type, b''))
            last_consumed = end
        pos = end if end > pos else pos + 1
    return results, last_consumed


# ──────────────────────────────────────────
# CAPTURE TCP AVEC RÉASSEMBLAGE
# ──────────────────────────────────────────

def detect_server_ip():
    """Détecte l'IP du serveur Dofus via lsof."""
    try:
        result = subprocess.run(
            ['lsof', '-i', 'TCP', '-n', '-P'],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split('\n'):
            if 'Dofus' in line and '5555' in line and '->' in line and 'ESTABLISHED' in line:
                return line.split('->')[-1].split(':')[0].strip()
    except Exception:
        pass
    return None


def _strip_ip_tcp_headers(raw):
    """Extrait le payload TCP en strippant les headers IP + TCP."""
    if len(raw) < 40:
        return b''
    # IP header
    ip_version = (raw[0] >> 4) & 0xF
    if ip_version != 4:
        return b''
    ip_header_len = (raw[0] & 0xF) * 4
    if ip_header_len < 20 or len(raw) < ip_header_len + 20:
        return b''
    # TCP header
    tcp_start = ip_header_len
    tcp_data_offset = (raw[tcp_start + 12] >> 4) & 0xF
    tcp_header_len = tcp_data_offset * 4
    if tcp_header_len < 20:
        return b''
    payload_start = tcp_start + tcp_header_len
    if payload_start >= len(raw):
        return b''  # ACK sans payload
    return raw[payload_start:]


class TcpCapture:
    """Capture tcpdump avec reconnexion automatique, réassemblage TCP, et callback par message."""

    def __init__(self, server_ip, handler_fn, iface=IFACE, port=SERVER_PORT):
        self.server_ip = server_ip
        self.handler_fn = handler_fn
        self.iface = iface
        self.port = port
        self._proc = None
        self._stop = threading.Event()
        self._last_packet_time = time.time()
        self.stale_timeout = 30
        # Buffer de réassemblage TCP
        self._buffer = b''
        self._buffer_lock = threading.Lock()

    @property
    def last_packet_age(self):
        return time.time() - self._last_packet_time

    def _build_cmd(self):
        filt = f"host {self.server_ip} and port {self.port}" if self.server_ip else f"port {self.port}"
        return ["tcpdump", "-i", self.iface, "-n", "-x", "-l", filt]

    def _process_packet(self, hex_data):
        try:
            raw = bytes.fromhex(hex_data)
        except ValueError:
            return
        self._last_packet_time = time.time()

        # Extraire le payload TCP (sans headers IP/TCP)
        payload = _strip_ip_tcp_headers(raw)
        if not payload:
            return

        with self._buffer_lock:
            self._buffer += payload

            # Extraire les messages COMPLETS du buffer
            messages, consumed = extract_messages_buffered(self._buffer)
            if consumed > 0:
                self._buffer = self._buffer[consumed:]

            # Cap le buffer pour éviter les fuites mémoire
            if len(self._buffer) > 131072:
                self._buffer = self._buffer[-65536:]

        for msg_type, value in messages:
            try:
                self.handler_fn(msg_type, value)
            except Exception as e:
                print(f"[ERR] {msg_type}: {e}")

        # Changement de map → vider le buffer pour ne pas mélanger
        # les données de l'ancienne et de la nouvelle map
        if any(mt == 'irj' for mt, _ in messages):
            with self._buffer_lock:
                self._buffer = b''

    def run(self):
        """Boucle principale de capture (bloquante). Reconnecte automatiquement."""
        while not self._stop.is_set():
            cmd = self._build_cmd()
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            current_hex = []
            self._buffer = b''
            try:
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    line = line.strip()
                    if line.startswith('0x'):
                        current_hex.append(line.split(':', 1)[1].strip().replace(' ', ''))
                    elif current_hex:
                        self._process_packet(''.join(current_hex))
                        current_hex = []
            except Exception:
                pass
            finally:
                if self._proc:
                    self._proc.terminate()
                    self._proc = None

            if not self._stop.is_set():
                print("[TCP] Connexion perdue, reconnexion dans 2s...")
                new_ip = detect_server_ip()
                if new_ip:
                    self.server_ip = new_ip
                    print(f"[TCP] Nouvelle IP : {new_ip}")
                time.sleep(2)

    def start(self):
        """Lance la capture dans un thread daemon."""
        t = threading.Thread(target=self.run, daemon=True)
        t.start()
        return t

    def stop(self):
        """Arrête la capture."""
        self._stop.set()
        if self._proc:
            self._proc.terminate()
