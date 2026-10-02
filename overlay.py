"""IO's focus glow: while a task runs, the window IO is working in gets a soft violet bloom with a field of tiny
twinkling pixel blocks around it, so you can see where it is acting.

The glow is invisible to IO itself: every overlay window is excluded from screen capture (IO's screenshots and
Windows-MCP's never contain it), click-through, never focusable, untitled and kept out of the taskbar and Alt+Tab.

Demo:  python overlay.py --demo "BlueStacks" [--seconds 15] [--capturable]
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import math
import os
import sys
import threading
import time

import numpy as np

# private DLL handles: argtypes set here must not change how boss.py's ctypes.windll calls convert
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
dwmapi = ctypes.WinDLL("dwmapi")
kernel32 = ctypes.WinDLL("kernel32")

H = ctypes.c_void_p
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, H, wt.UINT, wt.WPARAM, wt.LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, H, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", H), ("hIcon", H), ("hCursor", H), ("hbrBackground", H),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR), ("hIconSm", H)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def _proto(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, list(argtypes)


_proto(user32.RegisterClassExW, wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
_proto(user32.CreateWindowExW, H, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, H, H, H, H)
_proto(user32.DefWindowProcW, LRESULT, H, wt.UINT, wt.WPARAM, wt.LPARAM)
_proto(user32.DestroyWindow, wt.BOOL, H)
_proto(user32.ShowWindow, wt.BOOL, H, ctypes.c_int)
_proto(user32.SetWindowPos, wt.BOOL, H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
_proto(user32.UpdateLayeredWindow, wt.BOOL, H, H, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), H,
       ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD)
_proto(user32.PeekMessageW, wt.BOOL, ctypes.POINTER(wt.MSG), H, wt.UINT, wt.UINT, wt.UINT)
_proto(user32.TranslateMessage, wt.BOOL, ctypes.POINTER(wt.MSG))
_proto(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wt.MSG))
_proto(user32.SetWindowDisplayAffinity, wt.BOOL, H, wt.DWORD)
_proto(user32.GetForegroundWindow, H)
_proto(user32.GetWindowTextLengthW, ctypes.c_int, H)
_proto(user32.GetWindowTextW, ctypes.c_int, H, wt.LPWSTR, ctypes.c_int)
_proto(user32.GetClassNameW, ctypes.c_int, H, wt.LPWSTR, ctypes.c_int)
_proto(user32.GetWindowLongPtrW, ctypes.c_ssize_t, H, ctypes.c_int)
_proto(user32.IsWindow, wt.BOOL, H)
_proto(user32.IsWindowVisible, wt.BOOL, H)
_proto(user32.IsIconic, wt.BOOL, H)
_proto(user32.IsZoomed, wt.BOOL, H)
_proto(user32.GetWindowThreadProcessId, wt.DWORD, H, ctypes.POINTER(wt.DWORD))
_proto(user32.EnumWindows, wt.BOOL, ENUMPROC, wt.LPARAM)
_proto(user32.MonitorFromWindow, H, H, wt.DWORD)
_proto(user32.GetMonitorInfoW, wt.BOOL, H, ctypes.POINTER(MONITORINFO))
_proto(user32.GetDpiForWindow, wt.UINT, H)
_proto(user32.SetThreadDpiAwarenessContext, H, H)
_proto(gdi32.CreateCompatibleDC, H, H)
_proto(gdi32.CreateDIBSection, H, H, ctypes.POINTER(BITMAPINFOHEADER), wt.UINT, ctypes.POINTER(ctypes.c_void_p), H, wt.DWORD)
_proto(gdi32.SelectObject, H, H, H)
_proto(gdi32.DeleteObject, wt.BOOL, H)
_proto(gdi32.DeleteDC, wt.BOOL, H)
_proto(gdi32.GdiFlush, wt.BOOL)
_proto(dwmapi.DwmGetWindowAttribute, ctypes.c_long, H, wt.DWORD, ctypes.c_void_p, wt.DWORD)
_proto(kernel32.GetModuleHandleW, H, wt.LPCWSTR)

CLASS = "IOFocusGlow"
WS_POPUP = 0x80000000
# layered (per-pixel alpha), transparent (clicks fall through), tool window (no taskbar / Alt+Tab), topmost, never activated
EX_STYLE = 0x00080000 | 0x00000020 | 0x00000080 | 0x00000008 | 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WDA_EXCLUDEFROMCAPTURE = 0x11
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = H(-1)
SWP_FLAGS = 0x0001 | 0x0002 | 0x0010  # no size, no move, no activate
ULW_ALPHA = 2
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
SKIP_CLASSES = {CLASS, "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow",
                "XamlExplorerHostIslandWindow", "TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow"}

FPS = 30
POLL = 0.1  # how often the target window is looked up again
GLOW, EXT = 48, 30  # how far the bloom and the pixel blocks reach out from the edge, at 100% scaling
INNER = 0.6  # inward glows (maximized and snapped windows) reach less far, so they don't veil the window's content
# palette (RGB), lightest first: near-white, lavenders, violets, deep purple; panel.html's --spark-* and --accent
# tones, so the glow and the app read as one violet
LAVENDER, VIOLET, DEEP, WHITE = (196, 181, 251), (154, 128, 245), (84, 52, 204), (248, 246, 255)
PALETTE = np.array([WHITE, (221, 213, 253), LAVENDER, (185, 168, 251), VIOLET, (124, 92, 240), (109, 74, 227), DEEP], np.float32)

_wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))  # kept alive for the class's lifetime


def _text(hwnd, fn=user32.GetWindowTextW) -> str:
    buf = ctypes.create_unicode_buffer(256)
    fn(hwnd, buf, 256)
    return buf.value


def _frame(hwnd) -> tuple[int, int, int, int] | None:
    """The visible bounds of a window (no invisible resize borders), in physical pixels."""
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        return None
    return r.left, r.top, r.right, r.bottom


def _eligible(hwnd) -> bool:
    """A real app window the glow may outline: never IO itself, the desktop, the taskbar, shell flyouts or the glow."""
    if not hwnd or not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid() or user32.GetWindowLongPtrW(hwnd, -20) & WS_EX_TOOLWINDOW:  # GWL_EXSTYLE
        return False
    cloaked = ctypes.c_int()
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4)
    title = _text(hwnd)
    if cloaked.value or not title or title == "IO" or _text(hwnd, user32.GetClassNameW) in SKIP_CLASSES:
        return False
    r = _frame(hwnd)
    return bool(r) and r[2] - r[0] > 50 and r[3] - r[1] > 50


def _find(title: str):
    """The frontmost eligible window whose title contains `title` (case-insensitive)."""
    low, found = title.lower(), []

    def cb(hwnd, _):
        if low in _text(hwnd).lower() and _eligible(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(ENUMPROC(cb), 0)
    return found[0] if found else None


def _sdf(x, y, w: float, h: float, r: float):
    """Signed distance from pixel centres to a w x h rounded rect at the origin (positive outside)."""
    qx = np.abs(x - w / 2) - (w / 2 - r)
    qy = np.abs(y - h / 2) - (h / 2 - r)
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r


def _around(x, y, w: float, h: float):
    """Position around the rect's perimeter, 0..1 clockwise from the right."""
    return (np.arctan2((y - h / 2) / h, (x - w / 2) / w) / (2 * math.pi)) % 1.0


