"""
WebWallpaper - Use any website as your Windows desktop background
Requires: pip install PyQt6 PyQt6-WebEngine
Run: python WebWallpaper.py
"""

import os
import sys
import json
import ctypes
import ctypes.wintypes
import subprocess
from pathlib import Path

# ── GPU / Chromium flags — must be set before QApplication is imported ────────
# Force Chromium to use the NVIDIA GPU via the D3D11 / ANGLE path.
# These environment variables are read by QtWebEngine's Chromium at startup.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
    " ".join([
        # ── Safe D3D11 path — works reliably on GTX 1070 / Windows ──────────
        "--use-gl=angle",                      # ANGLE frontend (required on Windows)
        "--use-angle=d3d11",                   # D3D11 backend — stable on NVIDIA
        "--ignore-gpu-blocklist",              # Override Chromium's GPU deny-list
        "--enable-gpu-rasterization",          # Rasterise page content on GPU
        "--enable-accelerated-2d-canvas",      # Canvas2D via GPU
        "--enable-accelerated-video-decode",   # DXVA hardware video decode

        # ── Disable the features that caused the SharedImage crash ───────────
        # zero-copy and NativeGpuMemoryBuffers require kernel DMA support that
        # the ANGLE/D3D11 stack on desktop Windows does not expose → fatal.
        "--disable-zero-copy",
        "--disable-features=VaapiVideoDecoder,"
                            "Vulkan,"                 # Vulkan path unstable here
                            "UseSkiaRenderer,"        # Skia/Graphite needs Vulkan
                            "NativeGpuMemoryBuffers", # Root cause of crash

        # ── Anti-flicker flags ───────────────────────────────────────────────
        # OOP rasterization moves tile rasterization off the main thread,
        # eliminating blank frames between paint cycles.
        "--enable-oop-rasterization",
        # Ensure the compositor produces frames at a steady rate rather than
        # dropping to 0 fps when the window is occluded (wallpaper layer quirk).
        "--disable-backgrounding-occluded-windows",
        # Keep the renderer alive; backgrounded renderers throttle their frame
        # rate aggressively which causes visible stutter on the desktop layer.
        "--disable-renderer-backgrounding",
        # Don't throttle timers/animations when the page isn't "visible"
        # (embedded WorkerW windows look invisible to Chromium's visibility logic).
        "--disable-background-timer-throttling",
    ])
)

# Tell Qt to use the desktop OpenGL stack (not ANGLE for Qt itself, only for
# Chromium's internal ANGLE). This keeps Qt widgets rendering on the GPU too.
os.environ.setdefault("QT_OPENGL", "desktop")

# Prefer the NVIDIA adapter on Optimus / multi-GPU systems
os.environ.setdefault("SHIM_MCCOMPAT", "0x800000001")   # NVIDIA hint

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem,
    QSystemTrayIcon, QMenu, QDialog, QMessageBox, QFrame, QSplitter,
    QCheckBox, QSpinBox, QGroupBox
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage, QWebEngineProfile
from PyQt6.QtCore import Qt, QUrl, QTimer, QSize, pyqtSignal, QThread
from PyQt6.QtGui import QIcon, QFont, QColor, QPalette, QPixmap, QAction, QDesktopServices

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".webwallpaper" / "config.json"
DEFAULT_CONFIG = {
    "url": "https://earth.nullschool.net",
    "favorites": [
        {"name": "Earth Wind Map",      "url": "https://earth.nullschool.net"},
        {"name": "Google Earth",        "url": "https://earth.google.com/web/"},
        {"name": "Windy",               "url": "https://www.windy.com"},
        {"name": "NASA APOD",           "url": "https://apod.nasa.gov/apod/"},
        {"name": "Fluid Simulation",    "url": "https://paveldogreat.github.io/WebGL-Fluid-Simulation/"},
        {"name": "Hacker News",         "url": "https://news.ycombinator.com"},
    ],
    "refresh_interval": 0,   # 0 = no auto-refresh, else minutes
    "mute_audio": True,
    "zoom": 1.0,
    "start_with_windows": False,
}

