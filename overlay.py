"""IO's focus glow: while a task runs, the window IO is working in glows outward, a soft violet bloom shining through a
grid of tiny twinkling pixel squares, so you can see where it is acting.

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
GLOW, EXT = 40, 30  # how far the bloom and the pixel grid reach out from the edge, at 100% scaling
INNER = 0.6  # inward glows (maximized and snapped windows) reach less far, so they don't veil the window's content
RIM_GLOW = 0.6  # the rim's brightness between pulses (it brightens as each band of light leaves it)
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
    """Stable pseudo-random 0..1 per grid square, so the grid keeps its pattern as the window moves or resizes."""
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


# the rim's colours by position around it x edge softness, at BRIGHT + 1 brightness steps; its lights only drift, so
# a frame just picks a brightness and rotates it
RIM_BINS, RIM_LEVELS, BRIGHT = 1024, 8, 16
_g = _glint((np.arange(RIM_BINS) + 0.5) / RIM_BINS, 0.0)
_rim = (_bgra(np.repeat(np.array(LAVENDER, np.float32) + (np.array(WHITE, np.float32) - LAVENDER) * _g[:, None], RIM_LEVELS, 0))
        * ((0.7 + 0.3 * _g)[:, None] * (np.arange(RIM_LEVELS) / (RIM_LEVELS - 1))[None, :]).reshape(-1, 1))
RIM = np.concatenate([(_rim * (b / BRIGHT) + 0.5).astype(np.uint8).view(np.uint32)[:, 0] for b in range(BRIGHT + 1)]).reshape(-1, RIM_LEVELS)
_BINS = np.arange(RIM_BINS)


def _rim_table(t: float, f: float = 1.0):
    """The rim's colours this frame, at brightness f (0..1)."""
    rows = (_BINS - int(t / 9.0 * RIM_BINS)) % RIM_BINS + int(min(max(f, 0.0), 1.0) * BRIGHT + 0.5) * RIM_BINS
    return RIM[rows].ravel()


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
        self.px = self.flat = None

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
            self.flat = np.ctypeslib.as_array((ctypes.c_uint32 * (w * h)).from_address(bits.value))
            self.px = self.flat.reshape(h, w)
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


SQUARE, GAP = 2, 1  # the grid's squares and the gaps between them, px at 100%: panel.html's sparkle while a chat runs
WAVE = 2.8  # s between the soft bands of light that leave the rim and travel out through the grid
DSTEPS, TSTEPS, LEVELS = 64, 64, 32  # table steps: distance out from the edge, twinkle phase, opacity
DIST = (np.arange(DSTEPS) + 0.5) / DSTEPS  # a step's fraction of the way out
SHINE = (0.85 * (1 - DIST) ** 1.6).astype(np.float32)  # brightest at the edge, dimming outward
_tw = 0.5 + 0.5 * np.sin(np.arange(TSTEPS) * (2 * math.pi / TSTEPS))
TWINKLE = (0.3 + 0.7 * _tw ** 3).astype(np.float32)  # like the app's: mostly soft, now and then bright, never out
CREST = 0.4  # how much a band of light adds as it passes, fading as it travels out
# a square's premultiplied colour by palette entry x opacity: the brightest turn whiter, like light running hot
_a = np.tile(np.arange(LEVELS) / (LEVELS - 1), len(PALETTE))
_rgb = np.repeat(PALETTE, LEVELS, 0)
SQUARE_COL = _pack(_rgb + (np.array(WHITE, np.float32) - _rgb) * (0.7 * np.clip((_a - 0.5) / 0.5, 0, 1) ** 1.5)[:, None], _a)


