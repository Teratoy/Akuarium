#!/usr/bin/env python3
"""Akuarium – transparent-window aquarium simulator."""

import glob
import math
import os
import random
import sys

from PyQt6.QtCore import Qt, QTimer, QEvent, QRectF, QSize, QFileSystemWatcher
from PyQt6.QtGui import (
    QPixmap, QPainter, QKeyEvent, QCursor, QColor, QImage,
    QLinearGradient, QPainterPath, QPen, QIcon, QSurfaceFormat,
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QSlider, QGroupBox, QFormLayout, QSpinBox, QDoubleSpinBox,
    QPushButton, QGraphicsBlurEffect, QGraphicsScene,
    QGraphicsPixmapItem, QTabWidget, QScrollArea, QGridLayout,
    QToolButton, QFrame, QSizePolicy, QDialog, QButtonGroup,
)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FLORA_DIR = os.path.join(BASE_DIR, "Flora")
FISH_DIR = os.path.join(BASE_DIR, "Fish")
STARFISH_DIR = os.path.join(BASE_DIR, "Starfish")
KRUSTACEAN_DIR = os.path.join(BASE_DIR, "Krustaceans")
GROUND_DIR = os.path.join(BASE_DIR, "Ground")
RELIK_DIR = os.path.join(BASE_DIR, "Reliks")
BACKGROUND_DIR = os.path.join(BASE_DIR, "backgrounds")
EFFECTS_DIR = os.path.join(BASE_DIR, "Effects")
BUBBLE_PATH = os.path.join(EFFECTS_DIR, "bubble.png")
BACKGROUND_GLOBS = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp")

FPS = 60
TICK = 1000 // FPS


def _configure_gpu_surface() -> None:
    """Prefer a desktop-OpenGL surface with alpha so QPainter runs on the GPU."""
    fmt = QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
    fmt.setVersion(2, 1)
    fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    fmt.setSamples(0)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    fmt.setSwapInterval(1)  # vsync — avoids burning CPU/GPU past the display refresh
    QSurfaceFormat.setDefaultFormat(fmt)


def _alpha_mask(pixmap: QPixmap) -> QImage:
    """Cache an ARGB image once so hit-tests never re-convert pixmaps."""
    return pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _alpha_at(mask: QImage, x: int, y: int) -> int:
    return mask.pixelColor(x, y).alpha()


def _pin_below_x11(win_id: int) -> None:
    """Ask the window manager to keep this X11 window under all others."""
    try:
        from Xlib import X, display
        from Xlib.protocol import event
    except Exception:
        return
    try:
        dpy = display.Display()
        win = dpy.create_resource_object("window", int(win_id))
        root = dpy.screen().root
        net_state = dpy.intern_atom("_NET_WM_STATE")
        below = dpy.intern_atom("_NET_WM_STATE_BELOW")
        skip_task = dpy.intern_atom("_NET_WM_STATE_SKIP_TASKBAR")
        skip_pager = dpy.intern_atom("_NET_WM_STATE_SKIP_PAGER")
        sticky = dpy.intern_atom("_NET_WM_STATE_STICKY")
        mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask

        def add_state(a, b=0):
            ev = event.ClientMessage(
                window=win,
                client_type=net_state,
                data=(32, [1, a, b, 1, 0]),
            )
            root.send_event(ev, event_mask=mask)

        add_state(below, skip_task)
        add_state(skip_pager, sticky)
        net_desktop = dpy.intern_atom("_NET_WM_DESKTOP")
        win.change_property(net_desktop, dpy.intern_atom("CARDINAL"), 32, [0xFFFFFFFF])
        dpy.flush()
        dpy.close()
    except Exception:
        pass


def _clear_kwin_blur(win_id: int) -> None:
    """Drop any KWin blur region (it caused sliced corners)."""
    try:
        from Xlib import display
        dpy = display.Display()
        win = dpy.create_resource_object("window", int(win_id))
        atom = dpy.intern_atom("_KDE_NET_WM_BLUR_BEHIND_REGION")
        win.delete_property(atom)
        dpy.flush()
        dpy.close()
    except Exception:
        pass