def load_config():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            # Fill missing keys with defaults
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

# ── Windows API helpers ───────────────────────────────────────────────────────

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

_workerw_cache: int | None = None

def find_workerw():
    """Return the WorkerW HWND that sits behind desktop icons, or None.

    The 0x052C message is sent to Progman only once and the result is cached.
    Re-sending it on every call forces WorkerW to respawn, which flushes the
    compositor and causes a visible flash on every launch/reload.
    """
    global _workerw_cache
    if _workerw_cache:
        # Verify the cached handle is still valid before returning it.
        if user32.IsWindow(_workerw_cache):
            return _workerw_cache
        _workerw_cache = None

    # Send 0x052C to Progman to spawn WorkerW (once only)
    progman = user32.FindWindowW("Progman", None)
    user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000, None)

    workerw = ctypes.c_ulonglong(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_cb(hwnd, lparam):
        shell = user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
        if shell:
            workerw.value = user32.FindWindowExW(None, hwnd, "WorkerW", None)
        return True

    user32.EnumWindows(enum_cb, 0)
    _workerw_cache = workerw.value or None
    return _workerw_cache

def get_screen_rect():
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return w or 1920, h or 1080

# ── Shared view settings ─────────────────────────────────────────────────────

def _apply_view_settings(view, mute: bool = False):
    """Apply GPU + feature settings to any QWebEngineView."""
    s = view.settings()
    s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
    s.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
    s.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
    s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
    s.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
    s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
    if mute:
        view.page().setAudioMuted(True)


# ── Popup browser window ──────────────────────────────────────────────────────

def _open_popup(request):
    """Open new-window requests in the system default browser."""
    url = request.requestedUrl()
    if url.isEmpty():
        # Some sites fire window.open() before setting the URL; ignore these
        return
    QDesktopServices.openUrl(url)


# ── Wallpaper page ────────────────────────────────────────────────────────────

class WallpaperPage(QWebEnginePage):
    """Custom page that routes new-window requests to the system browser."""
    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self.newWindowRequested.connect(_open_popup)


# ── Wallpaper window ──────────────────────────────────────────────────────────

class WallpaperWindow(QWidget):
    """Plain QWidget — NOT QMainWindow.

    QMainWindow wraps its central widget inside an internal QStackedWidget
    container. That extra layer is painted by Qt independently of the
    QWebEngineView's Chromium compositor, which causes a compositor-desync
    flicker every time either layer repaints. Using a flat QWidget with a
    QVBoxLayout eliminates the intermediate surface entirely.
    """
    def __init__(self, url: str, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._auto_refresh)

        w, h = get_screen_rect()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnBottomHint
        )
        # Don't steal focus on show — avoids the brief z-order shuffle that
        # appears as a flash on the desktop layer.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # Don't let Qt paint a background before web content is ready.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # OpaquePaintEvent: tell Qt this widget draws every pixel itself so it
        # never issues an erase-background (WM_ERASEBKGND) call — that call is
        # the primary cause of the black/white flash seen between repaints.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setGeometry(0, 0, w, h)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.view = QWebEngineView(self)

        # Set the Chromium compositor background to black so the gap between
        # window creation and first page paint is black, not white.
        # setStyleSheet() only styles the Qt widget frame; setBackgroundColor()
        # controls what Chromium's compositor paints before the page is ready.
        self.view.page().setBackgroundColor(QColor(0, 0, 0))

        page = WallpaperPage(QWebEngineProfile.defaultProfile(), self.view)
        self.view.setPage(page)
        # Re-apply black background on the new page object too.
        page.setBackgroundColor(QColor(0, 0, 0))

        _apply_view_settings(self.view, mute=cfg.get("mute_audio", True))
        self.view.setZoomFactor(cfg.get("zoom", 1.0))
        layout.addWidget(self.view)
        self.load_url(url)
        self._apply_refresh(cfg.get("refresh_interval", 0))

    def load_url(self, url: str):
        if not url.startswith("http"):
            url = "https://" + url
        self.view.load(QUrl(url))

    def _auto_refresh(self):
        # Use load() instead of reload() for auto-refresh.  reload() discards
        # the current surface immediately and repaints with a white frame while
        # the new page loads.  load() keeps the old surface visible until the
        # new page's first paint, eliminating the flash.
        self.view.load(self.view.url())

    def _apply_refresh(self, minutes: int):
        self._refresh_timer.stop()
        if minutes > 0:
            self._refresh_timer.start(minutes * 60 * 1000)

    def embed(self):
        # Defer SetParent by one event-loop cycle.  Qt may re-create the native
        # HWND during show() processing; reading winId() synchronously can
        # return a stale handle that Qt immediately destroys.  singleShot(0)
        # ensures we read the final stable HWND after all pending Qt events.
        QTimer.singleShot(0, self._do_embed)

    def _do_embed(self):
        hwnd = int(self.winId())
        workerw = find_workerw()
        if not workerw:
            print("WebWallpaper: WorkerW not found — running as floating window")
            return
        user32.SetParent(hwnd, workerw)
        # Re-assert geometry after re-parenting; the child coordinate origin
        # shifts to the WorkerW client area origin after SetParent.
        w, h = get_screen_rect()
        SWP_SHOWWINDOW = 0x0040
        user32.SetWindowPos(hwnd, 0, 0, 0, w, h, SWP_SHOWWINDOW)

