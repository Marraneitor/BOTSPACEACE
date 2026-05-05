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

# Rutas de las imágenes de referencia  (misma carpeta que el script)
_HERE        = os.path.dirname(os.path.abspath(__file__))
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
    # ── Paleta de colores (tema oscuro) ──
    C_BG     = "#1e1e2e"
    C_PANEL  = "#2a2a3d"
    C_ACCENT = "#7c6af7"
    C_GREEN  = "#50fa7b"
    C_RED    = "#ff5555"
    C_GOLD   = "#f1c40f"
    C_FG     = "#cdd6f4"
    C_LOG_BG = "#0f0f1a"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("BOT — BOX1 / BOX2 Finder")
        self.root.configure(bg=self.C_BG)
        self.root.resizable(False, False)
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

    # ── Construcción de la interfaz ────────────────────────────────────────────
    def _build_ui(self):
        PAD = 12
        r   = self.root

        # Título
        tk.Label(
            r, text="⬛  BOX FINDER BOT",
            bg=self.C_BG, fg=self.C_ACCENT,
            font=("Consolas", 16, "bold"),
        ).pack(pady=(PAD, 2))

        tk.Label(
            r, text='Presiona  "P"  para Iniciar o Pausar el bot',
            bg=self.C_BG, fg=self.C_FG,
            font=("Consolas", 9),
        ).pack(pady=(0, PAD))

        # ── Estado (corriendo/pausado) ────────────────────────────────────────
        frm_st = tk.Frame(r, bg=self.C_PANEL)
        frm_st.pack(fill="x", padx=PAD, pady=2)
        tk.Label(
            frm_st, text="Estado :", bg=self.C_PANEL, fg=self.C_FG,
            font=("Consolas", 10), width=10, anchor="w",
        ).pack(side="left", padx=8, pady=6)
        self.lbl_status = tk.Label(
            frm_st, text="⏸  PAUSADO",
            bg=self.C_PANEL, fg=self.C_RED,
            font=("Consolas", 10, "bold"),
        )
        self.lbl_status.pack(side="left")

        # ── Modo (ESCANEANDO / VIAJANDO) ──────────────────────────────────────
        frm_md = tk.Frame(r, bg=self.C_PANEL)
        frm_md.pack(fill="x", padx=PAD, pady=2)
        tk.Label(
            frm_md, text="Modo    :", bg=self.C_PANEL, fg=self.C_FG,
            font=("Consolas", 10), width=10, anchor="w",
        ).pack(side="left", padx=8, pady=6)
        self.lbl_mode = tk.Label(
            frm_md, text=STATE_SCANNING,
            bg=self.C_PANEL, fg=self.C_ACCENT,
            font=("Consolas", 10, "bold"),
        )
        self.lbl_mode.pack(side="left")

        # ── Región de búsqueda ────────────────────────────────────────────────
        frm_rg = tk.Frame(r, bg=self.C_PANEL)
        frm_rg.pack(fill="x", padx=PAD, pady=2)
        tk.Label(
            frm_rg, text="Búsqueda :", bg=self.C_PANEL, fg=self.C_FG,
            font=("Consolas", 10), width=11, anchor="w",
        ).pack(side="left", padx=8, pady=6)
        self.lbl_region = tk.Label(
            frm_rg, text="Sin seleccionar",
            bg=self.C_PANEL, fg=self.C_GOLD,
            font=("Consolas", 10),
        )
        self.lbl_region.pack(side="left")

        # ── Área de movimiento ────────────────────────────────────────────────
        frm_mv = tk.Frame(r, bg=self.C_PANEL)
        frm_mv.pack(fill="x", padx=PAD, pady=2)
        tk.Label(
            frm_mv, text="Movimiento:", bg=self.C_PANEL, fg=self.C_FG,
            font=("Consolas", 10), width=11, anchor="w",
        ).pack(side="left", padx=8, pady=6)
        self.lbl_move = tk.Label(
            frm_mv, text="Sin seleccionar (opcional)",
            bg=self.C_PANEL, fg="#27ae60",
            font=("Consolas", 10),
        )
        self.lbl_move.pack(side="left")

        # ── Coord click LOGIN ──────────────────────────────────────────────────
        frm_login = tk.Frame(r, bg=self.C_PANEL)
        frm_login.pack(fill="x", padx=PAD, pady=2)
        tk.Label(
            frm_login, text="Click LOGIN:", bg=self.C_PANEL, fg=self.C_FG,
            font=("Consolas", 10), width=11, anchor="w",
        ).pack(side="left", padx=8, pady=6)
        self.lbl_login_click = tk.Label(
            frm_login, text="Sin seleccionar (opcional)",
            bg=self.C_PANEL, fg="#8e44ad",
            font=("Consolas", 10),
        )
        self.lbl_login_click.pack(side="left")

        # ── Coord click LOGIN1 ────────────────────────────────────────────────
        frm_login1 = tk.Frame(r, bg=self.C_PANEL)
        frm_login1.pack(fill="x", padx=PAD, pady=2)
        tk.Label(
            frm_login1, text="Click LOGIN1:", bg=self.C_PANEL, fg=self.C_FG,
            font=("Consolas", 10), width=11, anchor="w",
        ).pack(side="left", padx=8, pady=6)
        self.lbl_login1_click = tk.Label(
            frm_login1, text="Sin seleccionar (opcional)",
            bg=self.C_PANEL, fg="#8e44ad",
            font=("Consolas", 10),
        )
        self.lbl_login1_click.pack(side="left")

        # ── Coord click RE ────────────────────────────────────────────────────
        frm_re = tk.Frame(r, bg=self.C_PANEL)
        frm_re.pack(fill="x", padx=PAD, pady=2)
        tk.Label(
            frm_re, text="Click RE  :", bg=self.C_PANEL, fg=self.C_FG,
            font=("Consolas", 10), width=11, anchor="w",
        ).pack(side="left", padx=8, pady=6)
        self.lbl_re_click = tk.Label(
            frm_re, text="Sin seleccionar (opcional)",
            bg=self.C_PANEL, fg="#e67e22",
            font=("Consolas", 10),
        )
        self.lbl_re_click.pack(side="left")

        # ── Coord click RE1 ───────────────────────────────────────────────────
        frm_re1 = tk.Frame(r, bg=self.C_PANEL)
        frm_re1.pack(fill="x", padx=PAD, pady=2)
        tk.Label(
            frm_re1, text="Click RE1 :", bg=self.C_PANEL, fg=self.C_FG,
            font=("Consolas", 10), width=11, anchor="w",
        ).pack(side="left", padx=8, pady=6)
        self.lbl_re1_click = tk.Label(
            frm_re1, text="Sin seleccionar (opcional)",
            bg=self.C_PANEL, fg="#e67e22",
            font=("Consolas", 10),
        )
        self.lbl_re1_click.pack(side="left")

        # ── Botones ───────────────────────────────────────────────────────────
        frm_bt = tk.Frame(r, bg=self.C_BG)
        frm_bt.pack(fill="x", padx=PAD, pady=10)

        btn_kw = dict(
            relief="flat", bd=0, padx=14, pady=7,
            cursor="hand2", font=("Consolas", 9, "bold"),
        )
        self.btn_toggle = tk.Button(
            frm_bt, text="▶  INICIAR  (P)",
            bg=self.C_GREEN, fg=self.C_BG,
            command=self.toggle, **btn_kw,
        )
        self.btn_toggle.pack(side="left", padx=(0, 8))

        tk.Button(
            frm_bt, text="⌖  Búsqueda",
            bg=self.C_ACCENT, fg="white",
            command=self._select_region, **btn_kw,
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            frm_bt, text="⌖  Movimiento",
            bg="#27ae60", fg="white",
            command=self._select_move_region, **btn_kw,
        ).pack(side="left")

        # ── Botones de coordenada personalizada ───────────────────────────────
        frm_bt2 = tk.Frame(r, bg=self.C_BG)
        frm_bt2.pack(fill="x", padx=PAD, pady=(0, 6))

        tk.Button(
            frm_bt2, text="⌖  Click RE",
            bg="#e67e22", fg="white",
            command=self._select_re_click, **btn_kw,
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            frm_bt2, text="⌖  Click RE1",
            bg="#e67e22", fg="white",
            command=self._select_re1_click, **btn_kw,
        ).pack(side="left")

        # ── Botones de coordenada LOGIN ─────────────────────────────────────
        frm_bt3 = tk.Frame(r, bg=self.C_BG)
        frm_bt3.pack(fill="x", padx=PAD, pady=(0, 6))

        tk.Button(
            frm_bt3, text="⌖  Click LOGIN",
            bg="#8e44ad", fg="white",
            command=self._select_login_click, **btn_kw,
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            frm_bt3, text="⌖  Click LOGIN1",
            bg="#8e44ad", fg="white",
            command=self._select_login1_click, **btn_kw,
        ).pack(side="left")

        # ── Log ───────────────────────────────────────────────────────────────
        frm_lg = tk.Frame(r, bg=self.C_PANEL, bd=1, relief="sunken")
        frm_lg.pack(fill="both", expand=True, padx=PAD, pady=(4, PAD))

        tk.Label(
            frm_lg, text=" Registro de actividad ",
            bg=self.C_PANEL, fg=self.C_FG,
            font=("Consolas", 8),
        ).pack(anchor="w")

        self.txt = tk.Text(
            frm_lg, width=58, height=15,
            bg=self.C_LOG_BG, fg=self.C_FG,
            font=("Consolas", 8), state="disabled",
            wrap="word", relief="flat", bd=0,
            insertbackground=self.C_FG,
        )
        sb = ttk.Scrollbar(frm_lg, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self._log("Bot listo.  Selecciona una región con ⌖ y presiona P.")
        self._log(f"Cajas (click): {os.path.basename(BOX1_FILE)}, {os.path.basename(BOX2_FILE)}")
        self._log(f"Lumid (doble-click +10px): {os.path.basename(LUMID_FILE)}, {os.path.basename(ELITE_FILE)}")

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
            self.lbl_status.config(text="▶  ACTIVO",  fg=self.C_GREEN)
            self.btn_toggle.config(text="⏸  PAUSAR  (P)", bg=self.C_RED, fg="white")
            self._log("▶  Bot iniciado.")
            # Mostrar cruz de centro
            if self._overlay is None:
                self._overlay = CenterOverlay(self.root)
        else:
            self.lbl_status.config(text="⏸  PAUSADO", fg=self.C_RED)
            self.btn_toggle.config(text="▶  INICIAR  (P)", bg=self.C_GREEN, fg=self.C_BG)
            self._log("⏸  Bot pausado.")
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
            t.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
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