def _blur_pixmap(src: QPixmap, radius: float = 18) -> QPixmap:
    if src.isNull():
        return src
    item = QGraphicsPixmapItem(src)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(effect)
    scene = QGraphicsScene()
    scene.addItem(item)
    out = QPixmap(src.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    scene.render(p, QRectF(out.rect()), QRectF(src.rect()))
    p.end()
    return out


class FanBoostSensor:
    """Detect MSI Cooler Boost (Fn+Up) so the aquarium can react.

    Prefers ``/sys/devices/platform/msi-ec/cooler_boost`` when the msi-ec
    driver is loaded. Otherwise infers boost from hwmon fan RPM: Cooler Boost
    holds fans near max, which jumps well above the auto curve.
    """

    COOLER_BOOST_GLOB = "/sys/devices/platform/msi-ec*/cooler_boost"
    POLL_INTERVAL = 0.4

    def __init__(self):
        self.active = False
        self._poll_left = 0.0
        self._cooler_path: str | None = None
        self._fan_paths: list[str] = []
        self._baseline = 0.0
        self._have_baseline = False
        self._rediscover()

    def _rediscover(self) -> None:
        paths = sorted(glob.glob(self.COOLER_BOOST_GLOB))
        self._cooler_path = paths[0] if paths else None
        fans: list[str] = []
        for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            fans.extend(sorted(glob.glob(os.path.join(hwmon, "fan*_input"))))
        self._fan_paths = fans

    @staticmethod
    def _read_text(path: str) -> str | None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return None

    def _read_cooler_boost(self) -> bool | None:
        if not self._cooler_path:
            return None
        raw = self._read_text(self._cooler_path)
        if raw is None:
            self._cooler_path = None
            return None
        low = raw.lower()
        if low in ("on", "1", "true"):
            return True
        if low in ("off", "0", "false"):
            return False
        return None

    def _max_fan_rpm(self) -> int | None:
        if not self._fan_paths:
            return None
        best = 0
        any_ok = False
        for path in self._fan_paths:
            raw = self._read_text(path)
            if raw is None:
                continue
            try:
                rpm = int(raw)
            except ValueError:
                continue
            any_ok = True
            if rpm > best:
                best = rpm
        return best if any_ok else None

    def _from_rpm(self, rpm: int, cfg: dict) -> bool:
        # Absolute gates (RPM). Tunable for machines where the auto curve
        # already runs hot under load.
        on_rpm = float(cfg.get("fan_boost_rpm_on", 4800.0))
        off_rpm = float(cfg.get("fan_boost_rpm_off", 4100.0))
        delta = float(cfg.get("fan_boost_rpm_delta", 1000.0))
        floor = float(cfg.get("fan_boost_rpm_floor", 3200.0))

        if not self._have_baseline:
            self._baseline = float(rpm)
            self._have_baseline = True
            return rpm >= on_rpm

        if not self.active:
            # Track typical non-boost speed so a sudden Cooler Boost jump is obvious.
            self._baseline = self._baseline * 0.95 + float(rpm) * 0.05
            if rpm >= on_rpm or (rpm >= floor and rpm >= self._baseline + delta):
                return True
            return False

        # Stay boosted until fans drop back toward the pre-boost baseline.
        if rpm <= off_rpm and rpm <= self._baseline + delta * 0.35:
            return False
        return True

    def update(self, dt: float, cfg: dict) -> bool:
        self._poll_left -= dt
        if self._poll_left > 0:
            return self.active
        self._poll_left = self.POLL_INTERVAL

        if self._cooler_path is None and not self._fan_paths:
            self._rediscover()

        flag = self._read_cooler_boost()
        if flag is not None:
            self.active = flag
            return self.active

        rpm = self._max_fan_rpm()
        if rpm is None:
            self._rediscover()
            self.active = False
            return False

        self.active = self._from_rpm(rpm, cfg)
        return self.active


class _GlobalHotkeys:
    """Listen for F12 even when the aquarium does not take focus."""

    def __init__(self, aquarium: "AquariumWindow"):
        self.aq = aquarium
        self.dpy = None
        self.keycode = None
        try:
            from Xlib import X, XK, display
            self.X = X
            self.dpy = display.Display()
            self.keycode = self.dpy.keysym_to_keycode(XK.string_to_keysym("F12"))
            root = self.dpy.screen().root
            for mods in (0, X.Mod2Mask, X.LockMask, X.Mod2Mask | X.LockMask):
                root.grab_key(self.keycode, mods, True, X.GrabModeAsync, X.GrabModeAsync)
            self.dpy.flush()
        except Exception:
            self.dpy = None
            return
        self.timer = QTimer(aquarium)
        self.timer.timeout.connect(self._poll)
        self.timer.start(50)

    def _poll(self):
        if self.dpy is None:
            return
        try:
            while self.dpy.pending_events():
                ev = self.dpy.next_event()
                if ev.type == self.X.KeyPress and ev.detail == self.keycode:
                    self.aq.toggle_controls()
        except Exception:
            pass


# ── Sprite helpers ──────────────────────────────────────────────────────────

class Sprite:
    def __init__(self, pixmap: QPixmap, x: float, y: float):
        self.pixmap = pixmap
        self.flipped = pixmap.transformed(
            pixmap.deviceIndependentSize().toSize()
            and __import__("PyQt6.QtGui", fromlist=["QTransform"]).QTransform().scale(-1, 1)
        )
        self.x = x
        self.y = y

    def draw(self, painter: QPainter, flipped: bool = False):
        pm = self.flipped if flipped else self.pixmap
        painter.drawPixmap(int(self.x), int(self.y), pm)


class FloraSprite(Sprite):
    """Ground plant: sway on hover, click-drag horizontally along the floor."""

    def __init__(self, pixmap: QPixmap, x: float, y: float):
        self.pixmap = pixmap
        self._mask = _alpha_mask(pixmap)
        self.x = x
        self.y = y
        self.w = pixmap.width()
        self.h = pixmap.height()
        self.tilt = 0.0
        self.sway_phase = random.uniform(0, 2 * math.pi)
        self.sway_rate = random.uniform(0.35, 0.55)
        self.sway_amp = random.uniform(2.5, 4.5)
        self.dragging = False
        self._grab_dx = 0.0

    def contains(self, px: float, py: float) -> bool:
        lx, ly = int(px - self.x), int(py - self.y)
        if lx < 0 or ly < 0 or lx >= self.w or ly >= self.h:
            return False
        return _alpha_at(self._mask, lx, ly) > 20

    def begin_drag(self, px: float, py: float):
        self.dragging = True
        self._grab_dx = px - self.x

    def drag_to(self, px: float, py: float, bounds: tuple, cfg: dict):
        if not self.dragging:
            return
        bw, bh = bounds
        margin = cfg.get("ground_margin", 0)
        self.x = px - self._grab_dx
        self.x = max(0.0, min(self.x, bw - self.w))
        # Always stay planted on the ground (base slightly below screen).
        sink = max(12, int(self.h * 0.10))
        self.y = bh - margin - self.h + sink

    def end_drag(self):
        self.dragging = False

    def update(self, dt: float, mouse=None):
        hovering = (
            not self.dragging
            and mouse is not None
            and self.contains(mouse[0], mouse[1])
        )
        rate = self.sway_rate * (4.0 if hovering else 1.0)
        amp = self.sway_amp * (1.6 if hovering else 1.0)
        self.sway_phase += dt * rate
        self.tilt = math.sin(self.sway_phase) * amp

    def draw(self, painter: QPainter, flipped: bool = False):
        # Pivot from the base so plants sway in place.
        painter.save()
        painter.translate(self.x + self.w * 0.5, self.y + self.h)
        painter.rotate(self.tilt)
        painter.drawPixmap(-self.w // 2, -self.h, self.pixmap)
        painter.restore()


class FishSprite(Sprite):
    def __init__(self, pixmap: QPixmap, x: float, y: float, cfg: dict, speed_factor: float = 1.0):
        # pre-compute flipped pixmap properly
        from PyQt6.QtGui import QTransform
        self.pixmap = pixmap
        self.flipped_pixmap = pixmap.transformed(QTransform().scale(-1, 1))
        self._mask = _alpha_mask(pixmap)
        self._mask_flipped = _alpha_mask(self.flipped_pixmap)
        self.x = x
        self.y = y
        self.w = pixmap.width()
        self.h = pixmap.height()
        self.facing_left = False
        self.boost_left = 0.0
        self.bob_phase = random.uniform(0, 2 * math.pi)
        self.tilt = 0.0
        self.speed_factor = max(0.1, speed_factor)
        self.bubble_timer = random.uniform(1.5, 5.0)
        # 40% swim behind flora, 60% in front
        self.behind_flora = random.random() < 0.4
        self._new_target(cfg)

    def _new_target(self, cfg: dict):
        angle = random.uniform(0, 2 * math.pi)
        speed = random.uniform(cfg["min_speed"], cfg["max_speed"]) * self.speed_factor
        self.vx = math.cos(angle) * speed
        self.vy = math.sin(angle) * speed
        self.move_timer = random.uniform(cfg["min_move_time"], cfg["max_move_time"])

    def contains(self, px: float, py: float) -> bool:
        lx, ly = int(px - self.x), int(py - self.y)
        if lx < 0 or ly < 0 or lx >= self.w or ly >= self.h:
            return False
        mask = self._mask_flipped if not self.facing_left else self._mask
        return _alpha_at(mask, lx, ly) > 20

    def scare(self, cfg: dict):
        self.boost_left = cfg.get("click_boost_time", 3.0)

    def update(self, dt: float, bounds: tuple, cfg: dict, mouse=None):
        if self.boost_left > 0:
            self.boost_left = max(0.0, self.boost_left - dt)

        self.move_timer -= dt
        if self.move_timer <= 0:
            self._new_target(cfg)

        flee_vx = flee_vy = 0.0
        radius = cfg.get("avoid_distance", 140.0)
        if mouse is not None and radius > 0:
            dx = (self.x + self.w * 0.5) - mouse[0]
            dy = (self.y + self.h * 0.5) - mouse[1]
            dist = math.hypot(dx, dy)
            if 0.001 < dist < radius:
                falloff = 1.0 - dist / radius
                strength = falloff * cfg.get("avoid_strength", 220.0)
                nx, ny = dx / dist, dy / dist
                flee_vx = nx * strength
                flee_vy = ny * strength
                self.vx += nx * strength * dt
                self.vy += ny * strength * dt

        speed_mult = cfg.get("click_boost_mult", 4.0) if self.boost_left > 0 else 1.0
        self.x += (self.vx + flee_vx) * dt * speed_mult
        self.y += (self.vy + flee_vy) * dt * speed_mult

        bw, bh = bounds
        margin_bottom = cfg.get("ground_margin", 0)

        if self.x < 0:
            self.x = 0; self.vx = abs(self.vx)
        if self.x + self.w > bw:
            self.x = bw - self.w; self.vx = -abs(self.vx)
        if self.y < 0:
            self.y = 0; self.vy = abs(self.vy)
        if self.y + self.h > bh - margin_bottom:
            self.y = bh - margin_bottom - self.h; self.vy = -abs(self.vy)

        self.facing_left = (self.vx + flee_vx) < 0

        mvx = (self.vx + flee_vx) * speed_mult
        mvy = (self.vy + flee_vy) * speed_mult
        speed = math.hypot(mvx, mvy)
        self.bob_phase += dt * (1.8 + min(speed, 180.0) * 0.02)
        bob = math.sin(self.bob_phase) * 3.5
        pitch = max(-4.0, min(4.0, mvy * 0.035))
        sign = -1.0 if self.facing_left else 1.0
        self.tilt = sign * (bob + pitch)

        self.bubble_timer -= dt
        if self.bubble_timer <= 0:
            self.bubble_timer = random.uniform(2.5, 8.0)
            return True
        return False

    def draw(self, painter: QPainter, flipped: bool = False):
        pm = self.flipped_pixmap if not self.facing_left else self.pixmap
        painter.save()
        painter.translate(self.x + self.w * 0.5, self.y + self.h * 0.5)
        painter.rotate(self.tilt)
        painter.drawPixmap(-self.w // 2, -self.h // 2, pm)
        painter.restore()


class BubbleSprite:
    """Fast-rising wobbling bubble that pops off the top of the screen."""

    def __init__(self, pixmap: QPixmap, x: float, y: float):
        self.pixmap = pixmap
        self.w = pixmap.width()
        self.h = pixmap.height()
        self.base_x = x - self.w * 0.5
        self.x = self.base_x
        self.y = y - self.h * 0.5
        self.phase = random.uniform(0, 2 * math.pi)
        self.rise_speed = random.uniform(220.0, 340.0)
        self.wobble_amp = random.uniform(14.0, 32.0)
        self.wobble_rate = random.uniform(5.0, 9.0)
        self.alive = True

    def update(self, dt: float):
        self.phase += dt * self.wobble_rate
        self.y -= self.rise_speed * dt
        self.x = self.base_x + math.sin(self.phase) * self.wobble_amp
        if self.y + self.h < -4:
            self.alive = False

    def draw(self, painter: QPainter):
        painter.drawPixmap(int(self.x), int(self.y), self.pixmap)


class KrustaceanSprite:
    """Ground walker: fish-like movement, but only left/right on the floor."""

    def __init__(self, pixmap: QPixmap, x: float, y: float, cfg: dict):
        self.pixmap = pixmap
        self._mask = _alpha_mask(pixmap)
        self.x = x
        self.y = y
        self.w = pixmap.width()
        self.h = pixmap.height()
        self.boost_left = 0.0
        self.bob_phase = random.uniform(0, 2 * math.pi)
        self.tilt = 0.0
        self.vx = 0.0
        self._new_target(cfg)

    def _new_target(self, cfg: dict):
        speed = random.uniform(cfg["min_speed"], cfg["max_speed"]) * 0.7
        self.vx = speed if random.random() < 0.5 else -speed
        self.move_timer = random.uniform(cfg["min_move_time"], cfg["max_move_time"])

    def contains(self, px: float, py: float) -> bool:
        lx, ly = int(px - self.x), int(py - self.y)
        if lx < 0 or ly < 0 or lx >= self.w or ly >= self.h:
            return False
        return _alpha_at(self._mask, lx, ly) > 20

    def scare(self, cfg: dict):
        self.boost_left = cfg.get("click_boost_time", 3.0)

    def update(self, dt: float, bounds: tuple, cfg: dict, mouse=None):
        if self.boost_left > 0:
            self.boost_left = max(0.0, self.boost_left - dt)

        self.move_timer -= dt
        if self.move_timer <= 0:
            self._new_target(cfg)

        flee_vx = 0.0
        radius = cfg.get("avoid_distance", 140.0)
        if mouse is not None and radius > 0:
            dx = (self.x + self.w * 0.5) - mouse[0]
            dy = (self.y + self.h * 0.5) - mouse[1]
            dist = math.hypot(dx, dy)
            if 0.001 < dist < radius:
                falloff = 1.0 - dist / radius
                strength = falloff * cfg.get("avoid_strength", 220.0)
                # Horizontal flee only
                flee_vx = (1.0 if dx >= 0 else -1.0) * strength
                self.vx += (1.0 if dx >= 0 else -1.0) * strength * dt * 0.35

        speed_mult = cfg.get("click_boost_mult", 4.0) if self.boost_left > 0 else 1.0
        self.x += (self.vx + flee_vx) * dt * speed_mult

        bw, bh = bounds
        margin_bottom = cfg.get("ground_margin", 0)
        # Lock to ground level
        self.y = bh - margin_bottom - self.h

        if self.x < 0:
            self.x = 0
            self.vx = abs(self.vx)
        if self.x + self.w > bw:
            self.x = bw - self.w
            self.vx = -abs(self.vx)

        mvx = (self.vx + flee_vx) * speed_mult
        self.bob_phase += dt * (1.6 + min(abs(mvx), 180.0) * 0.015)
        self.tilt = math.sin(self.bob_phase) * 2.5

    def draw(self, painter: QPainter):
        painter.save()
        painter.translate(self.x + self.w * 0.5, self.y + self.h * 0.5)
        painter.rotate(self.tilt)
        painter.drawPixmap(-self.w // 2, -self.h // 2, self.pixmap)
        painter.restore()


class StarfishSprite:
    """Stationary critter: random spawn angle, idle wobble, poke/drag."""

    def __init__(self, pixmap: QPixmap, x: float, y: float):
        self.pixmap = pixmap
        self._mask = _alpha_mask(pixmap)
        self.x = x
        self.y = y
        self.w = pixmap.width()
        self.h = pixmap.height()
        self.base_angle = random.uniform(0, 360)
        self.tilt = self.base_angle
        self.bob_phase = random.uniform(0, 2 * math.pi)
        self.tilt_boost = 0.0
        self.dragging = False
        self._grab_dx = 0.0
        self._grab_dy = 0.0

    def contains(self, px: float, py: float) -> bool:
        # Inverse-rotate around center so hit-test matches drawn orientation.
        cx = self.x + self.w * 0.5
        cy = self.y + self.h * 0.5
        angle = -math.radians(self.tilt)
        dx, dy = px - cx, py - cy
        c, s = math.cos(angle), math.sin(angle)
        lx = int(dx * c - dy * s + self.w * 0.5)
        ly = int(dx * s + dy * c + self.h * 0.5)
        if lx < 0 or ly < 0 or lx >= self.w or ly >= self.h:
            return False
        return _alpha_at(self._mask, lx, ly) > 20

    def poke(self, cfg: dict):
        self.tilt_boost = cfg.get("starfish_poke_time", 2.5)

    def begin_drag(self, px: float, py: float):
        self.dragging = True
        self._grab_dx = px - self.x
        self._grab_dy = py - self.y

    def drag_to(self, px: float, py: float, bounds: tuple, cfg: dict):
        if not self.dragging:
            return
        bw, bh = bounds
        margin = cfg.get("ground_margin", 0)
        self.x = px - self._grab_dx
        self.y = py - self._grab_dy
        self.x = max(0.0, min(self.x, bw - self.w))
        self.y = max(0.0, min(self.y, bh - margin - self.h))

    def end_drag(self):
        self.dragging = False

    def update(self, dt: float, cfg: dict):
        if self.tilt_boost > 0:
            self.tilt_boost = max(0.0, self.tilt_boost - dt)
        base_rate = cfg.get("starfish_tilt_rate", 1.2)
        boost_rate = cfg.get("starfish_tilt_boost_rate", 5.5)
        rate = boost_rate if self.tilt_boost > 0 else base_rate
        amp = 6.0 if self.tilt_boost > 0 else 3.0
        self.bob_phase += dt * rate
        self.tilt = self.base_angle + math.sin(self.bob_phase) * amp

    def draw(self, painter: QPainter):
        painter.save()
        painter.translate(self.x + self.w * 0.5, self.y + self.h * 0.5)
        painter.rotate(self.tilt)
        painter.drawPixmap(-self.w // 2, -self.h // 2, self.pixmap)
        painter.restore()


class GroundSprite:
    def __init__(self, pixmap: QPixmap, x: float, y: float):
        self.pixmap = pixmap
        self.x = x
        self.y = y
        self.w = pixmap.width()
        self.h = pixmap.height()

    def draw(self, painter: QPainter):
        painter.drawPixmap(int(self.x), int(self.y), self.pixmap)


class RelikSprite:
    """Static ground decoration spawned with the terrain."""

    def __init__(self, pixmap: QPixmap, x: float, y: float):
        self.pixmap = pixmap
        self.x = x
        self.y = y
        self.w = pixmap.width()
        self.h = pixmap.height()

    def draw(self, painter: QPainter):
        painter.drawPixmap(int(self.x), int(self.y), self.pixmap)


class GodRaysEffect:
    """Soft volumetric light shafts that drift slowly across the tank."""

    def __init__(self, count: int = 6):
        self.beams = [self._new_beam() for _ in range(count)]

    def _new_beam(self) -> dict:
        direction = random.choice((-1.0, 1.0))
        base_angle = random.uniform(-14.0, 18.0)
        return {
            "x_frac": random.uniform(-0.05, 1.05),
            "base_angle": base_angle,
            "angle": base_angle,
            "width": random.uniform(36.0, 100.0),
            "spread": random.uniform(1.5, 2.8),
            "phase": random.uniform(0.0, math.pi * 2),
            "speed": random.uniform(0.18, 0.35),
            "strength": random.uniform(0.45, 1.0),
            # Screen-fraction per second — full crossing in ~25–50s.
            "drift": direction * random.uniform(0.02, 0.04),
            "angle_wobble": random.uniform(0.4, 1.2),
        }

    def update(self, dt: float):
        for b in self.beams:
            b["phase"] += dt * b["speed"]
            b["x_frac"] += b["drift"] * dt
            # Wrap so beams keep sliding across endlessly.
            if b["x_frac"] > 1.25:
                b["x_frac"] -= 1.5
            elif b["x_frac"] < -0.25:
                b["x_frac"] += 1.5
            b["angle"] = b["base_angle"] + math.sin(b["phase"] * 0.35) * b["angle_wobble"]

    def draw(self, painter: QPainter, w: int, h: int, intensity: float):
        if intensity <= 0.01 or w <= 0 or h <= 0:
            return
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for b in self.beams:
            pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(b["phase"]))
            a = int(48 * intensity * b["strength"] * pulse)
            if a < 2:
                continue
            ang = math.radians(b["angle"])
            x0 = b["x_frac"] * w
            x1 = x0 + math.tan(ang) * h
            wt = b["width"]
            wb = wt * b["spread"]

            path = QPainterPath()
            path.moveTo(x0 - wt * 0.5, -20)
            path.lineTo(x0 + wt * 0.5, -20)
            path.lineTo(x1 + wb * 0.5, h + 20)
            path.lineTo(x1 - wb * 0.5, h + 20)
            path.closeSubpath()

            grad = QLinearGradient(x0, 0, x1, h)
            grad.setColorAt(0.0, QColor(210, 235, 255, a))
            grad.setColorAt(0.35, QColor(190, 225, 250, int(a * 0.55)))
            grad.setColorAt(0.75, QColor(170, 210, 240, int(a * 0.2)))
            grad.setColorAt(1.0, QColor(150, 200, 230, 0))
            painter.fillPath(path, grad)

            # Soft halo — wider, much fainter
            path2 = QPainterPath()
            path2.moveTo(x0 - wt * 0.95, -20)
            path2.lineTo(x0 + wt * 0.95, -20)
            path2.lineTo(x1 + wb * 1.15, h + 20)
            path2.lineTo(x1 - wb * 1.15, h + 20)
            path2.closeSubpath()
            grad2 = QLinearGradient(x0, 0, x1, h)
            ha = max(1, a // 3)
            grad2.setColorAt(0.0, QColor(200, 230, 255, ha))
            grad2.setColorAt(0.5, QColor(180, 220, 245, ha // 2))
            grad2.setColorAt(1.0, QColor(160, 210, 235, 0))
            painter.fillPath(path2, grad2)
        painter.restore()


# ── Main aquarium window ───────────────────────────────────────────────────

class AquariumWindow(QOpenGLWidget):
    def __init__(self, ground_path: str | None = None):
        super().__init__()
        self.setWindowTitle("Akuarium")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_X11DoNotAcceptFocus)
        # Full-frame redraw into an FBO — rotations/blends run on the GPU.
        self.setUpdateBehavior(QOpenGLWidget.UpdateBehavior.NoPartialUpdate)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnBottomHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        self.cfg = {
            "min_speed": 20.0,
            "max_speed": 80.0,
            "min_move_time": 1.0,
            "max_move_time": 5.0,
            "fish_scale": 0.2,
            "flora_scale": 0.2,
            "ground_scale": 0.08,
            "ground_overlap": 0.62,
            "ground_margin": 0,
            "spawn_interval": 0.0,
            "max_fish": 50,
            "max_flora": 30,
            "click_boost_mult": 4.0,
            "click_boost_time": 3.0,
            "avoid_distance": 140.0,
            "avoid_strength": 220.0,
            "starfish_tilt_rate": 1.2,
            "starfish_tilt_boost_rate": 5.5,
            "starfish_poke_time": 2.5,
            "max_starfish": 30,
            "max_krustacean": 30,
            "bubble_scale": 0.05,
            "relik_scale": 0.16,
            "relik_spawn_chance": 0.4,
            "godrays_intensity": 0.55,
            "frost_intensity": 0.28,
            "blur_intensity": 0.0,
            "background_path": "",
            # Cooler Boost (Fn+Up): speed up the whole tank while fans are boosted.
            "fan_boost_speed_mult": 2.5,
            "fan_boost_rpm_on": 4800.0,
            "fan_boost_rpm_off": 4100.0,
            "fan_boost_rpm_delta": 1000.0,
            "fan_boost_rpm_floor": 3200.0,
        }

        self.ground_catalog = self._load_folder_catalog(GROUND_DIR, "ground")
        self.ground_pixmaps = self._pixmaps_for_ground(ground_path)
        self.relik_pixmaps = self._load_pixmaps(RELIK_DIR)
        self.background_catalog = self._load_background_catalog()
        self.flora_catalog = self._load_folder_catalog(FLORA_DIR, "flora")
        self.fish_catalog = self._load_folder_catalog(FISH_DIR, "fish")
        self.starfish_catalog = self._load_folder_catalog(STARFISH_DIR, "starfish")
        self.krustacean_catalog = self._load_folder_catalog(KRUSTACEAN_DIR, "krustacean")
        self.flora_pixmaps = [item["pixmap"] for item in self.flora_catalog]
        self.fish_pixmaps = [item["pixmap"] for item in self.fish_catalog]
        self.starfish_pixmaps = [item["pixmap"] for item in self.starfish_catalog]
        self.krustacean_pixmaps = [item["pixmap"] for item in self.krustacean_catalog]
        bubble_pm = QPixmap(BUBBLE_PATH)
        self.bubble_pixmap = bubble_pm if not bubble_pm.isNull() else None

        self.flora_sprites: list[FloraSprite] = []
        self.ground_sprites: list[GroundSprite] = []
        self.relik_sprites: list[RelikSprite] = []
        self.fish_sprites: list[FishSprite] = []
        self.starfish_sprites: list[StarfishSprite] = []
        self.krustacean_sprites: list[KrustaceanSprite] = []
        self.bubble_sprites: list[BubbleSprite] = []
        self.god_rays = GodRaysEffect()
        self._drag_starfish: StarfishSprite | None = None
        self._drag_flora: FloraSprite | None = None
        self._background_src: QPixmap | None = None
        self._background_scaled: QPixmap | None = None
        self._desktop_plate: QPixmap | None = None
        self._blur_layer: QPixmap | None = None

        self._spawn_ground()
        self._initial_spawn()
        self.set_background(self.cfg.get("background_path") or None)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK)

        self.spawn_accum = 0.0
        self.fan_boost = FanBoostSensor()

        self.controls: "ControlsWindow | None" = None
        self._hotkeys = _GlobalHotkeys(self)
        self._fish_watch = QFileSystemWatcher(self)
        for folder in (
            FISH_DIR, STARFISH_DIR, KRUSTACEAN_DIR, FLORA_DIR, BACKGROUND_DIR,
        ):
            if os.path.isdir(folder):
                self._fish_watch.addPath(folder)
        self._fish_watch.directoryChanged.connect(self._on_fish_dir_changed)
        self._fish_watch.fileChanged.connect(self._on_fish_dir_changed)
        QTimer.singleShot(0, self._sink)
        QTimer.singleShot(250, self._sink)

    def _load_pixmaps(self, folder: str) -> list[QPixmap]:
        pms = []
        for p in sorted(glob.glob(os.path.join(folder, "*.png"))):
            pm = QPixmap(p)
            if not pm.isNull():
                pms.append(pm)
        return pms

    def _load_folder_catalog(self, folder: str, kind: str) -> list[dict]:
        items = []
        for p in sorted(glob.glob(os.path.join(folder, "*.png"))):
            pm = QPixmap(p)
            if pm.isNull():
                continue
            items.append({
                "path": p,
                "name": os.path.splitext(os.path.basename(p))[0],
                "pixmap": pm,
                "kind": kind,
            })
        return items

    def _load_background_catalog(self) -> list[dict]:
        items = []
        paths: list[str] = []
        for pattern in BACKGROUND_GLOBS:
            paths.extend(glob.glob(os.path.join(BACKGROUND_DIR, pattern)))
        for p in sorted(set(paths)):
            pm = QPixmap(p)
            if pm.isNull():
                continue
            items.append({
                "path": p,
                "name": os.path.splitext(os.path.basename(p))[0],
                "pixmap": pm,
                "kind": "background",
            })
        return items

    def set_background(self, path: str | None):
        """Set tank backdrop from backgrounds/, or None for transparent desktop."""
        self.cfg["background_path"] = path or ""
        self._background_src = None
        self._background_scaled = None
        if path:
            pm = QPixmap(path)
            if not pm.isNull():
                self._background_src = pm
            else:
                self.cfg["background_path"] = ""
        self._rebuild_background_scaled()
        self._rebuild_blur_layer()
        self.update()

    def _rebuild_background_scaled(self):
        if self._background_src is None or self._background_src.isNull():
            self._background_scaled = None
            return
        w, h = max(1, self.width()), max(1, self.height())
        scaled = self._background_src.scaled(
            w, h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        # Center-crop to window size
        x = max(0, (scaled.width() - w) // 2)
        y = max(0, (scaled.height() - h) // 2)
        self._background_scaled = scaled.copy(x, y, w, h)

    def _refresh_desktop_plate(self):
        """Snapshot the desktop under the tank for the independent blur effect."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        # Hide briefly so fish/ground are not baked into the plate
        was_visible = self.isVisible()
        if was_visible:
            self.hide()
            QApplication.processEvents()
        grab = screen.grabWindow(0)
        if was_visible:
            self.show()
            self._sink()
            QApplication.processEvents()
        if grab.isNull():
            return
        w, h = max(1, self.width()), max(1, self.height())
        geo = self.geometry()
        # Crop to this window's screen rect when possible
        if grab.width() >= geo.x() + w and grab.height() >= geo.y() + h:
            grab = grab.copy(geo.x(), geo.y(), w, h)
        else:
            grab = grab.scaled(
                w, h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._desktop_plate = grab

    def _rebuild_blur_layer(self):
        """Build a blurred plate from desktop or background — independent of bg pick."""
        blur = float(self.cfg.get("blur_intensity", 0.0) or 0.0)
        if blur <= 0.05:
            self._blur_layer = None
            return
        w, h = max(1, self.width()), max(1, self.height())
        src = None
        if self._background_scaled is not None and not self._background_scaled.isNull():
            src = self._background_scaled
        else:
            if self._desktop_plate is None or self._desktop_plate.isNull():
                self._refresh_desktop_plate()
            src = self._desktop_plate
        if src is None or src.isNull():
            self._blur_layer = None
            return
        if src.width() != w or src.height() != h:
            src = src.scaled(
                w, h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        sw, sh = max(1, w // 2), max(1, h // 2)
        small = src.scaled(
            sw, sh,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        blurred = _blur_pixmap(small, blur * 0.5)
        self._blur_layer = blurred.scaled(
            w, h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _draw_background(self, painter: QPainter):
        # Blur plate is its own effect; sharp background only when blur is off
        if self._blur_layer is not None and not self._blur_layer.isNull():
            painter.drawPixmap(0, 0, self._blur_layer)
            return
        if self._background_scaled is None or self._background_scaled.isNull():
            return
        painter.drawPixmap(0, 0, self._background_scaled)

    def _pixmaps_for_ground(self, ground_path: str | None) -> list[QPixmap]:
        if ground_path:
            chosen = [item["pixmap"] for item in self.ground_catalog if item["path"] == ground_path]
            if chosen:
                return chosen
        return [item["pixmap"] for item in self.ground_catalog]

    def shop_catalog(self) -> list[dict]:
        items = list(self.fish_catalog)
        items.extend(self.krustacean_catalog)
        items.extend(self.starfish_catalog)
        items.extend(self.flora_catalog)
        return items

    def fish_signature(self) -> tuple[str, ...]:
        return tuple(item["path"] for item in self.shop_catalog())

    def reload_fish(self):
        self.flora_catalog = self._load_folder_catalog(FLORA_DIR, "flora")
        self.fish_catalog = self._load_folder_catalog(FISH_DIR, "fish")
        self.starfish_catalog = self._load_folder_catalog(STARFISH_DIR, "starfish")
        self.krustacean_catalog = self._load_folder_catalog(KRUSTACEAN_DIR, "krustacean")
        self.background_catalog = self._load_background_catalog()
        self.flora_pixmaps = [item["pixmap"] for item in self.flora_catalog]
        self.fish_pixmaps = [item["pixmap"] for item in self.fish_catalog]
        self.starfish_pixmaps = [item["pixmap"] for item in self.starfish_catalog]
        self.krustacean_pixmaps = [item["pixmap"] for item in self.krustacean_catalog]
        watched = set(self._fish_watch.files())
        wanted = {item["path"] for item in self.shop_catalog()}
        for folder in (
            FISH_DIR, STARFISH_DIR, KRUSTACEAN_DIR, FLORA_DIR, BACKGROUND_DIR,
        ):
            if os.path.isdir(folder):
                wanted.add(folder)
        for p in watched - wanted:
            self._fish_watch.removePath(p)
        for p in wanted - watched:
            self._fish_watch.addPath(p)
        if self.controls is not None:
            self.controls.refresh_shop()
            self.controls.refresh_backgrounds()

    def _on_fish_dir_changed(self, _path: str = ""):
        QTimer.singleShot(150, self.reload_fish)

    def _scaled(self, pm: QPixmap, kind: str, jitter: float = 0.0) -> QPixmap:
        key = "fish_scale" if kind in ("fish", "starfish", "krustacean") else f"{kind}_scale"
        s = self.cfg[key]
        if jitter > 0:
            s *= random.uniform(1.0 - jitter, 1.0 + jitter)
        if abs(s - 1.0) < 1e-6:
            return pm
        return pm.scaled(
            max(1, int(pm.width() * s)), max(1, int(pm.height() * s)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _spawn_ground(self):
        """Tile Ground/*.png across the bottom with heavy overlap for a dense look."""
        self.ground_sprites.clear()
        self.relik_sprites.clear()
        if not self.ground_pixmaps:
            return
        sw, sh = max(1, self.width()), max(1, self.height())
        overlap = self.cfg.get("ground_overlap", 0.62)
        margin = self.cfg.get("ground_margin", 0)
        # One shared height so every ground PNG sits on the same baseline.
        max_h = max(40, sh // 3)
        target_h = max(1, int(self.ground_pixmaps[0].height() * self.cfg["ground_scale"]))
        target_h = min(target_h, max_h)
        y = sh - target_h - margin
        x = -20.0
        i = 0
        for _ in range(800):
            if x >= sw:
                break
            pm = self.ground_pixmaps[i % len(self.ground_pixmaps)]
            spm = pm.scaledToHeight(
                target_h,
                Qt.TransformationMode.SmoothTransformation,
            )
            if spm.width() < 1:
                break
            self.ground_sprites.append(GroundSprite(spm, x, y))
            step = max(8.0, spm.width() * (1.0 - overlap))
            x += step + random.uniform(-6.0, 6.0)
            i += 1
        self._spawn_reliks()

    def _spawn_reliks(self):
        """Low chance to place one or two static Reliks on the ground."""
        self.relik_sprites.clear()
        if not self.relik_pixmaps:
            return
        if random.random() > self.cfg.get("relik_spawn_chance", 0.4):
            return
        sw, sh = max(1, self.width()), max(1, self.height())
        count = 1 + (1 if random.random() < 0.4 else 0)
        count = min(count, len(self.relik_pixmaps))
        # Cap horizontal overlap so Reliks stay visually distinct.
        max_overlap_frac = 0.22
        for pm in random.sample(self.relik_pixmaps, count):
            spm = self._scaled(pm, "relik", jitter=0.12)
            max_x = max(0, sw - spm.width())
            y = sh - spm.height() - self.cfg["ground_margin"] + random.randint(-4, 10)
            x = None
            for _ in range(32):
                candidate = random.randint(0, max_x)
                if self._relik_placement_ok(candidate, spm.width(), max_overlap_frac):
                    x = candidate
                    break
            if x is None:
                # Last resort: nudge into the emptiest horizontal gap.
                x = self._relik_farthest_x(spm.width(), max_x)
            self.relik_sprites.append(RelikSprite(spm, x, y))

    def _relik_placement_ok(self, x: float, w: float, max_overlap_frac: float) -> bool:
        for r in self.relik_sprites:
            overlap = min(x + w, r.x + r.w) - max(x, r.x)
            if overlap > min(w, r.w) * max_overlap_frac:
                return False
        return True

    def _relik_farthest_x(self, w: float, max_x: int) -> int:
        """Pick an x that maximizes distance from existing Relik centers."""
        if not self.relik_sprites or max_x <= 0:
            return random.randint(0, max_x)
        best_x, best_dist = 0, -1.0
        for candidate in range(0, max_x + 1, max(1, max_x // 40 or 1)):
            cx = candidate + w / 2
            dist = min(abs(cx - (r.x + r.w / 2)) for r in self.relik_sprites)
            if dist > best_dist:
                best_dist, best_x = dist, candidate
        return best_x

    def _initial_spawn(self):
        sw, sh = self.width(), self.height()
        for pm in self.flora_pixmaps:
            spm = self._scaled(pm, "flora")
            x = random.randint(0, max(0, sw - spm.width()))
            sink = max(12, int(spm.height() * 0.10))
            y = sh - spm.height() - self.cfg["ground_margin"] + sink
            self.flora_sprites.append(FloraSprite(spm, x, y))

        if self.fish_pixmaps:
            pm = random.choice(self.fish_pixmaps)
            spm = self._scaled(pm, "fish")
            x = random.randint(0, max(0, sw - spm.width()))
            y = random.randint(0, max(0, sh - spm.height() - self.cfg["ground_margin"]))
            self.fish_sprites.append(FishSprite(spm, x, y, self.cfg))

    def spawn_flora(self, pixmap: QPixmap | None = None):
        if pixmap is None:
            if not self.flora_pixmaps:
                return
            pixmap = random.choice(self.flora_pixmaps)
        if len(self.flora_sprites) >= self.cfg["max_flora"]:
            return
        spm = self._scaled(pixmap, "flora")
        sw, sh = self.width(), self.height()
        x = random.randint(0, max(0, sw - spm.width()))
        sink = max(12, int(spm.height() * 0.10))
        y = sh - spm.height() - self.cfg["ground_margin"] + sink
        self.flora_sprites.append(FloraSprite(spm, x, y))

    def spawn_fish(self, pixmap: QPixmap | None = None, speed_factor: float = 1.0):
        if pixmap is None:
            if not self.fish_pixmaps:
                return
            pixmap = random.choice(self.fish_pixmaps)
        if len(self.fish_sprites) >= self.cfg["max_fish"]:
            return
        spm = self._scaled(pixmap, "fish")
        sw, sh = self.width(), self.height()
        x = random.randint(0, max(0, sw - spm.width()))
        y = random.randint(0, max(0, sh - spm.height() - self.cfg["ground_margin"]))
        self.fish_sprites.append(FishSprite(spm, x, y, self.cfg, speed_factor=speed_factor))

    def spawn_fish_path(self, path: str):
        for item in self.fish_catalog:
            if item["path"] == path:
                self.spawn_fish(item["pixmap"])
                return
        for item in self.starfish_catalog:
            if item["path"] == path:
                self.spawn_starfish(item["pixmap"])
                return
        for item in self.krustacean_catalog:
            if item["path"] == path:
                self.spawn_krustacean(item["pixmap"])
                return
        for item in self.flora_catalog:
            if item["path"] == path:
                self.spawn_flora(item["pixmap"])
                return
        pm = QPixmap(path)
        if pm.isNull():
            return
        if path.startswith(STARFISH_DIR):
            self.spawn_starfish(pm)
        elif path.startswith(KRUSTACEAN_DIR):
            self.spawn_krustacean(pm)
        elif path.startswith(FLORA_DIR):
            self.spawn_flora(pm)
        else:
            self.spawn_fish(pm)

    def spawn_starfish(self, pixmap: QPixmap | None = None):
        if pixmap is None:
            if not self.starfish_pixmaps:
                return
            pixmap = random.choice(self.starfish_pixmaps)
        if len(self.starfish_sprites) >= self.cfg["max_starfish"]:
            return
        spm = self._scaled(pixmap, "starfish", jitter=0.18)
        sw, sh = self.width(), self.height()
        x = random.randint(0, max(0, sw - spm.width()))
        y = random.randint(0, max(0, sh - spm.height() - self.cfg["ground_margin"]))
        self.starfish_sprites.append(StarfishSprite(spm, x, y))

    def spawn_krustacean(self, pixmap: QPixmap | None = None):
        if pixmap is None:
            if not self.krustacean_pixmaps:
                return
            pixmap = random.choice(self.krustacean_pixmaps)
        if len(self.krustacean_sprites) >= self.cfg["max_krustacean"]:
            return
        spm = self._scaled(pixmap, "krustacean")
        sw, sh = self.width(), self.height()
        x = random.randint(0, max(0, sw - spm.width()))
        y = sh - spm.height() - self.cfg["ground_margin"]
        self.krustacean_sprites.append(KrustaceanSprite(spm, x, y, self.cfg))

    def spawn_bubble_from(self, fish: FishSprite):
        if self.bubble_pixmap is None:
            return
        scale = self.cfg.get("bubble_scale", 0.05) * random.uniform(0.75, 1.25)
        pm = self.bubble_pixmap.scaled(
            max(8, int(self.bubble_pixmap.width() * scale)),
            max(8, int(self.bubble_pixmap.height() * scale)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # Release near the fish mouth (front of travel).
        cx = fish.x + fish.w * (0.2 if fish.facing_left else 0.8)
        cy = fish.y + fish.h * 0.45
        self.bubble_sprites.append(BubbleSprite(pm, cx, cy))

    def tick(self):
        dt = TICK / 1000.0
        if self.fan_boost.update(dt, self.cfg):
            dt *= float(self.cfg.get("fan_boost_speed_mult", 2.5) or 1.0)
        bounds = (self.width(), self.height())
        cursor = self.mapFromGlobal(QCursor.pos())
        mouse = (cursor.x(), cursor.y())
        for f in self.fish_sprites:
            if f.update(dt, bounds, self.cfg, mouse):
                self.spawn_bubble_from(f)
        for k in self.krustacean_sprites:
            k.update(dt, bounds, self.cfg, mouse)
        for s in self.starfish_sprites:
            s.update(dt, self.cfg)
        for fl in self.flora_sprites:
            fl.update(dt, mouse)
        for b in self.bubble_sprites:
            b.update(dt)
        self.bubble_sprites = [b for b in self.bubble_sprites if b.alive]
        self.god_rays.update(dt)

        self.spawn_accum += dt
        if self.cfg["spawn_interval"] > 0 and self.spawn_accum >= self.cfg["spawn_interval"]:
            self.spawn_accum = 0.0
            if random.random() < 0.5:
                self.spawn_fish()
            else:
                self.spawn_flora()

        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        # OpenGL FBO starts opaque; wipe to transparent so the desktop shows through.
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_background(p)
        self._draw_frost(p)
        for s in self.ground_sprites:
            s.draw(p)
        for s in self.relik_sprites:
            s.draw(p)
        for s in self.krustacean_sprites:
            s.draw(p)
        for s in self.starfish_sprites:
            s.draw(p)
        for s in self.fish_sprites:
            if s.behind_flora:
                s.draw(p)
        for s in self.flora_sprites:
            s.draw(p)
        for s in self.fish_sprites:
            if not s.behind_flora:
                s.draw(p)
        for s in self.bubble_sprites:
            s.draw(p)
        self.god_rays.draw(
            p, self.width(), self.height(),
            self.cfg.get("godrays_intensity", 0.55),
        )
        p.end()

    def _draw_frost(self, painter: QPainter):
        """Very light frosted-glass wash behind the tank contents."""
        intensity = self.cfg.get("frost_intensity", 0.28)
        if intensity <= 0.01:
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        a = max(1, int(26 * intensity))
        painter.fillRect(0, 0, w, h, QColor(222, 236, 246, a))
        # Soft vertical sheen — glass catching light at the top
        sheen = max(1, int(18 * intensity))
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor(255, 255, 255, sheen))
        grad.setColorAt(0.4, QColor(240, 248, 255, sheen // 3))
        grad.setColorAt(1.0, QColor(200, 220, 235, 0))
        painter.fillRect(0, 0, w, h, grad)

    def _sink(self):
        """Keep the aquarium under every other window."""
        self.setWindowFlag(Qt.WindowType.WindowStaysOnBottomHint, True)
        self.lower()
        handle = self.windowHandle()
        if handle is not None:
            handle.setFlag(Qt.WindowType.WindowStaysOnBottomHint, True)
            handle.lower()
        _pin_below_x11(int(self.winId()))

    def showEvent(self, event):
        super().showEvent(event)
        self._sink()
        QTimer.singleShot(100, self._sink)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._spawn_ground()
        self._rebuild_background_scaled()
        self._rebuild_blur_layer()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.ActivationChange,
            QEvent.Type.WindowStateChange,
            QEvent.Type.ZOrderChange,
        ):
            if self.isActiveWindow():
                self._sink()
            else:
                self.lower()

    def mousePressEvent(self, event):
        pos = event.position()
        px, py = pos.x(), pos.y()
        for sprite in reversed(self.fish_sprites):
            if not sprite.behind_flora and sprite.contains(px, py):
                sprite.scare(self.cfg)
                self._sink()
                return
        for sprite in reversed(self.flora_sprites):
            if sprite.contains(px, py):
                sprite.begin_drag(px, py)
                self._drag_flora = sprite
                self.flora_sprites.remove(sprite)
                self.flora_sprites.append(sprite)
                self._sink()
                return
        for sprite in reversed(self.starfish_sprites):
            if sprite.contains(px, py):
                sprite.poke(self.cfg)
                sprite.begin_drag(px, py)
                self._drag_starfish = sprite
                # Bring dragged starfish to front among starfish
                self.starfish_sprites.remove(sprite)
                self.starfish_sprites.append(sprite)
                self._sink()
                return
        for sprite in reversed(self.krustacean_sprites):
            if sprite.contains(px, py):
                sprite.scare(self.cfg)
                self._sink()
                return
        for sprite in reversed(self.fish_sprites):
            if sprite.behind_flora and sprite.contains(px, py):
                sprite.scare(self.cfg)
                break
        self._sink()

    def mouseMoveEvent(self, event):
        if self._drag_starfish is not None:
            pos = event.position()
            bounds = (self.width(), self.height())
            self._drag_starfish.drag_to(pos.x(), pos.y(), bounds, self.cfg)
            self.update()
        elif self._drag_flora is not None:
            pos = event.position()
            bounds = (self.width(), self.height())
            self._drag_flora.drag_to(pos.x(), pos.y(), bounds, self.cfg)
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_starfish is not None:
            self._drag_starfish.end_drag()
            self._drag_starfish = None
        if self._drag_flora is not None:
            self._drag_flora.end_drag()
            self._drag_flora = None
        super().mouseReleaseEvent(event)

    def event(self, event):
        if event.type() in (
            QEvent.Type.WindowActivate,
            QEvent.Type.FocusIn,
        ):
            QTimer.singleShot(0, self._sink)
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_F12:
            self.toggle_controls()
        elif event.key() == Qt.Key.Key_Escape:
            QApplication.quit()

    def toggle_controls(self):
        if self.controls and self.controls.isVisible():
            self.controls.hide()
        else:
            if not self.controls:
                self.controls = ControlsWindow(self)
            self.reload_fish()
            self.controls.capture_desktop()
            self.controls.show()
            self.controls.raise_()
            self.controls.activateWindow()
            self._sink()


# ── Controls window ────────────────────────────────────────────────────────

AERO_STYLE = """
QWidget {
    background: transparent;
    color: #000000;
    font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif;
    font-size: 12px;
}
QLabel#aeroTitle {
    color: #000000;
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 1px;
    background: transparent;
}
QGroupBox {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255,255,255,70),
        stop:1 rgba(200,200,200,35));
    border: 1px solid rgba(255,255,255,180);
    border-radius: 16px;
    margin-top: 0px;
    padding: 8px 10px 10px 10px;
    font-weight: 700;
    color: #000000;
}
QLabel#sectionTitle {
    color: #000000;
    font-size: 13px;
    font-weight: 700;
    background: transparent;
    padding: 2px 2px 6px 2px;
}
QLabel {
    color: #000000;
    font-weight: 600;
    background: transparent;
}
QSpinBox, QDoubleSpinBox {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255,255,255,160),
        stop:0.5 rgba(240,240,240,120),
        stop:1 rgba(220,220,220,100));
    border: 1px solid rgba(255,255,255,200);
    border-radius: 9px;
    padding: 3px 8px;
    color: #000000;
    min-height: 24px;
    selection-background-color: rgba(160, 160, 160, 160);
    selection-color: #000000;
}
QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid rgba(210, 210, 210, 230);
}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255,255,255,200),
        stop:1 rgba(170,170,170,140));
    border: 1px solid rgba(255,255,255,180);
    width: 16px;
    margin: 2px;
    border-radius: 4px;
}
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255,255,255,150),
        stop:1 rgba(190,190,190,120));
    border: 1px solid rgba(255,255,255,210);
    border-bottom: 1px solid rgba(90, 90, 90, 100);
    border-radius: 7px;
    padding: 4px 8px;
    color: #000000;
    font-weight: 700;
    min-height: 18px;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255,255,255,220),
        stop:1 rgba(190,190,190,180));
}
QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(120,120,120,200),
        stop:1 rgba(220,220,220,180));
}
QPushButton#exitBtn {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(230,230,230,200),
        stop:1 rgba(90,90,90,170));
    color: #000000;
}
QTabWidget::pane {
    background: transparent;
    border: none;
    margin-top: 4px;
}
QTabBar::tab {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255,255,255,130),
        stop:1 rgba(190,190,190,90));
    border: 1px solid rgba(255,255,255,180);
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 5px 14px;
    margin-right: 3px;
    color: #000000;
    font-weight: 700;
}
QTabBar::tab:selected {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255,255,255,190),
        stop:1 rgba(220,220,220,140));
}
QScrollArea {
    background: transparent;
    border: none;
}
QWidget#shopPage, QWidget#settingsPage, QWidget#shopInner {
    background: transparent;
}
QToolButton#shopItem {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255,255,255,120),
        stop:1 rgba(190,190,190,70));
    border: 1px solid rgba(255,255,255,180);
    border-radius: 12px;
    padding: 6px 4px 4px 4px;
    color: #000000;
    font-weight: 700;
    min-width: 96px;
    max-width: 112px;
}
QToolButton#shopItem:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255,255,255,190),
        stop:1 rgba(210,210,210,120));
}
QToolButton#shopItem:pressed {
    background: rgba(160,160,160,140);
}
QToolButton#shopItem:checked {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(220,235,255,210),
        stop:1 rgba(170,200,230,150));
    border: 2px solid rgba(90, 140, 200, 220);
}
QLabel#shopEmpty {
    color: #000000;
    font-weight: 600;
    padding: 24px;
}
"""


class ControlsWindow(QWidget):
    def __init__(self, aquarium: AquariumWindow):
        super().__init__()
        self.aq = aquarium
        self._drag = None
        self._blur = None
        self._virt = None
        self._shop_sig: tuple[str, ...] | None = None
        self.setWindowTitle("Akuarium Controls")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
        )
        self.setMinimumWidth(420)
        self.setMinimumHeight(520)
        self.setStyleSheet(AERO_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(8)

        title = QLabel("Akuarium")
        title.setObjectName("aeroTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        tabs = QTabWidget()
        tabs.addTab(self._build_shop_page(), "Shop")
        tabs.addTab(self._build_backgrounds_page(), "Backgrounds")
        tabs.addTab(self._build_settings_page(), "Settings")
        layout.addWidget(tabs, 1)

        # Shared bottom actions
        bh = QHBoxLayout()
        bh.setSpacing(8)
        btn_spawn_flora = QPushButton("Spawn Flora")
        btn_clear = QPushButton("Clear All")
        btn_exit = QPushButton("Exit")
        btn_exit.setObjectName("exitBtn")
        btn_spawn_flora.clicked.connect(aquarium.spawn_flora)
        btn_clear.clicked.connect(self._clear_all)
        btn_exit.clicked.connect(QApplication.quit)
        bh.addWidget(btn_spawn_flora)
        bh.addWidget(btn_clear)
        bh.addWidget(btn_exit)
        layout.addLayout(bh)

        self.refresh_shop()

        # Live-apply timer
        self.apply_timer = QTimer(self)
        self.apply_timer.timeout.connect(self._apply)
        self.apply_timer.start(200)

    def _build_shop_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("shopPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 8, 0, 0)
        page_layout.setSpacing(6)

        hint = QLabel("Click a fish to spawn it")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        page_layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.shop_inner = QWidget()
        self.shop_inner.setObjectName("shopInner")
        self.shop_grid = QGridLayout(self.shop_inner)
        self.shop_grid.setContentsMargins(4, 4, 4, 4)
        self.shop_grid.setSpacing(8)
        self.shop_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        scroll.setWidget(self.shop_inner)
        page_layout.addWidget(scroll, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("settingsPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 8, 0, 0)
        page_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        inner = QWidget()
        inner.setObjectName("settingsPage")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(2, 2, 8, 8)
        layout.setSpacing(10)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)

        aquarium = self.aq

        mg, mf = self._section("Movement")
        self.min_speed = self._dspin(1, 500, aquarium.cfg["min_speed"])
        self.max_speed = self._dspin(1, 500, aquarium.cfg["max_speed"])
        self.min_move = self._dspin(0.1, 30, aquarium.cfg["min_move_time"])
        self.max_move = self._dspin(0.1, 30, aquarium.cfg["max_move_time"])
        self.avoid_distance = self._dspin(0, 2000, aquarium.cfg["avoid_distance"])
        self.avoid_strength = self._dspin(0, 1000, aquarium.cfg["avoid_strength"])
        mf.addRow("Min speed", self.min_speed)
        mf.addRow("Max speed", self.max_speed)
        mf.addRow("Min move time", self.min_move)
        mf.addRow("Max move time", self.max_move)
        mf.addRow("Avoid distance", self.avoid_distance)
        mf.addRow("Avoid strength", self.avoid_strength)
        layout.addWidget(mg)

        sg, sf = self._section("Spawning")
        self.spawn_interval = self._dspin(0, 60, aquarium.cfg["spawn_interval"])
        self.max_fish = self._ispin(0, 500, aquarium.cfg["max_fish"])
        self.max_flora = self._ispin(0, 500, aquarium.cfg["max_flora"])
        sf.addRow("Spawn interval (s)", self.spawn_interval)
        sf.addRow("Max fish", self.max_fish)
        sf.addRow("Max flora", self.max_flora)
        layout.addWidget(sg)

        scg, scf = self._section("Scale")
        self.fish_scale = self._dspin(0.1, 5.0, aquarium.cfg["fish_scale"])
        self.flora_scale = self._dspin(0.1, 5.0, aquarium.cfg["flora_scale"])
        self.ground_scale = self._dspin(0.01, 5.0, aquarium.cfg["ground_scale"])
        scf.addRow("Fish scale", self.fish_scale)
        scf.addRow("Flora scale", self.flora_scale)
        scf.addRow("Ground scale", self.ground_scale)
        layout.addWidget(scg)

        gg, gf = self._section("Ground")
        self.ground_margin = self._ispin(0, 1000, aquarium.cfg["ground_margin"])
        gf.addRow("Ground margin (px)", self.ground_margin)
        layout.addWidget(gg)

        eg, ef = self._section("Effects")
        self.godrays_intensity = self._dspin(0.0, 2.0, aquarium.cfg["godrays_intensity"])
        self.frost_intensity = self._dspin(0.0, 2.0, aquarium.cfg["frost_intensity"])
        self.blur_intensity = self._dspin(0.0, 40.0, aquarium.cfg.get("blur_intensity", 0.0))
        self.blur_intensity.setToolTip("Softens the backdrop (desktop or background image)")
        ef.addRow("God rays", self.godrays_intensity)
        ef.addRow("Frost", self.frost_intensity)
        ef.addRow("Blur", self.blur_intensity)
        layout.addWidget(eg)

        fbg, fbf = self._section("Fan boost")
        self.fan_boost_speed_mult = self._dspin(
            1.0, 8.0, aquarium.cfg.get("fan_boost_speed_mult", 2.5),
        )
        self.fan_boost_speed_mult.setToolTip(
            "Speeds up fish, flora, bubbles, and rays while Cooler Boost (Fn+Up) is on"
        )
        self.fan_boost_rpm_on = self._dspin(
            1000.0, 12000.0, aquarium.cfg.get("fan_boost_rpm_on", 4800.0),
        )
        self.fan_boost_rpm_on.setToolTip(
            "RPM at/above which boost is assumed on (used when msi-ec cooler_boost is unavailable)"
        )
        fbf.addRow("Speed mult", self.fan_boost_speed_mult)
        fbf.addRow("Detect RPM", self.fan_boost_rpm_on)
        layout.addWidget(fbg)

        layout.addStretch(1)

        scroll.setWidget(inner)
        page_layout.addWidget(scroll)
        return page

    def _build_backgrounds_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("shopPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 8, 0, 0)
        page_layout.setSpacing(6)

        hint = QLabel("From backgrounds/ — None keeps the desktop visible")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        page_layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.bg_inner = QWidget()
        self.bg_inner.setObjectName("shopInner")
        self.bg_grid = QGridLayout(self.bg_inner)
        self.bg_grid.setContentsMargins(4, 4, 4, 4)
        self.bg_grid.setSpacing(8)
        self.bg_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._bg_group = QButtonGroup(self)
        self._bg_group.setExclusive(True)
        scroll.setWidget(self.bg_inner)
        page_layout.addWidget(scroll, 1)

        self.refresh_backgrounds()
        return page

    def refresh_backgrounds(self):
        if not hasattr(self, "bg_grid"):
            return
        while self.bg_grid.count():
            item = self.bg_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for btn in self._bg_group.buttons():
            self._bg_group.removeButton(btn)

        current = self.aq.cfg.get("background_path") or ""
        cols = 2
        none_btn = QToolButton()
        none_btn.setObjectName("shopItem")
        none_btn.setCheckable(True)
        none_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        none_btn.setText("None")
        none_btn.setToolTip("Transparent — show desktop behind the tank")
        none_btn.setIconSize(QSize(96, 54))
        none_btn.clicked.connect(lambda _=False: self.aq.set_background(None))
        self._bg_group.addButton(none_btn, 0)
        self.bg_grid.addWidget(none_btn, 0, 0)
        if not current:
            none_btn.setChecked(True)

        catalog = self.aq.background_catalog
        if not catalog:
            empty = QLabel("No images in backgrounds/")
            empty.setObjectName("shopEmpty")
            self.bg_grid.addWidget(empty, 1, 0, 1, cols)
            return

        for i, item in enumerate(catalog):
            btn = QToolButton()
            btn.setObjectName("shopItem")
            btn.setCheckable(True)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            thumb = item["pixmap"].scaled(
                96, 54,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            if thumb.width() > 96 or thumb.height() > 54:
                x = max(0, (thumb.width() - 96) // 2)
                y = max(0, (thumb.height() - 54) // 2)
                thumb = thumb.copy(x, y, 96, 54)
            btn.setIcon(QIcon(thumb))
            btn.setIconSize(QSize(96, 54))
            btn.setText(item["name"])
            btn.setToolTip(item["path"])
            path = item["path"]
            btn.clicked.connect(lambda _=False, p=path: self.aq.set_background(p))
            self._bg_group.addButton(btn, i + 1)
            row, col = (i + 1) // cols, (i + 1) % cols
            self.bg_grid.addWidget(btn, row, col)
            if path == current:
                btn.setChecked(True)

    def refresh_shop(self):
        sig = self.aq.fish_signature()
        if sig == self._shop_sig and self.shop_grid.count() > 0:
            return
        self._shop_sig = sig

        while self.shop_grid.count():
            item = self.shop_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        catalog = self.aq.shop_catalog()
        if not catalog:
            empty = QLabel("No items found in Fish/, Starfish/, or Flora/")
            empty.setObjectName("shopEmpty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.shop_grid.addWidget(empty, 0, 0)
            return

        cols = 3
        for i, fish in enumerate(catalog):
            btn = QToolButton()
            btn.setObjectName("shopItem")
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            thumb = fish["pixmap"].scaled(
                72, 72,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            btn.setIcon(QIcon(thumb))
            btn.setIconSize(QSize(72, 72))
            kind = fish.get("kind", "fish")
            btn.setText(fish["name"])
            btn.setToolTip(f"Spawn {fish['name']} ({kind})")
            path = fish["path"]
            btn.clicked.connect(lambda _=False, p=path: self.aq.spawn_fish_path(p))
            self.shop_grid.addWidget(btn, i // cols, i % cols)

    def capture_desktop(self):
        """Snapshot the desktop (without this window) and blur it."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        grab = screen.grabWindow(0)
        if grab.isNull():
            return
        w = max(1, grab.width() // 4)
        h = max(1, grab.height() // 4)
        small = grab.scaled(
            w, h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._blur = _blur_pixmap(small, 16)
        self._virt = screen.virtualGeometry()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, lambda: _clear_kwin_blur(int(self.winId())))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        radius = 22.0
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)

        # Soft frost + glass, all clipped to the same rounded path
        p.setClipPath(path)

        if self._blur is not None and not self._blur.isNull() and self._virt is not None:
            geo = self.frameGeometry()
            vg = self._virt
            bw, bh = self._blur.width(), self._blur.height()
            sx = (geo.x() - vg.x()) / max(1, vg.width()) * bw
            sy = (geo.y() - vg.y()) / max(1, vg.height()) * bh
            sw = geo.width() / max(1, vg.width()) * bw
            sh = geo.height() / max(1, vg.height()) * bh
            p.setOpacity(0.28)
            p.drawPixmap(r, self._blur, QRectF(sx, sy, sw, sh))
            p.setOpacity(1.0)

        body = QLinearGradient(r.topLeft(), r.bottomLeft())
        body.setColorAt(0.0, QColor(255, 255, 255, 72))
        body.setColorAt(0.45, QColor(230, 230, 230, 38))
        body.setColorAt(1.0, QColor(200, 200, 200, 48))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(body)
        p.drawPath(path)

        hi = QLinearGradient(0, r.top(), 0, r.top() + r.height() * 0.42)
        hi.setColorAt(0.0, QColor(255, 255, 255, 70))
        hi.setColorAt(0.55, QColor(255, 255, 255, 18))
        hi.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(hi)
        p.drawPath(path)

        p.setClipping(False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 200), 1.6))
        p.drawPath(path)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        super().mouseReleaseEvent(event)

    def _section(self, title: str):
        box = QGroupBox()
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        inner = QVBoxLayout(box)
        inner.setContentsMargins(8, 6, 8, 8)
        inner.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        form = QFormLayout()
        form.setSpacing(10)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        inner.addWidget(label)
        inner.addLayout(form)
        return box, form

    def _dspin(self, lo, hi, val):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(0.1 if hi <= 10 else 1.0)
        s.setDecimals(2)
        s.setValue(val)
        s.setMinimumHeight(28)
        s.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return s

    def _ispin(self, lo, hi, val):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(int(val))
        s.setMinimumHeight(28)
        s.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return s

    def _apply(self):
        c = self.aq.cfg
        c["min_speed"] = self.min_speed.value()
        c["max_speed"] = self.max_speed.value()
        c["min_move_time"] = self.min_move.value()
        c["max_move_time"] = self.max_move.value()
        c["avoid_distance"] = self.avoid_distance.value()
        c["avoid_strength"] = self.avoid_strength.value()
        c["spawn_interval"] = self.spawn_interval.value()
        c["max_fish"] = self.max_fish.value()
        c["max_flora"] = self.max_flora.value()
        c["fish_scale"] = self.fish_scale.value()
        c["flora_scale"] = self.flora_scale.value()
        old_ground = c["ground_scale"]
        c["ground_scale"] = self.ground_scale.value()
        c["ground_margin"] = self.ground_margin.value()
        c["godrays_intensity"] = self.godrays_intensity.value()
        c["frost_intensity"] = self.frost_intensity.value()
        old_blur = float(c.get("blur_intensity", 0.0) or 0.0)
        c["blur_intensity"] = self.blur_intensity.value()
        c["fan_boost_speed_mult"] = self.fan_boost_speed_mult.value()
        c["fan_boost_rpm_on"] = self.fan_boost_rpm_on.value()
        # Keep off-threshold a bit below on-threshold for hysteresis.
        c["fan_boost_rpm_off"] = max(1000.0, self.fan_boost_rpm_on.value() - 700.0)
        if abs(old_blur - c["blur_intensity"]) > 1e-6:
            self.aq._rebuild_blur_layer()
            self.aq.update()
        if abs(old_ground - c["ground_scale"]) > 1e-6:
            self.aq._spawn_ground()

    def _clear_all(self):
        self.aq.flora_sprites.clear()
        self.aq.fish_sprites.clear()
        self.aq.starfish_sprites.clear()
        self.aq.krustacean_sprites.clear()
        self.aq.bubble_sprites.clear()
        self.aq._drag_starfish = None
        self.aq._drag_flora = None

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_F12:
            self.hide()
        else:
            super().keyPressEvent(event)


# ── Entry point ────────────────────────────────────────────────────────────

def load_ground_catalog() -> list[dict]:
    items = []
    for p in sorted(glob.glob(os.path.join(GROUND_DIR, "*.png"))):
        pm = QPixmap(p)
        if pm.isNull():
            continue
        items.append({
            "path": p,
            "name": os.path.splitext(os.path.basename(p))[0],
            "pixmap": pm,
            "kind": "ground",
        })
    return items


class GroundPickerDialog(QDialog):
    """Startup dialog: pick which Ground/*.png to tile the floor with."""

    def __init__(self, catalog: list[dict]):
        super().__init__()
        self.setWindowTitle("Choose Ground")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Dialog
        )
        self.setStyleSheet(AERO_STYLE)
        self.setMinimumWidth(380)
        self.selected_path: str | None = catalog[0]["path"] if catalog else None
        self._drag = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        title = QLabel("Choose ground")
        title.setObjectName("aeroTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        hint = QLabel("Pick a floor style, then start")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(10)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        cols = 2
        for i, item in enumerate(catalog):
            btn = QToolButton()
            btn.setObjectName("shopItem")
            btn.setCheckable(True)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            thumb = item["pixmap"].scaled(
                96, 72,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            if thumb.width() > 96 or thumb.height() > 72:
                x = max(0, (thumb.width() - 96) // 2)
                y = max(0, (thumb.height() - 72) // 2)
                thumb = thumb.copy(x, y, 96, 72)
            btn.setIcon(QIcon(thumb))
            btn.setIconSize(QSize(96, 72))
            btn.setText(item["name"])
            btn.setToolTip(item["name"])
            path = item["path"]
            btn.clicked.connect(lambda _=False, p=path: self._select(p))
            self._group.addButton(btn, i)
            grid.addWidget(btn, i // cols, i % cols)
            if i == 0:
                btn.setChecked(True)
        layout.addLayout(grid)

        row = QHBoxLayout()
        row.setSpacing(8)
        btn_start = QPushButton("Start")
        btn_quit = QPushButton("Quit")
        btn_quit.setObjectName("exitBtn")
        btn_start.clicked.connect(self.accept)
        btn_start.setDefault(True)
        btn_quit.clicked.connect(self.reject)
        row.addWidget(btn_quit)
        row.addWidget(btn_start)
        layout.addLayout(row)

        self.adjustSize()

    def _select(self, path: str):
        self.selected_path = path

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        path = QPainterPath()
        path.addRoundedRect(r, 22.0, 22.0)
        p.setClipPath(path)

        body = QLinearGradient(r.topLeft(), r.bottomLeft())
        body.setColorAt(0.0, QColor(255, 255, 255, 88))
        body.setColorAt(0.45, QColor(230, 230, 230, 52))
        body.setColorAt(1.0, QColor(200, 200, 200, 64))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(body)
        p.drawPath(path)

        p.setClipping(False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 200), 1.6))
        p.drawPath(path)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)


def pick_ground() -> str | None:
    catalog = load_ground_catalog()
    if not catalog:
        return None
    dlg = GroundPickerDialog(catalog)
    # Center on primary screen
    screen = QApplication.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        dlg.adjustSize()
        dlg.move(
            geo.center().x() - dlg.width() // 2,
            geo.center().y() - dlg.height() // 2,
        )
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return dlg.selected_path


def main():
    # X11/XWayland lets KWin honor keep-below; Wayland often raises on click.
    if os.environ.get("DISPLAY") and "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "xcb"
    # Must be set before QApplication so the OpenGL paint engine is used.
    _configure_gpu_surface()
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseDesktopOpenGL, True)
    app = QApplication(sys.argv)
    ground_path = pick_ground()
    if ground_path is None and load_ground_catalog():
        # User quit the picker while grounds exist
        sys.exit(0)
    win = AquariumWindow(ground_path=ground_path)
    win.show()
    win._sink()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
