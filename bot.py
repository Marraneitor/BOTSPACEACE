#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOT — Buscador de imágenes BOX1 / BOX2
=======================================
  • Tecla  P          →  Iniciar / Pausar el bot  (global, no necesita foco)
  • Botón ⌖ Búsqueda  →  Área donde se escanean BOX1/BOX2
  • Botón ⌖ Movimiento→  Área donde se hace clic aleatorio si no se detecta nada
  • Análisis cada 50 ms — click en la imagen más cercana al centro O click aleatorio
  • Cooldown de 3 segundos entre clicks (imagen encontrada o aleatorio)
  • Click completo: presión + suelta
  • Coloca  box1.png  y  box2.png  en la misma carpeta que este script.
  • Seguridad: mueve el mouse a la esquina SUPERIOR-IZQUIERDA para parar todo.
"""

import os
import sys
import time
import random
import threading
import json
import ctypes
from ctypes import wintypes

import cv2
import numpy as np
import pyautogui
import keyboard
import tkinter as tk
from tkinter import ttk

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════════

# Rutas de las imágenes de referencia  (misma carpeta que el script/exe)
# Compatible con PyInstaller --onefile (sys.executable) y ejecución directa (__file__)
if getattr(sys, 'frozen', False):
    _HERE = os.path.dirname(sys.executable)
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
BOX1_FILE    = os.path.join(_HERE, "box1.png")
BOX2_FILE    = os.path.join(_HERE, "box2.png")
LUMID_FILE   = os.path.join(_HERE, "lumid.png")
ELITE_FILE   = os.path.join(_HERE, "elitelumid.png")
RE_FILE      = os.path.join(_HERE, "re.png")
RE1_FILE     = os.path.join(_HERE, "re1.png")
LOGIN_FILE   = os.path.join(_HERE, "login.png")
LOGIN1_FILE  = os.path.join(_HERE, "login1.png")
CONFIG_FILE  = os.path.join(_HERE, "config.json")

# Imágenes que requieren doble click 10 px por encima del centro detectado
DOUBLE_CLICK_NAMES = {"LUMID", "ELITELUMID"}

# Imágenes que usan coordenada de click personalizada (definida por el usuario)
CUSTOM_CLICK_NAMES = {"RE", "RE1"}

THRESHOLD        = 0.80  # Confianza mínima (0–1) para aceptar un match
RE_THRESHOLD     = 0.70  # Umbral más bajo para RE y RE1
LOGIN_THRESHOLD  = 0.75  # Umbral para imágenes de login
SCAN_INTERVAL  = 0.001  # Intervalo de análisis en segundos (1 ms = 1000 veces/seg)
CLICK_HOLD     = 0.01  # Duración del click (presión → suelta)
COLLECT_WAIT   = 2.0   # Segundos de espera tras hacer click en una caja
RANDOM_DELAY   = 2.0   # Cooldown entre clicks aleatorios (sin caja detectada)
MAX_LOG_ROWS   = 150   # Máximo de líneas en el log antes de recortar

# ── Estados del worker ──
STATE_SCANNING  = "🔍 ESCANEANDO"
STATE_TRAVELING = "🚀 VIAJANDO"

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.0

# ═══════════════════════════════════════════════════════════════════════════════
#  ESTADO COMPARTIDO
# ═══════════════════════════════════════════════════════════════════════════════

_state = {
    "running":     False,
    "region":      None,   # (x1, y1, x2, y2) — área de búsqueda de imágenes
    "move_region": None,   # (x1, y1, x2, y2) — área de click aleatorio
    "re_click":     None,   # (x, y) — coordenada fija de click para imagen RE
    "re1_click":    None,   # (x, y) — coordenada fija de click para imagen RE1
    "login_click":  None,   # (x, y) — coordenada fija de click para imagen LOGIN
    "login1_click": None,   # (x, y) — coordenada fija de click para imagen LOGIN1
    "bot_state":   STATE_SCANNING,
    "quit":        threading.Event(),
}

# ═══════════════════════════════════════════════════════════════════════════════
#  SELECTOR DE REGIÓN  (overlay transparente)
# ═══════════════════════════════════════════════════════════════════════════════

class RegionSelector:
    """
    Ventana a pantalla completa semitransparente.
    El usuario hace clic, arrastra y suelta para definir el área de búsqueda.
    """

    def __init__(self, callback, label="Área de búsqueda", color="#ff4444"):
        self.callback  = callback
        self._color    = color
        self._sx = self._sy = 0
        self._rect_id  = None

        self.win = tk.Toplevel()
        self.win.attributes("-fullscreen", True)
        self.win.attributes("-alpha", 0.30)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="black")
        self.win.config(cursor="crosshair")

        self.canvas = tk.Canvas(
            self.win, bg="gray15", cursor="crosshair", highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)

        sw = self.win.winfo_screenwidth()
        self.canvas.create_text(
            sw // 2, 44,
            text=f"{label}  —  Clic en una esquina · arrastra · suelta   |   ESC = cancelar",
            fill="white", font=("Consolas", 13, "bold"),
        )

        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Escape>", lambda _: self.win.destroy())

    def _on_press(self, evt):
        self._sx, self._sy = evt.x, evt.y
        if self._rect_id:
            self.canvas.delete(self._rect_id)
            self._rect_id = None

    def _on_drag(self, evt):
        if self._rect_id:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            self._sx, self._sy, evt.x, evt.y,
            outline=self._color, width=2,
            fill=self._color, stipple="gray25",
        )

    def _on_release(self, evt):
        x1 = min(self._sx, evt.x);  y1 = min(self._sy, evt.y)
        x2 = max(self._sx, evt.x);  y2 = max(self._sy, evt.y)
        self.win.destroy()
        if (x2 - x1) > 15 and (y2 - y1) > 15:
            self.callback((x1, y1, x2, y2))


# ═══════════════════════════════════════════════════════════════════════════════
#  SELECTOR DE PUNTO  (overlay para marcar una sola coordenada con un clic)
# ═══════════════════════════════════════════════════════════════════════════════

class PointSelector:
    """
    Ventana a pantalla completa semitransparente.
    El usuario hace un solo clic para definir la coordenada de destino.
    """

    def __init__(self, callback, label="Coordenada de click", color="#e67e22"):
        self.callback = callback

        self.win = tk.Toplevel()
        self.win.attributes("-fullscreen", True)
        self.win.attributes("-alpha", 0.35)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="black")
        self.win.config(cursor="crosshair")

        self.canvas = tk.Canvas(
            self.win, bg="gray15", cursor="crosshair", highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)

        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        self.canvas.create_text(
            sw // 2, 44,
            text=f"{label}  —  Haz clic en la coordenada exacta de destino   |   ESC = cancelar",
            fill="white", font=("Consolas", 13, "bold"),
        )
        self.canvas.create_text(
            sw // 2, sh // 2,
            text="+",
            fill=color, font=("Consolas", 28, "bold"),
        )

        self.canvas.bind("<ButtonPress-1>", self._on_click)
        self.win.bind("<Escape>", lambda _: self.win.destroy())

    def _on_click(self, evt):
        x, y = evt.x_root, evt.y_root
        self.win.destroy()
        self.callback((x, y))


# ═══════════════════════════════════════════════════════════════════════════════
#  OVERLAY DE CENTRO  (cruz verde fija en el centro de pantalla)
# ═══════════════════════════════════════════════════════════════════════════════

class CenterOverlay:
    """Pequeña ventana transparente siempre encima que marca el centro de pantalla."""
    RADIUS = 30   # radio del crosshair en píxeles

    def __init__(self, root):
        sw, sh  = pyautogui.size()
        size    = self.RADIUS * 2
        ox      = sw // 2 - self.RADIUS
        oy      = sh // 2 - self.RADIUS

        self.win = tk.Toplevel(root)
        self.win.geometry(f"{size}x{size}+{ox}+{oy}")
        self.win.overrideredirect(True)           # sin bordes ni barra
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", "black")  # negro → transparente
        self.win.configure(bg="black")
        self.win.wm_attributes("-disabled", True)          # clics pasan a través

        c = tk.Canvas(self.win, width=size, height=size,
                      bg="black", highlightthickness=0)
        c.pack()
        mid = self.RADIUS
        # Líneas de la cruz
        c.create_line(0,   mid, size, mid,  fill="#00ff88", width=2)
        c.create_line(mid, 0,   mid,  size, fill="#00ff88", width=2)
        # Círculo central
        r = 5
        c.create_oval(mid-r, mid-r, mid+r, mid+r, outline="#00ff88", width=2)

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
#  CLICK RÁPIDO  (SendInput — sin animación de cursor)
# ═══════════════════════════════════════════════════════════════════════════════

_user32 = ctypes.windll.user32
_SW     = _user32.GetSystemMetrics(0)
_SH     = _user32.GetSystemMetrics(1)

# MOUSEEVENTF flags — con ABSOLUTE el sistema interpreta dx/dy como coords normalizadas 0-65535
_ME_MOVE_ABS  = 0x8001  # MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
_ME_DOWN_ABS  = 0x8002  # MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE
_ME_UP_ABS    = 0x8004  # MOUSEEVENTF_LEFTUP  | MOUSEEVENTF_ABSOLUTE


def _norm(x: int, y: int):
    """Convierte px de pantalla a coordenadas normalizadas 0-65535."""
    nx = int(x * 65535 / max(_SW - 1, 1))
    ny = int(y * 65535 / max(_SH - 1, 1))
    return nx, ny


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_ulong),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("_u", _INPUTUNION)]


_INPUT_SZ = ctypes.sizeof(_INPUT)


# ═══════════════════════════════════════════════════════════════════════════════
#  WORKER  (hilo de búsqueda — segundo plano)
# ═══════════════════════════════════════════════════════════════════════════════

def _full_click(x: int, y: int):
    """
    Mueve el cursor a (x,y) y hace doble click completo.
    Usa mouse_event con coordenadas ABSOLUTAS — sin race condition.
    """
    nx, ny = _norm(x, y)
    _user32.mouse_event(_ME_MOVE_ABS, nx, ny, 0, 0)
    _user32.mouse_event(_ME_DOWN_ABS, nx, ny, 0, 0)
    _user32.mouse_event(_ME_UP_ABS,   nx, ny, 0, 0)
    _user32.mouse_event(_ME_DOWN_ABS, nx, ny, 0, 0)
    _user32.mouse_event(_ME_UP_ABS,   nx, ny, 0, 0)


def _double_click(x: int, y: int):
    """Alias — igual que _full_click (ya es doble click)."""
    _full_click(x, y)


def _capture(region):
    """Captura la región y devuelve imagen BGR, o None si falla."""
    x1, y1, x2, y2 = region
    ss = pyautogui.screenshot(region=(x1, y1, x2 - x1, y2 - y1))
    return cv2.cvtColor(np.array(ss), cv2.COLOR_RGB2BGR)


def _find_template_matches(img, tmpl, threshold):
    """Devuelve múltiples coincidencias (score, x, y) filtradas por solapamiento."""
    th, tw = tmpl.shape[:2]
    res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(res >= threshold)
    if len(xs) == 0:
        return []

    # Ordenamos por score y suprimimos vecinos muy cercanos para evitar duplicados.
    raw = sorted(((float(res[y, x]), int(x), int(y)) for y, x in zip(ys, xs)), reverse=True)
    selected = []
    min_dist2 = max(4, (min(tw, th) // 2) ** 2)

    for score, x, y in raw:
        cx = x + tw // 2
        cy = y + th // 2
        too_close = False
        for _, sx, sy in selected:
            scx = sx + tw // 2
            scy = sy + th // 2
            if (cx - scx) ** 2 + (cy - scy) ** 2 <= min_dist2:
                too_close = True
                break
        if not too_close:
            selected.append((score, x, y))

    return selected



def _bot_worker(log_fn, state_fn):
    """
    Máquina de estados:
      ESCANEANDO → busca cajas cada 50 ms; al encontrar una hace click y pasa a VIAJANDO.
      VIAJANDO   → solo vigila si la caja desapareció (recogida) o si hubo timeout;
                   NO hace ningún otro click mientras viaja.
    """
    box1  = cv2.imread(BOX1_FILE)  if os.path.exists(BOX1_FILE)  else None
    box2  = cv2.imread(BOX2_FILE)  if os.path.exists(BOX2_FILE)  else None
    lumid = cv2.imread(LUMID_FILE) if os.path.exists(LUMID_FILE) else None
    elite = cv2.imread(ELITE_FILE) if os.path.exists(ELITE_FILE) else None
    re_t   = cv2.imread(RE_FILE)     if os.path.exists(RE_FILE)     else None
    re1_t  = cv2.imread(RE1_FILE)    if os.path.exists(RE1_FILE)    else None
    login  = cv2.imread(LOGIN_FILE)  if os.path.exists(LOGIN_FILE)  else None
    login1 = cv2.imread(LOGIN1_FILE) if os.path.exists(LOGIN1_FILE) else None

    if box1   is None: log_fn(f"⚠  {os.path.basename(BOX1_FILE)}   no encontrado — se omitirá.")
    if box2   is None: log_fn(f"⚠  {os.path.basename(BOX2_FILE)}   no encontrado — se omitirá.")
    if lumid  is None: log_fn(f"⚠  {os.path.basename(LUMID_FILE)}  no encontrado — se omitirá.")
    if elite  is None: log_fn(f"⚠  {os.path.basename(ELITE_FILE)}  no encontrado — se omitirá.")
    if re_t   is None: log_fn(f"⚠  {os.path.basename(RE_FILE)}     no encontrado — se omitirá.")
    if re1_t  is None: log_fn(f"⚠  {os.path.basename(RE1_FILE)}    no encontrado — se omitirá.")
    if login  is None: log_fn(f"⚠  {os.path.basename(LOGIN_FILE)}  no encontrado — se omitirá.")
    if login1 is None: log_fn(f"⚠  {os.path.basename(LOGIN1_FILE)} no encontrado — se omitirá.")
    if box1 is None and box2 is None and lumid is None and elite is None and re_t is None and re1_t is None:
        log_fn("✖  Sin imágenes de referencia. Agrega las imágenes junto al script.")
        return

    templates  = [(n, t) for n, t in
                  [("BOX1", box1), ("BOX2", box2), ("LUMID", lumid), ("ELITELUMID", elite)]
                  if t is not None]
    quit_ev    = _state["quit"]
    sw, sh     = pyautogui.size()
    scx, scy   = sw // 2, sh // 2   # centro = posición fija de la nave

    last_random_t = 0.0
    last_re_t     = 0.0
    RE_COOLDOWN   = 10.0  # segundos mínimos entre ejecuciones de la secuencia RE

    while not quit_ev.is_set():

        # ── Pausado ────────────────────────────────────────────────────────────
        if not _state["running"]:
            time.sleep(0.15)
            continue

        # ── Sin región configurada ─────────────────────────────────────────────
        region = _state["region"]
        if region is None:
            log_fn("ℹ  Selecciona una región de búsqueda con ⌖ Búsqueda.")
            time.sleep(1.0)
            continue

        # ── Captura ────────────────────────────────────────────────────────────
        try:
            img = _capture(region)
        except Exception as exc:
            log_fn(f"⚠  Error capturando pantalla: {exc}")
            time.sleep(SCAN_INTERVAL)
            continue

        x1, y1, x2, y2 = region
        w,  h          = x2 - x1, y2 - y1
        now            = time.time()

        # ══════════════════════════════════════════════════════════════════════
        #  PRIORIDAD 0 — LOGIN / LOGIN1
        #  Si se detecta alguna de estas pantallas → click directo y reiniciar ciclo
        # ══════════════════════════════════════════════════════════════════════
        login_clicked = False
        for lg_name, lg_tmpl in [("LOGIN", login), ("LOGIN1", login1)]:
            if lg_tmpl is None:
                continue
            lh, lw = lg_tmpl.shape[:2]
            if lw > w or lh > h:
                continue
            lg_matches = _find_template_matches(img, lg_tmpl, LOGIN_THRESHOLD)
            if lg_matches:
                score, mx, my = lg_matches[0]
                det_cx = x1 + mx + lw // 2
                det_cy = y1 + my + lh // 2
                coord_key = "login_click" if lg_name == "LOGIN" else "login1_click"
                custom = _state[coord_key]
                tx, ty = custom if custom is not None else (det_cx, det_cy)
                log_fn(f"✔  {lg_name} [conf={score:.2f}] → click ({tx},{ty})")
                _full_click(tx, ty)
                login_clicked = True
                time.sleep(0.5)  # pequeña pausa para que la pantalla cambie
                break
        if login_clicked:
            time.sleep(SCAN_INTERVAL)
            continue

        # ══════════════════════════════════════════════════════════════════════
        #  PRIORIDAD 1 — RE
        #  Si se detecta RE y pasó el cooldown → ejecutar secuencia completa
        # ══════════════════════════════════════════════════════════════════════
        if re_t is not None and (now - last_re_t) >= RE_COOLDOWN:
            re_matches = _find_template_matches(img, re_t, RE_THRESHOLD)
            if re_matches:
                score, mx, my = re_matches[0]
                th, tw = re_t.shape[:2]
                bcx = x1 + mx + tw // 2
                bcy = y1 + my + th // 2
                re_coord  = _state["re_click"]
                re1_coord = _state["re1_click"]
                log_fn(f"✔  RE [conf={score:.2f}] → esperando 5s…")
                time.sleep(5.0)
                tx, ty = re_coord if re_coord is not None else (bcx, bcy)
                log_fn(f"✔  RE → click ({tx},{ty})")
                _full_click(tx, ty)
                log_fn("✔  RE → esperando 2s antes de RE1…")
                time.sleep(2.0)
                if re1_coord is not None:
                    log_fn(f"✔  RE1 → click ({re1_coord[0]},{re1_coord[1]})")
                    _full_click(re1_coord[0], re1_coord[1])
                else:
                    log_fn("⚠  RE1 → sin coordenada definida, se omite.")
                last_re_t = time.time()
                time.sleep(SCAN_INTERVAL)
                continue

        # ══════════════════════════════════════════════════════════════════════
        #  PRIORIDAD 2 — BOX1 y demás imágenes
        #  Busca candidatos, elige el más cercano al centro y hace click.
        # ══════════════════════════════════════════════════════════════════════
        candidates = []
        for name, tmpl in templates:
            th, tw = tmpl.shape[:2]
            if tw > w or th > h:
                continue
            matches = _find_template_matches(img, tmpl, THRESHOLD)
            for score, mx, my in matches:
                cx = x1 + mx + tw // 2
                cy = y1 + my + th // 2
                candidates.append((name, tmpl, score, cx, cy))

        if candidates:
            ordered = sorted(candidates, key=lambda c: (c[3] - scx) ** 2 + (c[4] - scy) ** 2)
            bname, btmpl, bscore, bcx, bcy = ordered[0]
            dist = int(((bcx - scx) ** 2 + (bcy - scy) ** 2) ** 0.5)

            if bname in DOUBLE_CLICK_NAMES:
                tx, ty = bcx, bcy - 10
                log_fn(f"✔  {bname} [conf={bscore:.2f}] dist={dist}px → doble-click ({tx},{ty}) + Ctrl")
                _double_click(tx, ty)
                time.sleep(0.08)
                keyboard.press("ctrl")
                time.sleep(0.05)
                keyboard.release("ctrl")
            elif bname == "BOX1":
                log_fn(f"✔  BOX1 [conf={bscore:.2f}] dist={dist}px → doble-click ({bcx},{bcy})")
                _full_click(bcx, bcy)
            else:
                log_fn(f"✔  {bname} [conf={bscore:.2f}] dist={dist}px → click ({bcx},{bcy})")
                _full_click(bcx, bcy)

        else:
            # ══════════════════════════════════════════════════════════════════
            #  PRIORIDAD 3 — Click aleatorio en zona de movimiento
            # ══════════════════════════════════════════════════════════════════
            move = _state["move_region"]
            if move is not None and (now - last_random_t) >= RANDOM_DELAY:
                mx1, my1, mx2, my2 = move
                rx = random.randint(mx1, mx2)
                ry = random.randint(my1, my2)
                log_fn(f"~  Sin cajas — click aleatorio en ({rx},{ry})")
                _full_click(rx, ry)
                last_random_t = now
                time.sleep(0.35)

        # ── Ciclo a 1 ms ───────────────────────────────────────────────────────
        time.sleep(SCAN_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════════
#  APLICACIÓN  (GUI tkinter)
# ═══════════════════════════════════════════════════════════════════════════════

class App:
    # ── Paleta DarkOrbit 2006 ─────────────────────────────────────────────
    C_BG      = "#060608"    # space black
    C_PANEL   = "#0e0e12"    # dark steel
    C_PANEL2  = "#131318"    # lighter steel
    C_BORDER  = "#3a2800"    # dark orange border
    C_BORDER2 = "#cc5500"    # bright orange border
    C_ACCENT  = "#ff7700"    # DarkOrbit orange
    C_ACCENT2 = "#cc4400"    # dark orange
    C_ACCENT3 = "#4a1800"    # very dark orange (bg glow)
    C_GREEN   = "#44cc00"    # target green
    C_RED     = "#dd2200"    # danger red
    C_YELLOW  = "#ffcc00"    # gold/yellow
    C_STEEL   = "#8a9aaa"    # metallic text
    C_STEEL_D = "#3a4a58"    # dim steel
    C_FG      = "#c8a870"    # warm metallic text
    C_FG_DIM  = "#4a3820"    # dim warm text
    C_LOG_BG  = "#040406"    # near black
    C_GOLD    = "#ffcc00"    # alias
    C_AMBER   = "#ff7700"    # alias
    C_ORANGE  = "#ff5500"    # bright orange
    C_PURPLE  = "#8844cc"    # purple (login)

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("SPACE ACE BOT - TARGET ACQUISITION MODULE")
        self.root.geometry("560x900")
        self.root.minsize(560, 700)
        self.root.configure(bg=self.C_BG)
        self.root.resizable(False, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._overlay = None   # CenterOverlay (se crea al iniciar)

        self._build_ui()
        self._register_hotkey()
        self._load_config()

        # Arrancar el worker como hilo demonio
        _state["quit"].clear()
        threading.Thread(
            target=_bot_worker, args=(self._log, self._set_mode), daemon=True
        ).start()

    # ── Construcción interfaz estilo DarkOrbit 2006 ────────────────────────────
    def _build_ui(self):
        W   = 560
        PAD = 10
        r   = self.root

        # ── HEADER ──────────────────────────────────────────────────────────
        hdr = tk.Canvas(r, width=W, height=108, bg=self.C_BG, highlightthickness=0)
        hdr.pack(fill="x")
        # gradiente simulado
        for y0, y1, shade in [(0,36,"#180c00"),(36,72,"#100800"),(72,108,"#080400")]:
            hdr.create_rectangle(0, y0, W, y1, fill=shade, outline="")
        # bordes naranja
        hdr.create_line(0, 0,   W, 0,   fill=self.C_ACCENT,  width=3)
        hdr.create_line(0, 107, W, 107, fill=self.C_ACCENT2, width=2)
        hdr.create_line(8, 68, W-8, 68, fill=self.C_ACCENT3, width=1)
        # esquinas metálicas amarillas
        for pts in [(0,0,20,0),(0,0,0,20),(W-20,0,W,0),(W,0,W,20)]:
            hdr.create_line(*pts, fill=self.C_YELLOW, width=2)
        for pts in [(0,107,20,107),(0,87,0,107),(W-20,107,W,107),(W,87,W,107)]:
            hdr.create_line(*pts, fill=self.C_YELLOW, width=2)
        # título con sombra naranja
        hdr.create_text(W//2+2, 32, anchor="center",
                        text=">>> SPACE ACE BOT <<<",
                        fill="#1a0800", font=("Impact", 22))
        hdr.create_text(W//2, 30, anchor="center",
                        text=">>> SPACE ACE BOT <<<",
                        fill=self.C_YELLOW, font=("Impact", 22))
        hdr.create_text(W//2, 52, anchor="center",
                        text="TARGET  ACQUISITION  MODULE  //  v3.0",
                        fill=self.C_ACCENT2, font=("Consolas", 8, "bold"))
        hdr.create_rectangle(W//2-82, 62, W//2+82, 78,
                             fill="#1e0a00", outline=self.C_ACCENT2)
        hdr.create_text(W//2, 70, anchor="center",
                        text=" [  P  ]  START / PAUSE ",
                        fill=self.C_ACCENT, font=("Consolas", 8, "bold"))
        # scanlines
        for y in range(3, 108, 6):
            hdr.create_line(0, y, W, y, fill="#0a0500", width=1)

        # ── STATUS ──────────────────────────────────────────────────────────
        self._do_section(r, "STATUS DEL SISTEMA", PAD)
        card_st = self._do_card(r, PAD)

        left_st = tk.Frame(card_st, bg=self.C_PANEL, width=40)
        left_st.pack(side="left", fill="y")
        left_st.pack_propagate(False)
        self._dot = tk.Canvas(left_st, width=16, height=16,
                              bg=self.C_PANEL, highlightthickness=0)
        self._dot.pack(expand=True)
        self._dot_oval = self._dot.create_oval(2, 2, 14, 14,
                                               fill=self.C_RED, outline=self.C_ACCENT2)
        self._blink_job = None
        self._blink_on  = False

        tk.Frame(card_st, bg=self.C_BORDER2, width=1).pack(side="left", fill="y")
        right_st = tk.Frame(card_st, bg=self.C_PANEL)
        right_st.pack(side="left", fill="both", expand=True, padx=10, pady=6)
        rs1 = tk.Frame(right_st, bg=self.C_PANEL); rs1.pack(fill="x", pady=2)
        tk.Label(rs1, text="ESTADO :", bg=self.C_PANEL, fg=self.C_FG_DIM,
                 font=("Consolas", 8, "bold"), width=9, anchor="w").pack(side="left")
        self.lbl_status = tk.Label(rs1, text="[ STAND-BY ]",
                                   bg=self.C_PANEL, fg=self.C_RED,
                                   font=("Consolas", 11, "bold"))
        self.lbl_status.pack(side="left")
        rs2 = tk.Frame(right_st, bg=self.C_PANEL); rs2.pack(fill="x", pady=2)
        tk.Label(rs2, text="MODO   :", bg=self.C_PANEL, fg=self.C_FG_DIM,
                 font=("Consolas", 8, "bold"), width=9, anchor="w").pack(side="left")
        self.lbl_mode = tk.Label(rs2, text=STATE_SCANNING,
                                 bg=self.C_PANEL, fg=self.C_ACCENT,
                                 font=("Consolas", 11, "bold"))
        self.lbl_mode.pack(side="left")

        # ── ZONAS ──────────────────────────────────────────────────────────
        self._do_section(r, "ZONAS DE OPERACION", PAD)
        card_zn = self._do_card(r, PAD)
        z1 = tk.Frame(card_zn, bg=self.C_PANEL); z1.pack(fill="x", padx=8, pady=(6,3))
        self._do_badge(z1, "SCN", self.C_ACCENT)
        tk.Label(z1, text="BUSQUEDA", bg=self.C_PANEL, fg=self.C_STEEL_D,
                 font=("Consolas", 8, "bold"), width=10, anchor="w").pack(side="left", padx=4)
        self.lbl_region = tk.Label(z1, text=">>> sin definir",
                                   bg=self.C_PANEL, fg=self.C_YELLOW, font=("Consolas", 9))
        self.lbl_region.pack(side="left")
        tk.Frame(card_zn, bg=self.C_BORDER, height=1).pack(fill="x", padx=4)
        z2 = tk.Frame(card_zn, bg=self.C_PANEL2); z2.pack(fill="x", padx=8, pady=(3,6))
        self._do_badge(z2, "MOV", self.C_GREEN)
        tk.Label(z2, text="MOVIMIENTO", bg=self.C_PANEL2, fg=self.C_STEEL_D,
                 font=("Consolas", 8, "bold"), width=10, anchor="w").pack(side="left", padx=4)
        self.lbl_move = tk.Label(z2, text=">>> sin definir",
                                 bg=self.C_PANEL2, fg=self.C_GREEN, font=("Consolas", 9))
        self.lbl_move.pack(side="left")

        # ── COORDENADAS ────────────────────────────────────────────────────
        self._do_section(r, "COORDENADAS DE CLICK", PAD)
        card_co = self._do_card(r, PAD)
        coord_defs = [
            ("LOGIN",  self.C_PURPLE, "lbl_login_click",  "LGN"),
            ("LOGIN1", self.C_PURPLE, "lbl_login1_click", "LG1"),
            ("RE",     self.C_ORANGE, "lbl_re_click",     " RE"),
            ("RE1",    self.C_ORANGE, "lbl_re1_click",    "RE1"),
        ]
        for i, (name, color, attr, badge_txt) in enumerate(coord_defs):
            bg = self.C_PANEL if i % 2 == 0 else self.C_PANEL2
            inner = tk.Frame(card_co, bg=bg)
            inner.pack(fill="x")
            row_in = tk.Frame(inner, bg=bg)
            row_in.pack(fill="x", padx=8, pady=4)
            self._do_badge(row_in, badge_txt, color)
            tk.Label(row_in, text=name, bg=bg, fg=self.C_FG_DIM,
                     font=("Consolas", 9, "bold"), width=8, anchor="w").pack(side="left", padx=4)
            lbl = tk.Label(row_in, text=">>> sin definir", bg=bg, fg=color, font=("Consolas", 9))
            lbl.pack(side="left")
            setattr(self, attr, lbl)
            if i < len(coord_defs) - 1:
                tk.Frame(card_co, bg=self.C_BORDER, height=1).pack(fill="x")

        # ── CONTROLES ──────────────────────────────────────────────────────
        self._do_section(r, "CONTROLES", PAD)

        # Botón INICIAR (ancho completo)
        self.btn_toggle = tk.Button(
            r, text=">>>  INICIAR  [ P ]  <<<",
            bg=self.C_ACCENT3, fg=self.C_YELLOW,
            relief="flat", bd=0, padx=6, pady=11,
            cursor="hand2", font=("Impact", 13),
            command=self.toggle,
            activebackground=self.C_ACCENT, activeforeground="#000",
        )
        self.btn_toggle.pack(fill="x", padx=PAD, pady=(0, 4))
        self.btn_toggle.bind("<Enter>", lambda e: self.btn_toggle.config(
            bg=self.C_ACCENT, fg="#000000"))
        self.btn_toggle.bind("<Leave>", lambda e: self.btn_toggle.config(
            bg=self.C_ACCENT3, fg=self.C_YELLOW))

        # Fila: BUSQUEDA + MOVIMIENTO
        fr1 = tk.Frame(r, bg=self.C_BG)
        fr1.pack(fill="x", padx=PAD, pady=(0, 4))
        fr1.columnconfigure((0, 1), weight=1, uniform="b")
        self._do_btn_g(fr1, "[  BUSQUEDA  ]",   self.C_ACCENT, self.C_ACCENT3, self._select_region,      0, 0)
        self._do_btn_g(fr1, "[  MOVIMIENTO  ]", self.C_GREEN,  "#001a08",       self._select_move_region, 0, 1)

        # Fila: CLICK RE + CLICK RE1
        fr2 = tk.Frame(r, bg=self.C_BG)
        fr2.pack(fill="x", padx=PAD, pady=(0, 4))
        fr2.columnconfigure((0, 1), weight=1, uniform="b")
        self._do_btn_g(fr2, "[  CLICK RE  ]",  self.C_ORANGE, "#1e0400", self._select_re_click,  0, 0)
        self._do_btn_g(fr2, "[  CLICK RE1  ]", self.C_ORANGE, "#1e0400", self._select_re1_click, 0, 1)

        # Fila: CLICK LOGIN + CLICK LOGIN1
        fr3 = tk.Frame(r, bg=self.C_BG)
        fr3.pack(fill="x", padx=PAD, pady=(0, 8))
        fr3.columnconfigure((0, 1), weight=1, uniform="b")
        self._do_btn_g(fr3, "[  LOGIN  ]",  self.C_PURPLE, "#0e0018", self._select_login_click,  0, 0)
        self._do_btn_g(fr3, "[  LOGIN1  ]", self.C_PURPLE, "#0e0018", self._select_login1_click, 0, 1)

        # ── LOG ──────────────────────────────────────────────────────────────
        log_outer = tk.Frame(r, bg=self.C_BORDER2)
        log_outer.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))
        log_title = tk.Frame(log_outer, bg=self.C_ACCENT3)
        log_title.pack(fill="x")
        tk.Label(log_title, text=" >>  SYSTEM LOG  //  COMBAT TELEMETRY  ",
                 bg=self.C_ACCENT3, fg=self.C_YELLOW,
                 font=("Consolas", 8, "bold")).pack(side="left", padx=4, pady=2)
        frm_lg = tk.Frame(log_outer, bg=self.C_LOG_BG)
        frm_lg.pack(fill="both", expand=True)
        self.txt = tk.Text(
            frm_lg, bg=self.C_LOG_BG, fg=self.C_FG,
            font=("Consolas", 8), state="disabled",
            wrap="word", relief="flat", bd=0,
            insertbackground=self.C_FG,
            selectbackground="#2a1800",
        )
        self.txt.tag_config("ok",   foreground=self.C_GREEN)
        self.txt.tag_config("warn", foreground=self.C_YELLOW)
        self.txt.tag_config("err",  foreground=self.C_RED)
        self.txt.tag_config("info", foreground=self.C_ACCENT)
        self.txt.tag_config("ts",   foreground=self.C_FG_DIM)
        sb = ttk.Scrollbar(frm_lg, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(fill="both", expand=True, padx=6, pady=6)
        self._log("ℹ  Sistema listo. Configura zona de busqueda y presiona P.")

    # ── DarkOrbit UI Helpers ────────────────────────────────────────────────────
    def _do_section(self, parent, text, pad=10):
        """DarkOrbit section header: triángulo naranja + texto + línea."""
        f = tk.Frame(parent, bg=self.C_BG)
        f.pack(fill="x", padx=pad, pady=(10, 2))
        tri = tk.Canvas(f, width=10, height=14, bg=self.C_BG, highlightthickness=0)
        tri.pack(side="left")
        tri.create_polygon(0,14, 0,2, 10,8, fill=self.C_ACCENT, outline="")
        tk.Label(f, text=f" {text} ",
                 bg=self.C_BG, fg=self.C_ACCENT,
                 font=("Consolas", 9, "bold")).pack(side="left")
        tk.Canvas(f, height=1, bg=self.C_ACCENT2, highlightthickness=0
                  ).pack(side="left", fill="x", expand=True)

    def _do_card(self, parent, pad=10):
        """DarkOrbit panel: borde naranja + fondo acero."""
        outer = tk.Frame(parent, bg=self.C_BORDER2)
        outer.pack(fill="x", padx=pad, pady=(0, 3))
        inner = tk.Frame(outer, bg=self.C_PANEL, padx=1, pady=1)
        inner.pack(fill="x", padx=1, pady=1)
        return inner

    def _do_badge(self, parent, text, color):
        """DarkOrbit badge: chip angular coloreado."""
        r = int(color[1:3], 16) // 5
        g = int(color[3:5], 16) // 5
        b = int(color[5:7], 16) // 5
        bg_dark = f"#{r:02x}{g:02x}{b:02x}"
        c = tk.Canvas(parent, width=34, height=18, bg=bg_dark,
                      highlightthickness=1, highlightbackground=color)
        c.pack(side="left", padx=(0, 3))
        c.create_text(17, 9, text=text, fill=color,
                      font=("Consolas", 7, "bold"), anchor="center")

    def _do_btn_g(self, parent, text, fg_color, bg_dark, cmd, row, col):
        """DarkOrbit button: acero con relleno naranja al hover."""
        btn = tk.Button(
            parent, text=text,
            bg=bg_dark, fg=fg_color,
            relief="flat", bd=0, padx=6, pady=9,
            cursor="hand2", font=("Consolas", 9, "bold"),
            command=cmd,
            activebackground=fg_color, activeforeground="#000000",
        )
        btn.grid(row=row, column=col, sticky="ew", padx=2, pady=2)
        btn.bind("<Enter>", lambda e, b=btn, c=fg_color:  b.config(bg=c, fg="#000000"))
        btn.bind("<Leave>", lambda e, b=btn, c=bg_dark, fc=fg_color: b.config(bg=c, fg=fc))
        return btn

    # ── Blink animation ───────────────────────────────────────────────────────
    def _start_blink(self):
        self._blink_on = True
        self._blink_tick()

    def _stop_blink(self):
        if self._blink_job:
            self.root.after_cancel(self._blink_job)
            self._blink_job = None
        self._dot.itemconfig(self._dot_oval, fill=self.C_RED)

    def _blink_tick(self):
        color = self.C_GREEN if self._blink_on else self.C_ACCENT3
        self._dot.itemconfig(self._dot_oval, fill=color)
        self._blink_on = not self._blink_on
        self._blink_job = self.root.after(600, self._blink_tick)

    # ── Tecla P – hotkey global ────────────────────────────────────────────────
    def _register_hotkey(self):
        try:
            keyboard.add_hotkey("p", lambda: self.root.after(0, self.toggle))
        except Exception as exc:
            self._log(f"⚠  No se pudo registrar hotkey global: {exc}")
            self._log("   Usa el botón ▶ INICIAR para arrancar el bot.")

    # ── Callback de estado del worker ────────────────────────────────────────
    def _set_mode(self, mode: str):
        def _upd():
            if mode == STATE_TRAVELING:
                self.lbl_mode.config(text=STATE_TRAVELING, fg=self.C_GOLD)
            else:
                self.lbl_mode.config(text=STATE_SCANNING, fg=self.C_ACCENT)
        self.root.after(0, _upd)

    # ── Toggle Iniciar / Pausar ────────────────────────────────────────────────
    def toggle(self):
        if not _state["running"] and _state["region"] is None:
            self._log("⚠  Primero selecciona una región con el botón ⌖.")
            return
        _state["running"] = not _state["running"]
        if _state["running"]:
            self.lbl_status.config(text="[ ACTIVO ]",  fg=self.C_GREEN)
            self.btn_toggle.config(
                text=">>>  PAUSAR  [ P ]  <<<",
                bg=self.C_RED, fg=self.C_YELLOW)
            self.btn_toggle.bind("<Enter>", lambda e: self.btn_toggle.config(
                bg="#ff4400", fg="#000000"))
            self.btn_toggle.bind("<Leave>", lambda e: self.btn_toggle.config(
                bg=self.C_RED, fg=self.C_YELLOW))
            self._log("▶  Bot iniciado.")
            self._start_blink()
            if self._overlay is None:
                self._overlay = CenterOverlay(self.root)
        else:
            self.lbl_status.config(text="[ STAND-BY ]", fg=self.C_RED)
            self.btn_toggle.config(
                text=">>>  INICIAR  [ P ]  <<<",
                bg=self.C_ACCENT3, fg=self.C_YELLOW)
            self.btn_toggle.bind("<Enter>", lambda e: self.btn_toggle.config(
                bg=self.C_ACCENT, fg="#000000"))
            self.btn_toggle.bind("<Leave>", lambda e: self.btn_toggle.config(
                bg=self.C_ACCENT3, fg=self.C_YELLOW))
            self._log("⏸  Bot pausado.")
            self._stop_blink()
            # Ocultar cruz de centro
            if self._overlay is not None:
                self._overlay.destroy()
                self._overlay = None
            self._set_mode(STATE_SCANNING)

    # ── Selección de región de búsqueda ─────────────────────────────────────────
    def _select_region(self):
        self.root.iconify()
        self.root.after(300, lambda: RegionSelector(
            self._on_region, label="Área de BÚSQUEDA (BOX1/BOX2)", color="#ff4444"
        ))

    def _on_region(self, region):
        _state["region"] = region
        x1, y1, x2, y2 = region
        info = f"({x1},{y1}) → ({x2},{y2})  [{x2-x1} × {y2-y1} px]"
        self.lbl_region.config(text=info)
        self._log(f"ℹ  Región de búsqueda: {info}")
        self._save_config()
        self.root.deiconify()

    # ── Selección de área de movimiento ──────────────────────────────────────────
    def _select_move_region(self):
        self.root.iconify()
        self.root.after(300, lambda: RegionSelector(
            self._on_move_region, label="Área de MOVIMIENTO (click aleatorio)", color="#27ae60"
        ))

    def _on_move_region(self, region):
        _state["move_region"] = region
        x1, y1, x2, y2 = region
        info = f"({x1},{y1}) → ({x2},{y2})  [{x2-x1} × {y2-y1} px]"
        self.lbl_move.config(text=info)
        self._log(f"ℹ  Área de movimiento: {info}")
        self._save_config()
        self.root.deiconify()

    # ── Selección de coordenada de click para RE ──────────────────────────────
    def _select_re_click(self):
        self.root.iconify()
        self.root.after(300, lambda: PointSelector(
            self._on_re_click,
            label="COORD CLICK  RE  (re.png)",
            color="#e67e22",
        ))

    def _on_re_click(self, coord):
        _state["re_click"] = coord
        x, y = coord
        self.lbl_re_click.config(text=f"({x}, {y})")
        self._log(f"ℹ  Click RE → coordenada fija: ({x}, {y})")
        self._save_config()
        self.root.deiconify()

    # ── Selección de coordenada de click para RE1 ─────────────────────────────
    def _select_re1_click(self):
        self.root.iconify()
        self.root.after(300, lambda: PointSelector(
            self._on_re1_click,
            label="COORD CLICK  RE1  (re1.png)",
            color="#e67e22",
        ))

    def _on_re1_click(self, coord):
        _state["re1_click"] = coord
        x, y = coord
        self.lbl_re1_click.config(text=f"({x}, {y})")
        self._log(f"ℹ  Click RE1 → coordenada fija: ({x}, {y})")
        self._save_config()
        self.root.deiconify()

    # ── Selección de coordenada de click para LOGIN ──────────────────────────
    def _select_login_click(self):
        self.root.iconify()
        self.root.after(300, lambda: PointSelector(
            self._on_login_click,
            label="COORD CLICK  LOGIN  (login.png)",
            color="#8e44ad",
        ))

    def _on_login_click(self, coord):
        _state["login_click"] = coord
        x, y = coord
        self.lbl_login_click.config(text=f"({x}, {y})")
        self._log(f"ℹ  Click LOGIN → coordenada fija: ({x}, {y})")
        self._save_config()
        self.root.deiconify()

    # ── Selección de coordenada de click para LOGIN1 ──────────────────────────
    def _select_login1_click(self):
        self.root.iconify()
        self.root.after(300, lambda: PointSelector(
            self._on_login1_click,
            label="COORD CLICK  LOGIN1  (login1.png)",
            color="#8e44ad",
        ))

    def _on_login1_click(self, coord):
        _state["login1_click"] = coord
        x, y = coord
        self.lbl_login1_click.config(text=f"({x}, {y})")
        self._log(f"ℹ  Click LOGIN1 → coordenada fija: ({x}, {y})")
        self._save_config()
        self.root.deiconify()

    # ── Guardar / Cargar configuración ──────────────────────────────────────────
    def _save_config(self):
        data = {}
        if _state["region"]:
            data["region"] = list(_state["region"])
        if _state["move_region"]:
            data["move_region"] = list(_state["move_region"])
        if _state["re_click"]:
            data["re_click"] = list(_state["re_click"])
        if _state["re1_click"]:
            data["re1_click"] = list(_state["re1_click"])
        if _state["login_click"]:
            data["login_click"] = list(_state["login_click"])
        if _state["login1_click"]:
            data["login1_click"] = list(_state["login1_click"])
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            self._log(f"⚠  No se pudo guardar config: {exc}")

    def _load_config(self):
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if "region" in data:
            r = tuple(data["region"])
            _state["region"] = r
            x1, y1, x2, y2 = r
            self.lbl_region.config(text=f"({x1},{y1}) → ({x2},{y2})  [{x2-x1} × {y2-y1} px]")
        if "move_region" in data:
            r = tuple(data["move_region"])
            _state["move_region"] = r
            x1, y1, x2, y2 = r
            self.lbl_move.config(text=f"({x1},{y1}) → ({x2},{y2})  [{x2-x1} × {y2-y1} px]")
        if "re_click" in data:
            c = tuple(data["re_click"])
            _state["re_click"] = c
            self.lbl_re_click.config(text=f"({c[0]}, {c[1]})")
        if "re1_click" in data:
            c = tuple(data["re1_click"])
            _state["re1_click"] = c
            self.lbl_re1_click.config(text=f"({c[0]}, {c[1]})")
        if "login_click" in data:
            c = tuple(data["login_click"])
            _state["login_click"] = c
            self.lbl_login_click.config(text=f"({c[0]}, {c[1]})")
        if "login1_click" in data:
            c = tuple(data["login1_click"])
            _state["login1_click"] = c
            self.lbl_login1_click.config(text=f"({c[0]}, {c[1]})")
        self._log("ℹ  Configuración cargada desde config.json")

    # ── Log ────────────────────────────────────────────────────────────────────
    def _log(self, msg: str):
        def _write():
            t = self.txt
            t.configure(state="normal")
            ts = f"[{time.strftime('%H:%M:%S')}] "
            t.insert("end", ts, "ts")
            first = msg.lstrip()[:1]
            if first in ("✔", "▶"):
                tag = "ok"
            elif first in ("⚠", "~"):
                tag = "warn"
            elif first in ("✖", "⏸"):
                tag = "err"
            elif first == "ℹ":
                tag = "info"
            else:
                tag = ""
            t.insert("end", msg + "\n", tag)
            total = int(t.index("end-1c").split(".")[0])
            if total > MAX_LOG_ROWS:
                t.delete("1.0", f"{total - MAX_LOG_ROWS}.0")
            t.configure(state="disabled")
            t.see("end")
        self.root.after(0, _write)

    # ── Cierre limpio ──────────────────────────────────────────────────────────
    def _on_close(self):
        self._save_config()
        _state["quit"].set()
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    App().run()