def _lane(n: int, k: int, p: int, g: int, reach: int, inner: bool) -> tuple:
    """Square positions along one axis of a side n px long, squares k px on a pitch of p: the half nearer 0 lined up
    with the edge at 0 and the other half with the edge at n, counting out from it (the first square g px off it)
    past it by reach, or inside it. The halves meet mid-side a pitch or a little more apart. Returns the positions,
    each one's index from its edge (the far half's offset), and which lie in the band along the edges."""
    m = n // 2 // p + 2
    if inner:
        jn = jf = np.arange(m)
        near, far = g + jn * p, n - g - k - jf * p
    else:
        jn = jf = np.arange(-m, reach // p + 2)
        near, far = -(g + k) - jn * p, n + g + jf * p
    on = near + k / 2 < n / 2
    near, jn = near[on], jn[on]
    on = (far + k / 2 >= n / 2) & (far >= near.max() + p)
    far, jf = far[on], jf[on]
    pos = np.concatenate((near, far))
    mid = pos + k / 2
    return pos, np.concatenate((jn, jf + (1 << 16))), ((mid <= reach) | (mid >= n - reach)) if inner else ((mid < 0) | (mid > n))


def _grid(w: int, h: int, r: int, s: float, reach: int, inner: bool) -> tuple:
    """The glow's pixel grid around (or, inner, just inside) a w x h window: uniform squares on a fixed pitch, filling
    a band reach px deep, each quarter of it lined up with the two edges it touches so it hugs every side alike.
    Returns the squares' left and top, their size, their centres' distance out from the edge (fraction of reach),
    and a stable random number generator for them."""
    k = max(1, int(SQUARE * s + 0.5))
    p = k + max(1, int(GAP * s + 0.5))
    g = max(1, int(2 * s + 0.5))  # the rim's room
    xs, ix, ex = _lane(w, k, p, g, reach, inner)
    ys, iy, ey = _lane(h, k, p, g, reach, inner)
    # the bands along the top and bottom (corners included), then the sides between them
    x = np.concatenate((np.tile(xs, ey.sum()), np.repeat(xs[ex], (~ey).sum())))
    y = np.concatenate((np.repeat(ys[ey], len(xs)), np.tile(ys[~ey], ex.sum())))
    i = np.concatenate((np.tile(ix, ey.sum()), np.repeat(ix[ex], (~ey).sum())))
    j = np.concatenate((np.repeat(iy[ey], len(xs)), np.tile(iy[~ey], ex.sum())))
    # distance from a squarer corner than the window's, so the grid wraps its corners about as fully as its sides
    d = (-1 if inner else 1) * _sdf(x + k / 2, y + k / 2, w, h, r / 3)
    on = (d >= g + k / 2 - 0.75) & (d <= reach)
    return x[on], y[on], k, d[on] / reach, lambda salt: _hash(i[on], j[on], int(inner), salt)


class Field:
    """Every square of the grid, in one set of arrays: each frame's light is a few table lookups."""

    def __init__(self, t, rnd) -> None:
        # near the edge densest and mostly lavender, further out sparser, violet and deep purple
        keep = rnd(0) < 0.97 * (1 - t) ** 0.7
        self.keep = np.flatnonzero(keep)
        t = t[keep]
        r1, r2, r3, r4, r5 = (rnd(n)[keep] for n in range(1, 6))
        pick = np.clip((r1 * 0.35 + t * 0.75) * len(PALETTE), 2, len(PALETTE) - 1).astype(np.intp)
        pick[r2 < 0.15 * (1 - t)] = 1
        self.pal = pick * LEVELS
        self.step = np.minimum(t * DSTEPS, DSTEPS - 1).astype(np.intp) * TSTEPS
        self.omega = TSTEPS * (0.25 + 0.9 * r3)  # twinkles a quarter to about once a second, like the app's
        self.phase = TSTEPS * r4
        self.reveal = 0.6 * t + 0.25 * r5  # when it lights up as the glow fades in: from the rim outward

    def colors(self, t: float, shown: float = 1.0) -> tuple:
        """Each square's colour this frame, and the rim's pulse (0..1). Every WAVE seconds a soft band of light
        leaves the rim and travels out through the grid, fading as it goes; each square twinkles gently on its own.
        While the glow fades in (shown 0..1) the squares light up from the rim outward; fading out, the outermost go
        first."""
        at = (t % WAVE) / (0.6 * WAVE) * 1.3 - 0.15  # the band's centre, as a fraction of the way out
        band = CREST * np.exp(-((DIST - at) / 0.13) ** 2) * (1 - min(max(at, 0.0), 1.0)) ** 0.7
        shine = SHINE[:, None] * TWINKLE + band[:, None] * (0.6 + 0.4 * TWINKLE)
        level = (np.minimum(shine, 1) * (LEVELS - 1) + 0.5).astype(np.intp).ravel()
        lit = level[self.step + ((self.omega * t + self.phase).astype(np.intp) & (TSTEPS - 1))]
        if shown < 1:
            lit = (lit * np.clip((shown - self.reveal) / 0.15, 0, 1)).astype(np.intp)
        return SQUARE_COL[self.pal + lit], math.exp(-(at / 0.12) ** 2)


class Sparkle:
    """The animated layer of one border strip: the thin bright rim hugging the window's (rounded) edge and the grid's
    squares, as fixed pixel indices. Strips overlap at their seams so a square there is drawn whole by one strip;
    the rim is drawn only over the strip's own part (own) so it isn't doubled."""

    def __init__(self, rect, own, w: int, h: int, r: int, s: float, inner: bool, x, y, k: int) -> None:
        x0, y0, x1, y1 = rect
        sw = x1 - x0
        ys, xs = np.mgrid[own[1]:own[3], own[0]:own[2]]
        d = (-1 if inner else 1) * _sdf(xs + 0.5, ys + 0.5, w, h, r)  # distances count inward for a glow drawn inside
        level = np.rint(np.clip(1 - np.abs(d - 0.5) / (1.0 + 0.8 * s), 0, 1) ** 1.6 * (RIM_LEVELS - 1)).astype(np.int32)
        on = (d > -1.0) & (level > 0)
        xs, ys = xs[on], ys[on]
        self.rim = (ys - y0) * sw + (xs - x0)
        self.rim_key = (np.minimum(_around(xs + 0.5, ys + 0.5, w, h) * RIM_BINS, RIM_BINS - 1).astype(np.int32) * RIM_LEVELS
                        + level[on])
        # this strip's squares: those whose centre is over its own part and that fit inside it
        mx, my = x + k / 2, y + k / 2
        mine = np.flatnonzero((mx >= own[0]) & (mx < own[2]) & (my >= own[1]) & (my < own[3])
                              & (x >= x0) & (y >= y0) & (x + k <= x1) & (y + k <= y1))
        oy, ox = np.divmod(np.arange(k * k), k)
        self.pix = (((y[mine] - y0) * sw + (x[mine] - x0))[:, None] + (oy * sw + ox)).ravel()
        self.blk = np.repeat(mine, k * k)

    def draw(self, flat, rim, colors) -> None:
        """rim: _rim_table; colors: Field.colors for every square."""
        flat[self.pix] = colors[self.blk]
        flat[self.rim] = rim[self.rim_key]


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
    def _sparks(w: int, h: int, r: int, width: int, clip, inner: bool, m: int) -> list:
        """The sparkle strips: _strips, each also reaching m px across its seams so a square on a seam fits whole in
        the strip its centre is over: [(rect, its own part) or None]."""
        out = []
        for i, own in enumerate(Glow._strips(w, h, r, width, None, inner)):
            if not own:
                out.append(None)
                continue
            a, b, c, d = own
            rc = (a, b, c, d + m) if i == 0 else (a, b - m, c, d) if i == 1 else (a, b - m, c, d + m)
            if clip:
                own, rc = ((max(q[0], clip[0]), max(q[1], clip[1]), min(q[2], clip[2]), min(q[3], clip[3])) for q in (own, rc))
            out.append((rc, own) if own[2] > own[0] and own[3] > own[1] else None)
        return out

    @staticmethod
    def _lut(glow: int, s: float):
        """Bloom colour and opacity by distance from the edge (quarter-pixel steps), premultiplied."""
        dist = np.arange(glow * 4 + 1, dtype=np.float32) / 4
        # three falloffs layered into a soft, calm bloom, eased out to nothing at the outer edge
        a = 0.3 * np.exp(-dist / (3.5 * s)) + 0.2 * np.exp(-dist / (12 * s)) + 0.14 * np.exp(-dist / (26 * s))
        q = np.clip((dist - 0.45 * glow) / (0.55 * glow), 0, 1)
        a *= 1 - q * q * (3 - 2 * q)
        f = np.clip(dist / (0.6 * glow), 0, 1)[:, None]
        lav, vio, deep = (np.array(c, np.float32) for c in (LAVENDER, VIOLET, DEEP))
        rgb = np.where(f < 0.3, lav + (vio - lav) * (f / 0.3), vio + (deep - vio) * ((f - 0.3) / 0.7) * 0.6)
        return _pack(rgb.astype(np.float32), a)

    def _layout(self, bloom, spark, w: int, h: int, r: int, s: float, reach: tuple, strips, inner: bool) -> tuple[list, Field]:
        """Draws the bloom strips and builds the pixel grid and the sparkle strips: [(rect, Sparkle or None) or None]
        per window, and the grid's Field."""
        lut = self._lut(reach[0], s)
        x, y, k, out, rnd = _grid(w, h, r, s, reach[1], inner)
        field = Field(out, rnd)
        x, y = x[field.keep], y[field.keep]
        made = []
        for i, (sf, rc) in enumerate(zip(bloom + spark, strips)):
            if not rc:
                sf.hide()
                made.append(None)
                continue
            if i < 4:
                px = sf.canvas(rc[2] - rc[0], rc[3] - rc[1])
                xs = np.arange(rc[0], rc[2], dtype=np.float32)[None, :] + 0.5
                ys = np.arange(rc[1], rc[3], dtype=np.float32)[:, None] + 0.5
                d = -_sdf(xs, ys, w, h, r) if inner else _sdf(xs, ys, w, h, r)
                px[:] = np.where(d >= 0, lut[np.clip((d * 4).astype(np.int32), 0, len(lut) - 1)], 0)
                made.append((rc, None))
            else:
                rc, own = rc
                sf.canvas(rc[2] - rc[0], rc[3] - rc[1])
                made.append((rc, Sparkle(rc, own, w, h, r, s, inner, x, y, k)))
        return made, field

    def _loop(self, bloom, spark) -> None:
        msg = wt.MSG()
        hwnd = goal = None
        rect = None  # the eased rect (floats) the glow is drawn around
        r, s, mon, inner = 0, 1.0, None, False
        fade = 0.0
        key = geo = None
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
            fresh = False
            if (w, h, r, s, clip, inner) != geo:
                geo = (w, h, r, s, clip, inner)
                reach = (round(GLOW * s * (INNER if inner else 1)), round(EXT * s * (INNER if inner else 1)))
                square = max(1, int(SQUARE * s + 0.5))
                strips = (self._strips(w, h, r, reach[0], clip, inner)
                          + self._sparks(w, h, r, reach[1] + 2 * square, clip, inner, square // 2 + 1))
                fresh = (w, h, r, s, strips) != key
            if fresh:
                key = (w, h, r, s, strips)
                made, field = self._layout(bloom, spark, w, h, r, s, reach, strips, inner)
            e = fade * fade * (3 - 2 * fade)  # smoothstep
            breath = round(85 * (0.8 + 0.2 * math.sin((now - t0) * 2 * math.pi / 4.8))) / 85  # steps too fine to see
            colors, pulse = field.colors(now - t0, e)
            # the rim lights up early and goes out late, and brightens as each band of light leaves it
            rim = _rim_table(now - t0, min(1.0, 2.5 * e) * (RIM_GLOW + (1 - RIM_GLOW) * pulse))
            for i, sf in enumerate(bloom + spark):
                if made[i]:
                    rc, sp = made[i]
                    if sp:
                        sp.draw(sf.flat, rim, colors)
                    sf.push(l + rc[0], t + rc[1], 1.0 if sp else e * breath, pixels=bool(sp) or fresh)
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