def _pack(rgb, a):
    """Premultiplied BGRA as uint32 from RGB 0-255 and alpha 0-1."""
    return _pack4(_bgra(rgb), a)


def _bgra(rgb):
    """RGB rows as float BGRA rows with alpha 255, ready for _pack4."""
    out = np.full((len(rgb), 4), 255, np.float32)
    out[:, :3] = np.asarray(rgb, np.float32)[:, ::-1]
    return out


def _pack4(bgra, a):
    return (bgra * np.clip(a, 0, 1)[:, None].astype(np.float32) + 0.5).astype(np.uint8).view(np.uint32).ravel()


def _hash(i, j, salt: int, k: int):
    """Stable pseudo-random 0..1 per grid cell, so the block field keeps its pattern as the window moves."""
    h = (i.astype(np.int64) * 0x9E3779B1 + j.astype(np.int64) * 0x85EBCA77 + (salt * 31 + k) * 0xC2B2AE3D) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2C1B3C6D) & 0xFFFFFFFF
    h ^= h >> 12
    h = (h * 0x297A2D39) & 0xFFFFFFFF
    h ^= h >> 15
    return h.astype(np.float64) / 2 ** 32


def _glint(s, t: float):
    """Two soft lights drifting slowly around the rim, on opposite sides."""
    p = ((s - t / 9.0) * 2) % 1.0
    dist = np.minimum(p, 1 - p) / 2
    return np.exp(-(dist / 0.045) ** 2)