# ── Tray icon ─────────────────────────────────────────────────────────────────

TRAY_ICON_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="6" fill="#1e1e2e"/>
  <circle cx="16" cy="16" r="9" fill="none" stroke="#89b4fa" stroke-width="2"/>
  <ellipse cx="16" cy="16" rx="4" ry="9" fill="none" stroke="#89b4fa" stroke-width="1.5"/>
  <line x1="7" y1="16" x2="25" y2="16" stroke="#89b4fa" stroke-width="1.5"/>
</svg>"""

def make_tray_icon():
    px = QPixmap(32, 32)
    px.fill(QColor("#1e1e2e"))
    return QIcon(px)

# ── Control panel ─────────────────────────────────────────────────────────────

DARK = "#1e1e2e"
SURFACE = "#313244"
ACCENT = "#89b4fa"
TEXT = "#cdd6f4"
SUBTEXT = "#a6adc8"
RED = "#f38ba8"
GREEN = "#a6e3a1"

STYLE = f"""
QWidget {{ background: {DARK}; color: {TEXT}; font-family: 'Segoe UI', sans-serif; font-size: 13px; }}
QLineEdit {{
    background: {SURFACE}; border: 1px solid #45475a; border-radius: 6px;
    padding: 6px 10px; color: {TEXT};
}}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QPushButton {{
    background: {SURFACE}; border: 1px solid #45475a; border-radius: 6px;
    padding: 6px 14px; color: {TEXT};
}}
QPushButton:hover {{ background: #45475a; }}
QPushButton#apply  {{ background: {ACCENT}; color: {DARK}; font-weight: bold; border: none; }}
QPushButton#apply:hover {{ background: #b4d0fb; }}
QPushButton#danger {{ background: {RED};   color: {DARK}; font-weight: bold; border: none; }}
QPushButton#danger:hover {{ background: #f5a8bc; }}
QPushButton#success{{ background: {GREEN}; color: {DARK}; font-weight: bold; border: none; }}
QListWidget {{
    background: {SURFACE}; border: 1px solid #45475a; border-radius: 6px;
    outline: none;
}}
QListWidget::item {{ padding: 8px 10px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {ACCENT}; color: {DARK}; }}
QListWidget::item:hover   {{ background: #45475a; }}
QLabel#heading {{ font-size: 18px; font-weight: bold; color: {TEXT}; }}
QLabel#sub     {{ font-size: 11px; color: {SUBTEXT}; }}
QLabel#status  {{ font-size: 11px; color: {SUBTEXT}; }}
QGroupBox {{
    border: 1px solid #45475a; border-radius: 8px;
    margin-top: 10px; padding: 10px;
    font-size: 11px; color: {SUBTEXT};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid #45475a; background: {SURFACE}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QSpinBox {{ background: {SURFACE}; border: 1px solid #45475a; border-radius: 6px;
    padding: 4px 8px; color: {TEXT}; }}
QScrollBar:vertical {{ background: {SURFACE}; width: 6px; border-radius: 3px; }}
QScrollBar::handle:vertical {{ background: #45475a; border-radius: 3px; }}
"""

class ControlPanel(QDialog):
    wallpaper_changed = pyqtSignal(str)
    settings_changed  = pyqtSignal(dict)

    def __init__(self, cfg: dict, wallpaper_active: bool, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("WebWallpaper")
        self.setMinimumSize(560, 520)
        self.setStyleSheet(STYLE)
        self._build_ui(wallpaper_active)

    def _build_ui(self, wallpaper_active):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)

        # ── Header
        hdr = QHBoxLayout()
        title = QLabel("🌐  WebWallpaper")
        title.setObjectName("heading")
        hdr.addWidget(title)
        hdr.addStretch()
        ver = QLabel("v1.0")
        ver.setObjectName("sub")
        hdr.addWidget(ver)
        root.addLayout(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #45475a;")
        root.addWidget(sep)

        # ── URL bar
        url_row = QHBoxLayout()
        self.url_input = QLineEdit(self.cfg["url"])
        self.url_input.setPlaceholderText("Enter URL  e.g. https://earth.nullschool.net")
        self.url_input.returnPressed.connect(self._apply_url)
        url_row.addWidget(self.url_input)
        apply_btn = QPushButton("Set Wallpaper")
        apply_btn.setObjectName("apply")
        apply_btn.clicked.connect(self._apply_url)
        url_row.addWidget(apply_btn)
        root.addLayout(url_row)

        # ── Favourites + preview split
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Favourites list
        fav_frame = QWidget()
        fav_layout = QVBoxLayout(fav_frame)
        fav_layout.setContentsMargins(0, 0, 0, 0)
        fav_layout.addWidget(QLabel("Favourites"))
        self.fav_list = QListWidget()
        self._populate_favs()
        self.fav_list.itemDoubleClicked.connect(self._load_fav)
        fav_layout.addWidget(self.fav_list)

        fav_btns = QHBoxLayout()
        add_btn = QPushButton("＋ Add current")
        add_btn.clicked.connect(self._add_fav)
        del_btn = QPushButton("✕ Remove")
        del_btn.setObjectName("danger")
        del_btn.clicked.connect(self._del_fav)
        fav_btns.addWidget(add_btn)
        fav_btns.addWidget(del_btn)
        fav_layout.addLayout(fav_btns)
        splitter.addWidget(fav_frame)

        # Settings panel
        settings_frame = QWidget()
        sf_layout = QVBoxLayout(settings_frame)
        sf_layout.setContentsMargins(0, 0, 0, 0)
        sf_layout.addWidget(QLabel("Settings"))

        grp = QGroupBox("Playback")
        grp_layout = QVBoxLayout(grp)

        self.mute_cb = QCheckBox("Mute audio")
        self.mute_cb.setChecked(self.cfg.get("mute_audio", True))
        grp_layout.addWidget(self.mute_cb)

        refresh_row = QHBoxLayout()
        refresh_row.addWidget(QLabel("Auto-refresh every"))
        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(0, 1440)
        self.refresh_spin.setValue(self.cfg.get("refresh_interval", 0))
        self.refresh_spin.setSuffix(" min  (0 = off)")
        refresh_row.addWidget(self.refresh_spin)
        refresh_row.addStretch()
        grp_layout.addLayout(refresh_row)

        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Zoom %"))
        self.zoom_spin = QSpinBox()
        self.zoom_spin.setRange(25, 400)
        self.zoom_spin.setValue(int(self.cfg.get("zoom", 1.0) * 100))
        self.zoom_spin.setSuffix(" %")
        zoom_row.addWidget(self.zoom_spin)
        zoom_row.addStretch()
        grp_layout.addLayout(zoom_row)

        sf_layout.addWidget(grp)

        grp2 = QGroupBox("System")
        grp2_layout = QVBoxLayout(grp2)
        self.startup_cb = QCheckBox("Start with Windows")
        self.startup_cb.setChecked(self.cfg.get("start_with_windows", False))
        self.startup_cb.stateChanged.connect(self._toggle_startup)
        grp2_layout.addWidget(self.startup_cb)
        sf_layout.addWidget(grp2)
        sf_layout.addStretch()

        save_btn = QPushButton("Save Settings")
        save_btn.setObjectName("success")
        save_btn.clicked.connect(self._save_settings)
        sf_layout.addWidget(save_btn)
        splitter.addWidget(settings_frame)

        splitter.setSizes([240, 280])
        root.addWidget(splitter)

        # ── Status bar
        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color: #45475a;")
        root.addWidget(sep2)

        status_row = QHBoxLayout()
        dot = "🟢" if wallpaper_active else "🔴"
        status_txt = "Wallpaper active" if wallpaper_active else "Wallpaper not embedded (run as admin for full embed)"
        self.status_lbl = QLabel(f"{dot} {status_txt}")
        self.status_lbl.setObjectName("status")
        status_row.addWidget(self.status_lbl)
        status_row.addStretch()

        gpu_btn = QPushButton("🖥 GPU info")
        gpu_btn.setToolTip("Open chrome://gpu in a new window to verify hardware acceleration")
        gpu_btn.clicked.connect(self._show_gpu_info)
        status_row.addWidget(gpu_btn)

        hide_btn = QPushButton("Hide to tray")
        hide_btn.clicked.connect(self.hide)
        status_row.addWidget(hide_btn)
        root.addLayout(status_row)

    def _populate_favs(self):
        self.fav_list.clear()
        for fav in self.cfg["favorites"]:
            item = QListWidgetItem(fav["name"])
            item.setToolTip(fav["url"])
            item.setData(Qt.ItemDataRole.UserRole, fav["url"])
            self.fav_list.addItem(item)

    def _load_fav(self, item):
        url = item.data(Qt.ItemDataRole.UserRole)
        self.url_input.setText(url)
        self._apply_url()

    def _add_fav(self):
        url = self.url_input.text().strip()
        if not url:
            return
        name, ok = _simple_input(self, "Add Favourite", "Name for this page:", url.split("//")[-1][:40])
        if ok and name:
            self.cfg["favorites"].append({"name": name, "url": url})
            self._populate_favs()
            save_config(self.cfg)

    def _del_fav(self):
        row = self.fav_list.currentRow()
        if row >= 0:
            self.cfg["favorites"].pop(row)
            self._populate_favs()
            save_config(self.cfg)

    def _apply_url(self):
        url = self.url_input.text().strip()
        if url:
            self.cfg["url"] = url
            save_config(self.cfg)
            self.wallpaper_changed.emit(url)

    def _save_settings(self):
        self.cfg["mute_audio"]        = self.mute_cb.isChecked()
        self.cfg["refresh_interval"]  = self.refresh_spin.value()
        self.cfg["zoom"]              = self.zoom_spin.value() / 100.0
        save_config(self.cfg)
        self.settings_changed.emit(self.cfg)
        QMessageBox.information(self, "Saved", "Settings saved. Some changes take effect on next launch.")

    def _show_gpu_info(self):
        """Open chrome://gpu in a small diagnostic window."""
        dlg = QDialog(self)
        dlg.setWindowTitle("GPU / Hardware Acceleration Status")
        dlg.setMinimumSize(900, 640)
        dlg.setStyleSheet(STYLE)
        lay = QVBoxLayout(dlg)
        lbl = QLabel(
            "✅ If 'Graphics Feature Status' shows <b>Hardware accelerated</b> for "
            "Canvas, WebGL, and Compositing — your GTX 1070 is active.\n"
            "🔴 Any entry showing <b>Software only</b> means that feature is not GPU-accelerated."
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{TEXT}; font-size:12px; padding:4px;")
        lay.addWidget(lbl)
        gpu_view = QWebEngineView()
        gpu_view.load(QUrl("chrome://gpu"))
        lay.addWidget(gpu_view)
        nvidia_info = _gpu_info()
        lay.addWidget(QLabel(f"nvidia-smi: {nvidia_info}"))
        dlg.exec()

    def _toggle_startup(self, state):
        _set_startup(bool(state))
        self.cfg["start_with_windows"] = bool(state)
        save_config(self.cfg)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _simple_input(parent, title, label, default=""):
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setStyleSheet(STYLE)
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(label))
    inp = QLineEdit(default)
    lay.addWidget(inp)
    btns = QHBoxLayout()
    ok_btn = QPushButton("OK");     ok_btn.setObjectName("apply")
    ca_btn = QPushButton("Cancel"); 
    ok_btn.clicked.connect(dlg.accept)
    ca_btn.clicked.connect(dlg.reject)
    btns.addWidget(ok_btn); btns.addWidget(ca_btn)
    lay.addLayout(btns)
    result = dlg.exec()
    return inp.text(), result == QDialog.DialogCode.Accepted

def _set_startup(enable: bool):
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    exe = sys.executable
    script = Path(__file__).resolve()
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, "WebWallpaper", 0, winreg.REG_SZ, f'"{exe}" "{script}"')
        else:
            try:
                winreg.DeleteValue(key, "WebWallpaper")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Startup registry error: {e}")

# ── Main app ──────────────────────────────────────────────────────────────────

class App(QApplication):
    def __init__(self, argv):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)
        self.cfg = load_config()

        # Wallpaper window
        self.wallpaper = WallpaperWindow(self.cfg["url"], self.cfg)
        self.wallpaper.show()
        # embed() schedules SetParent via QTimer.singleShot(0) so it runs
        # after show() has fully committed the final native HWND.
        self.wallpaper.embed()
        embedded = True  # updated asynchronously; assume success for panel UI

        # Tray
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        menu = QMenu()
        show_act   = QAction("⚙  Control Panel", self)
        reload_act = QAction("↺  Reload Wallpaper", self)
        quit_act   = QAction("✕  Quit", self)
        show_act.triggered.connect(self._show_panel)
        reload_act.triggered.connect(lambda: self.wallpaper.view.load(self.wallpaper.view.url()))
        quit_act.triggered.connect(self.quit)
        menu.addAction(show_act)
        menu.addAction(reload_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.setToolTip("WebWallpaper")
        self.tray.show()

        # Control panel
        self.panel = ControlPanel(self.cfg, embedded)
        self.panel.wallpaper_changed.connect(self.wallpaper.load_url)
        self.panel.settings_changed.connect(self._on_settings)
        self.panel.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_panel()

    def _show_panel(self):
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    def _on_settings(self, cfg):
        self.wallpaper.view.page().setAudioMuted(cfg.get("mute_audio", True))
        self.wallpaper.view.setZoomFactor(cfg.get("zoom", 1.0))
        self.wallpaper._apply_refresh(cfg.get("refresh_interval", 0))


def _gpu_info() -> str:
    """Return a short string describing GPU acceleration status."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,driver_version,utilization.gpu",
             "--format=csv,noheader,nounits"],
            timeout=4, stderr=subprocess.DEVNULL
        ).decode().strip()
        return f"NVIDIA  {out}"
    except Exception:
        return "nvidia-smi not found — check NVIDIA drivers"


def main():
    # ── Qt application attributes (must be set before QApplication()) ──────────
    # Use the real desktop OpenGL stack for Qt widgets — keeps the GPU warm.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseDesktopOpenGL)
    # Shared OpenGL context is required by QtWebEngine.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    app = App(sys.argv)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