# the rim's colours by position around it x edge softness; the lights only drift, so each frame just rotates it
RIM_BINS, RIM_LEVELS = 1024, 8
_g = _glint((np.arange(RIM_BINS) + 0.5) / RIM_BINS, 0.0)
RIM = _pack(np.repeat(np.array(LAVENDER, np.float32) + (np.array(WHITE, np.float32) - LAVENDER) * _g[:, None], RIM_LEVELS, 0),
            ((0.7 + 0.3 * _g)[:, None] * (np.arange(RIM_LEVELS) / (RIM_LEVELS - 1))[None, :]).ravel()).reshape(RIM_BINS, RIM_LEVELS)


def _rim_table(t: float):
    return np.roll(RIM, int(t / 9.0 * RIM_BINS) % RIM_BINS, axis=0).ravel()


class Surface:
    """One layered, click-through, capture-excluded window with a top-down 32-bit DIB to draw into."""

    def __init__(self, capturable: bool) -> None:
        self.hwnd = user32.CreateWindowExW(EX_STYLE, CLASS, None, WS_POPUP, 0, 0, 1, 1, None, None, kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            raise OSError(f"CreateWindowEx failed ({ctypes.get_last_error()})")
        if not capturable and not user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE):
            raise OSError(f"can't exclude the glow from screen capture ({ctypes.get_last_error()})")
        self.dc = gdi32.CreateCompatibleDC(None)
        self.bmp = self.old = None
        self.size = (0, 0)
        self.pos, self.level = (0, 0), -1
        self.shown = False
        self.px = None

    def canvas(self, w: int, h: int):
        """A cleared (h, w) uint32 pixel view of the window's bitmap, resized if needed."""
        if (w, h) != self.size:
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            bits = ctypes.c_void_p()
            bmp = gdi32.CreateDIBSection(None, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            if not bmp:
                raise OSError("CreateDIBSection failed")
            old = gdi32.SelectObject(self.dc, bmp)
            if self.bmp:
                gdi32.DeleteObject(self.bmp)
            else:
                self.old = old
            self.bmp, self.size = bmp, (w, h)
            self.px = np.ctypeslib.as_array((ctypes.c_uint32 * (w * h)).from_address(bits.value)).reshape(h, w)
        self.px[:] = 0
        return self.px

    def push(self, x: int, y: int, alpha: float, pixels: bool = True) -> None:
        """Puts the window at (x, y) with an overall opacity; pixels=False keeps the last bitmap (cheap)."""
        level = max(0, min(255, int(alpha * 255 + 0.5)))
        if not pixels and self.shown and (x, y, level) == (*self.pos, self.level):
            return
        blend = BLENDFUNCTION(0, 0, level, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        pos = wt.POINT(x, y)
        if pixels:
            gdi32.GdiFlush()
            ok = user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(pos), ctypes.byref(wt.SIZE(*self.size)), self.dc,
                                            ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
        else:
            ok = user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(pos) if (x, y) != self.pos else None, None, None,
                                            None, 0, ctypes.byref(blend), ULW_ALPHA)
        if not ok:
            raise OSError(f"UpdateLayeredWindow failed ({ctypes.get_last_error()})")
        self.pos, self.level = (x, y), level
        if not self.shown:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
            self.shown = True

    def hide(self) -> None:
        if self.shown:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.shown = False

    def destroy(self) -> None:
        user32.DestroyWindow(self.hwnd)
        if self.bmp:
            gdi32.SelectObject(self.dc, self.old)
            gdi32.DeleteObject(self.bmp)
        gdi32.DeleteDC(self.dc)


class Sparkle:
    """The animated layer of one border strip: the rim and the twinkling blocks, as fixed pixel indices into the strip."""

    def __init__(self, side: int, rect, w: int, h: int, r: int, s: float, ext: int, inner: bool) -> None:
        x0, y0, x1, y1 = rect
        sw = x1 - x0
        sign = -1 if inner else 1  # distances count inward for a glow drawn inside the window
        # rim: a thin bright line hugging the window's (rounded) edge
        ys, xs = np.mgrid[y0:y1, x0:x1]
        d = sign * _sdf(xs + 0.5, ys + 0.5, w, h, r)
        on = (d > -1.0) & (d < 2.4 * s)
        self.rim = np.flatnonzero(on)
        weight = np.clip(1 - np.abs(d.ravel()[self.rim] - 0.5) / (1.0 + 0.8 * s), 0, 1) ** 1.6
        at = _around(xs.ravel()[self.rim] + 0.5, ys.ravel()[self.rim] + 0.5, w, h)
        self.rim_key = (np.minimum(at * RIM_BINS, RIM_BINS - 1).astype(np.int32) * RIM_LEVELS
                        + np.rint(weight * (RIM_LEVELS - 1)).astype(np.int32))

        # pixel blocks on a grid anchored to this strip's edge, so they ride along with it: dense at the edge,
        # thinning out with distance, a few cells left empty or holding a smaller block
        c = max(4, round(4.5 * s))
        gap = max(1, round(0.9 * s))
        span = (x0, x1) if side < 2 else (y0, y1)
        if inner:
            out = {0: (y0, y1), 1: (h - y1, h - y0), 2: (x0, x1), 3: (w - x1, w - x0)}[side]
        else:
            out = {0: (-y1, -y0), 1: (y0 - h, y1 - h), 2: (-x1, -x0), 3: (x0 - w, x1 - w)}[side]
        i, j = np.meshgrid(np.arange(span[0] // c, span[1] // c + 1), np.arange(out[0] // c, out[1] // c + 1))
        i, j = i.ravel(), j.ravel()
        rnd = [_hash(i, j, side, k) for k in range(8)]
        k = np.where(rnd[0] < 0.22, c - 2 * gap, c - gap).astype(int)
        u = i * c + (rnd[1] * (c - gap - k + 1)).astype(int)
        v = j * c + gap
        if inner:
            bx, by = {0: (u, v), 1: (u, h - v - k), 2: (v, u), 3: (w - v - k, u)}[side]
        else:
            bx, by = {0: (u, -v - k), 1: (u, h + v), 2: (-v - k, u), 3: (w + v, u)}[side]
        dc = sign * _sdf(bx + k / 2, by + k / 2, w, h, r)
        t = np.clip(dc / ext, 0, 1)
        keep = ((dc >= 0.75 * k + 1.5 * s) & (rnd[3] < 0.97 * (1 - t) ** 1.3)
                & (bx >= x0) & (by >= y0) & (bx + k <= x1) & (by + k <= y1))
        bx, by, k, t = bx[keep], by[keep], k[keep], t[keep]
        rnd = [q[keep] for q in rnd]
        # near the edge mostly lavender and white, further out violet and deep purple
        pick = np.clip((rnd[4] * 0.5 + t * 0.55) * len(PALETTE), 1, len(PALETTE) - 1).astype(int)
        pick[rnd[5] < 0.07 * (1 - t)] = 0
        self.col = PALETTE[pick]
        self.amp = ((1 - t) ** 0.6 * (0.6 + 0.4 * rnd[6])).astype(np.float32)
        self.amp[pick == 0] = np.minimum(1, self.amp[pick == 0] * 1.3)
        self.omega = (2 * math.pi * (0.15 + 0.35 * rnd[7])).astype(np.float32)
        self.phase = (rnd[5] * 2 * math.pi * 7.3 % (2 * math.pi)).astype(np.float32)
        self.reveal = (0.6 * t + 0.25 * rnd[2]).astype(np.float32)  # when it sparkles in: the field grows out from the rim
        self.bs = _around(bx + k / 2, by + k / 2, w, h)
        kmax = int(k.max()) if len(k) else 1
        oy, ox = np.mgrid[0:kmax, 0:kmax]
        mask = (ox.ravel()[None, :] < k[:, None]) & (oy.ravel()[None, :] < k[:, None])
        pix = (by[:, None] - y0 + oy.ravel()[None, :]) * sw + (bx[:, None] - x0 + ox.ravel()[None, :])
        self.pix = pix[mask]
        self.blk = np.repeat(np.arange(len(k)), mask.sum(1))

    def draw(self, px, blocks, rim) -> None:
        """blocks: this frame's colour of every block (all strips); rim: _rim_table."""
        flat = px.reshape(-1)
        flat[self.pix] = blocks[self.blk]
        flat[self.rim] = rim[self.rim_key]


class Field:
    """Every strip's blocks in one set of arrays, so each frame's twinkle is a handful of numpy calls."""

    def __init__(self, sparks: list) -> None:
        n = 0
        for sp in sparks:
            sp.blk = sp.blk + n
            n += len(sp.amp)
        for name in ("col", "amp", "omega", "phase", "reveal", "bs"):
            setattr(self, name, np.concatenate([getattr(sp, name) for sp in sparks]) if sparks else np.zeros((0, 3) if name == "col" else 0, np.float32))
        self.col = _bgra(self.col)

    def colors(self, t: float, shown: float = 1.0):
        """Each block fades in and out on its own slow phase, and brightens as a rim light passes. While the glow fades
        in (shown 0..1) the blocks sparkle in from the rim outward; fading out, the outermost go first."""
        tw = 0.5 + 0.5 * np.sin(self.omega * t + self.phase)
        a = self.amp * (0.3 + 0.7 * tw * tw * np.sqrt(tw)) * (1 + 0.6 * _glint(self.bs, t))
        if shown < 1:
            a *= np.clip((shown - self.reveal) / 0.15, 0, 1)
        return _pack4(self.col, a)


class Glow:
    def __init__(self, hint=None, capturable: bool = False) -> None:
        self.hint = hint
        self.capturable = capturable
        self.title = ""
        self.visible = False
        self.alive = True
        self.failed = False
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._main, name="io-focus-glow", daemon=True)

    def _main(self) -> None:
        surfaces: list[Surface] = []
        try:
            user32.SetThreadDpiAwarenessContext(H(-4))  # physical pixels, like boss.py and Windows-MCP
            wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, _wndproc, 0, 0, kernel32.GetModuleHandleW(None), None, None,
                             None, None, CLASS, None)
            if not user32.RegisterClassExW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:  # already registered
                raise OSError(f"RegisterClassEx failed ({ctypes.get_last_error()})")
            # 4 bloom windows (redrawn only when the size changes; breathing is their overall opacity) under 4 sparkle ones
            for _ in range(8):
                surfaces.append(Surface(self.capturable))
            fails, last = 0, 0.0
            while self.alive:
                try:
                    self._loop(surfaces[:4], surfaces[4:])
                except Exception as e:
                    # a frame can fail now and then (a display change, a locked session): skip a beat and carry on
                    fails = fails + 1 if time.perf_counter() - last < 60 else 1
                    last = time.perf_counter()
                    if fails >= 5:
                        raise
                    print(f"focus glow: frame failed, retrying: {e!r}", file=sys.stderr)
                    for sf in surfaces:
                        sf.hide()
                    self.wake.wait(1.0)
                    self.wake.clear()
        except Exception as e:
            self.failed = True
            print(f"focus glow disabled: {e!r}", file=sys.stderr)
        finally:
            for sf in surfaces:
                try:
                    sf.destroy()
                except Exception:
                    pass

    def _goal(self, hwnd):
        """Where the glow goes for this window: (rect, corner radius, scale, clip rect, inside), or None if it's gone."""
        if not hwnd or not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None
        rect = _frame(hwnd)
        if not rect:
            return None
        s = (user32.GetDpiForWindow(hwnd) or 96) / 96
        mi = MONITORINFO(ctypes.sizeof(MONITORINFO))
        user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
        m, wa = mi.rcMonitor, mi.rcWork
        mon = (m.left, m.top, m.right, m.bottom)
        l, t, r, b = rect
        if not (l >= mon[0] - 1 and t >= mon[1] - 1 and r <= mon[2] + 1 and b <= mon[3] + 1):
            return rect, round(8 * s), s, None, False  # spans monitors: draw it all
        # Windows 11 rounds the corners of normal windows. Maximized, snapped and full-screen ones are square and have
        # no room around them on some sides, so their glow shines inward from the edges instead
        flush = sum((l <= wa.left + 1, t <= wa.top + 1, r >= wa.right - 1, b >= wa.bottom - 1))
        if user32.IsZoomed(hwnd) or flush >= 2 or (r - l >= 0.9 * (mon[2] - mon[0]) and b - t >= 0.9 * (mon[3] - mon[1])):
            return rect, 0, s, mon, True
        return rect, round(8 * s), s, mon, False

    def _pick(self, prev):
        title = self.title or (self.hint() if self.hint else "") or ""
        if title and (hwnd := _find(title)):
            return hwnd
        fg = user32.GetForegroundWindow()
        if _eligible(fg):
            return fg
        return prev if _eligible(prev) else None  # e.g. you clicked IO's own window: keep showing where it works

    @staticmethod
    def _strips(w: int, h: int, r: int, width: int, clip, inner: bool) -> list:
        """The four border strips around (or just inside) a w x h rect: top, bottom, left, right, relative to its
        corner and clipped to its monitor; outside ones reach into the corners' curve so the glow can hug it."""
        ri = max(r, 2)
        if inner:
            rects = [(0, 0, w, width), (0, h - width, w, h), (0, width, width, h - width), (w - width, width, w, h - width)]
        else:
            rects = [(-width, -width, w + width, ri), (-width, h - ri, w + width, h + width),
                     (-width, ri, ri, h - ri), (w - ri, ri, w + width, h - ri)]
        if clip:
            rects = [(max(a, clip[0]), max(b, clip[1]), min(c, clip[2]), min(d, clip[3])) for a, b, c, d in rects]
        return [rc if rc[2] - rc[0] > 0 and rc[3] - rc[1] > 0 else None for rc in rects]

    @staticmethod
    def _lut(glow: int, s: float):
        """Bloom colour and opacity by distance from the edge (quarter-pixel steps), premultiplied."""
        dist = np.arange(glow * 4 + 1, dtype=np.float32) / 4
        # three falloffs layered into a soft bloom, eased out to nothing at the outer edge
        a = 0.55 * np.exp(-dist / (3.5 * s)) + 0.4 * np.exp(-dist / (12 * s)) + 0.3 * np.exp(-dist / (26 * s))
        q = np.clip((dist - 0.45 * glow) / (0.55 * glow), 0, 1)
        a *= 1 - q * q * (3 - 2 * q)
        f = np.clip(dist / (0.6 * glow), 0, 1)[:, None]
        lav, vio, deep = (np.array(c, np.float32) for c in (LAVENDER, VIOLET, DEEP))
        rgb = np.where(f < 0.3, lav + (vio - lav) * (f / 0.3), vio + (deep - vio) * ((f - 0.3) / 0.7) * 0.6)
        return _pack(rgb.astype(np.float32), a)

    def _layout(self, bloom, spark, w: int, h: int, r: int, s: float, reach: tuple, strips, inner: bool) -> tuple[list, Field]:
        """Draws the bloom strips and builds the sparkle strips: [(rect, Sparkle or None) or None] per window, and
        the sparkle strips' block field."""
        lut = self._lut(reach[0], s)
        made = []
        for i, (sf, rc) in enumerate(zip(bloom + spark, strips)):
            if not rc:
                sf.hide()
                made.append(None)
                continue
            px = sf.canvas(rc[2] - rc[0], rc[3] - rc[1])
            if i < 4:
                xs = np.arange(rc[0], rc[2], dtype=np.float32)[None, :] + 0.5
                ys = np.arange(rc[1], rc[3], dtype=np.float32)[:, None] + 0.5
                d = -_sdf(xs, ys, w, h, r) if inner else _sdf(xs, ys, w, h, r)
                px[:] = np.where(d >= 0, lut[np.clip((d * 4).astype(np.int32), 0, len(lut) - 1)], 0)
                made.append((rc, None))
            else:
                made.append((rc, Sparkle(i - 4, rc, w, h, r, s, reach[1], inner)))
        return made, Field([m[1] for m in made if m and m[1]])

    def _loop(self, bloom, spark) -> None:
        msg = wt.MSG()
        hwnd = goal = None
        rect = None  # the eased rect (floats) the glow is drawn around
        r, s, mon, inner = 0, 1.0, None, False
        fade = 0.0
        key = None
        made: list = []
        field = None
        switched = next_poll = 0.0
        t0 = last = time.perf_counter()
        while self.alive:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            now = time.perf_counter()
            dt, last = min(0.1, now - last), now
            if self.visible and now >= next_poll:
                next_poll = now + POLL
                if (picked := self._pick(hwnd)) != hwnd:
                    hwnd, switched = picked, now
            goal = self._goal(hwnd) if self.visible else None
            fade = min(1.0, fade + dt / 0.7) if goal else max(0.0, fade - dt / 0.8)
            if fade <= 0:
                for sf in bloom + spark:
                    sf.hide()
                rect = hwnd = None
                self.wake.wait(POLL)
                self.wake.clear()
                continue
            gliding = False
            if goal:
                gl, gt, gr, gb = goal[0]
                target = np.array((gl, gt, gr - gl, gb - gt), np.float64)  # position and size ease apart, so a drag never resizes
                if rect is None:
                    rect = target.copy()
                else:
                    # glide over to a newly picked window, follow a dragged one closely
                    rect += (target - rect) * (1 - math.exp(-dt * (8.0 if now - switched < 1.0 else 30.0)))
                    gliding = np.abs(target - rect).max() >= 0.5
                    if not gliding:
                        rect = target.copy()
                _, r, s, mon, inner = goal
            l, t, w, h = (int(round(v)) for v in rect)
            # unclipped while gliding, so the glow can travel between monitors
            clip = (mon[0] - l, mon[1] - t, mon[2] - l, mon[3] - t) if mon and not gliding else None
            reach = (round(GLOW * s * (INNER if inner else 1)), round(EXT * s * (INNER if inner else 1)))
            strips = self._strips(w, h, r, reach[0], clip, inner) + self._strips(w, h, r, reach[1] + 2, clip, inner)
            fresh = (w, h, r, s, strips) != key
            if fresh:
                key = (w, h, r, s, strips)
                made, field = self._layout(bloom, spark, w, h, r, s, reach, strips, inner)
            e = fade * fade * (3 - 2 * fade)  # smoothstep
            breath = 0.8 + 0.2 * math.sin((now - t0) * 2 * math.pi / 4.8)
            rim, blocks = _rim_table(now - t0), field.colors(now - t0, e)
            for i, sf in enumerate(bloom + spark):
                if made[i]:
                    rc, sp = made[i]
                    if sp:
                        sp.draw(sf.px, blocks, rim)
                    # the rim lights up early; the blocks then sparkle in on their own (Field.colors)
                    sf.push(l + rc[0], t + rc[1], min(1.0, 2.5 * e) if sp else e * breath, pixels=bool(sp) or fresh)
            time.sleep(max(0.0, 1 / FPS - (time.perf_counter() - now)))


_glow: Glow | None = None


def _safe(fn):
    """The glow is decoration: its API never raises into IO."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(f"focus glow: {fn.__name__} failed: {e!r}", file=sys.stderr)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


@_safe
def start(hint=None, capturable: bool = False) -> None:
    """Starts the glow's thread (hidden). hint: callable returning the title (part) of the window IO is working in, or ''.
    capturable: let screenshots see it (for reviewing the look only). After a failure it stays off until stop()."""
    global _glow
    if _glow is None:
        _glow = Glow(hint, capturable)
        _glow.thread.start()


@_safe
def show() -> None:
    """Fades the glow in around the window IO is working in."""
    if _glow:
        _glow.visible = True
        _glow.wake.set()


@_safe
def hide() -> None:
    """Fades the glow out."""
    if _glow:
        _glow.visible = False


@_safe
def set_target_title(title: str) -> None:
    """Pins the glow to the window whose title contains this ('' goes back to the hint / the foreground window)."""
    if _glow:
        _glow.title = title or ""
        _glow.wake.set()


@_safe
def stop() -> None:
    global _glow
    if _glow:
        _glow.alive = False
        _glow.wake.set()
        _glow.thread.join(2)
        _glow = None


def main() -> None:
    ap = argparse.ArgumentParser(description="Shows IO's focus glow around a window.")
    ap.add_argument("--demo", metavar="TITLE", required=True, help="part of the window's title ('' = the foreground window)")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--capturable", action="store_true", help="let screenshots see the glow (for review only)")
    a = ap.parse_args()
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    start(capturable=a.capturable)
    set_target_title(a.demo)
    show()
    cpu, t = time.process_time(), time.perf_counter()
    time.sleep(max(0.0, a.seconds - 0.8))
    hide()
    time.sleep(0.8)
    print(f"cpu {100 * (time.process_time() - cpu) / (time.perf_counter() - t):.1f}% of one core over {a.seconds:g}s"
          + (" (the glow failed, see above)" if _glow and _glow.failed else ""))
    stop()


if __name__ == "__main__":
    main()
