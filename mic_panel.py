"""
Claude Code Voice Mode Microphone Control Panel
Always-on-top floating window with:
- Push to Talk (hold button)
- Toggle to Talk (click to start/stop)
- Mic volume slider
- Slide-out Settings and Console panels
- Live service console capture (Whisper, AllTalk)
- Launcher for Claude Code terminals
- Minimize to system tray
"""
import ctypes
import ctypes.wintypes
import io
import json
import logging
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import wave
import webbrowser
from tkinter import ttk, messagebox
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
from tkwinterm.winterminal import Terminal

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WHISPER_URL = "http://127.0.0.1:8787"
SAMPLE_RATE = 16000
CHANNELS = 1
STATE_FILE = Path("F:/Apps/freedom_system/REPO_claude_code_voice_mode/mic_state.json")
PREFS_FILE = Path("F:/Apps/freedom_system/REPO_claude_code_voice_mode/mic_prefs.json")

# VAD Configuration
VAD_AGGRESSIVENESS = 2         # 0 (least aggressive) to 3 (most aggressive)
VAD_SILENCE_TIMEOUT = 5.0      # seconds of silence after speech to trigger send
VAD_SPEECH_ONSET_FRAMES = 3    # consecutive 30ms speech frames to confirm speech start
VAD_PRE_BUFFER_MS = 300        # milliseconds of pre-roll audio to keep
VAD_MIN_RECORDING_S = 0.5      # minimum recording length to process
VAD_FRAME_SAMPLES = 480        # 30ms at 16kHz — required by webrtcvad
VAD_FRAME_BYTES = VAD_FRAME_SAMPLES * 2  # 960 bytes (int16)
LOG_FILE = Path("F:/Apps/freedom_system/log/claude_code_voice_mode_mic_panel.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Service management
WHISPER_PORT = 8787
ALLTALK_PORT = 7851
WHISPER_HEALTH_PATH = "/health"
ALLTALK_HEALTH_PATH = "/api/ready"
WHISPER_CWD = r"F:\Apps\freedom_system\app_cabinet\whisper_stt"
ALLTALK_CWD = r"F:\Apps\freedom_system\app_cabinet\alltalk_tts"
REPOS_DIR = r"F:\Apps\freedom_system"

# Panel dimensions
PANEL_WIDTH_COLLAPSED = 280
PANEL_WIDTH_EXPANDED = 1280
PANEL_HEIGHT = 860

LOG_FORMAT = "[MIC_PANEL] [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


class TextHandler(logging.Handler):
    """Logging handler that writes to a tkinter Text widget (thread-safe)."""

    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record) + "\n"
        try:
            self.text_widget.after(0, self._append, msg)
        except RuntimeError:
            pass  # main thread not in main loop yet

    def _append(self, msg):
        self.text_widget.insert(tk.END, msg)
        self.text_widget.see(tk.END)


# ---------------------------------------------------------------------------
# Shared mic state (read by MCP server)
# ---------------------------------------------------------------------------
def save_mic_state(state: dict):
    """Save mic state to file for MCP server to read."""
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save mic state: {e}")


def load_prefs() -> dict:
    """Load persistent preferences from file."""
    try:
        if PREFS_FILE.exists():
            return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to load prefs: {e}")
    return {}


def save_prefs(prefs: dict):
    """Save persistent preferences to file."""
    try:
        PREFS_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save prefs: {e}")


def create_tray_icon_image(color="green"):
    """Create a small colored circle icon for system tray."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {
        "green": (0, 200, 0, 255),
        "red": (200, 0, 0, 255),
        "yellow": (200, 200, 0, 255),
        "gray": (128, 128, 128, 255),
    }
    fill = colors.get(color, colors["gray"])
    draw.ellipse([4, 4, 60, 60], fill=fill, outline=(255, 255, 255, 255), width=2)
    return img


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
_URL_RE = re.compile(r'https?://\S+')


def _make_readonly(text_widget):
    """Block typing but allow navigation, selection, and copy."""
    def _on_key(event):
        # Allow Ctrl+C (copy), Ctrl+A (select all), navigation keys
        if event.state & 0x4 and event.keysym in ("c", "C", "a", "A"):
            return
        if event.keysym in ("Up", "Down", "Left", "Right", "Home", "End",
                            "Prior", "Next", "Shift_L", "Shift_R",
                            "Control_L", "Control_R"):
            return
        return "break"
    text_widget.bind("<Key>", _on_key)


def _setup_link_tags(text_widget):
    """Configure a Text widget to support clickable URL links.

    Disabled Text widgets block tag_bind events, so we bind at
    the widget level and check for the 'link' tag in the handler.
    """
    text_widget.tag_configure("link", foreground="#58a6ff", underline=True)
    text_widget.bind("<Motion>", lambda e: _on_link_motion(text_widget, e))
    text_widget.bind("<Button-1>", lambda e: _on_link_click(text_widget, e))


def _on_link_motion(widget, event):
    """Change cursor to hand when hovering over a link tag."""
    idx = widget.index(f"@{event.x},{event.y}")
    if "link" in widget.tag_names(idx):
        widget.config(cursor="hand2")
    else:
        widget.config(cursor="xterm")


def _on_link_click(widget, event):
    """Open the URL under the mouse cursor in the default browser."""
    idx = widget.index(f"@{event.x},{event.y}")
    if "link" not in widget.tag_names(idx):
        return
    tag_range = widget.tag_prevrange("link", f"{idx}+1c")
    if tag_range:
        url = widget.get(*tag_range)
        webbrowser.open(url)


def _append_to_text_widget(widget, text):
    """Thread-safe append to a tk.Text widget via .after()."""
    def _do():
        try:
            start_idx = widget.index(tk.END)
            text_clean = _ANSI_RE.sub('', text)
            widget.insert(tk.END, text_clean)
            # Tag any URLs in the just-inserted text
            for m in _URL_RE.finditer(text_clean):
                line_start = widget.index(f"{start_idx}+{m.start()}c")
                line_end = widget.index(f"{start_idx}+{m.end()}c")
                widget.tag_add("link", line_start, line_end)
            widget.see(tk.END)
        except Exception:
            logger.exception("_append_to_text_widget error")
    widget.after(0, _do)


class MicControlPanel:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Claude Code Voice Mode Mic")
        self.root.geometry(f"{PANEL_WIDTH_COLLAPSED}x{PANEL_HEIGHT}")
        self.root.resizable(False, True)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # State
        self.mode = tk.StringVar(value="push_to_talk")
        self.is_recording = False
        self.is_muted = True
        self.volume = tk.IntVar(value=50)
        self.audio_queue = queue.Queue()
        self.audio_stream = None
        self.level_value = 0.0
        self.tray_icon = None
        self.hidden = False
        self.tts_paused = False
        self._level_monitor_stop = threading.Event()
        self._input_devices = self._query_input_devices()
        # Restore last input device from prefs, fall back to Windows Default
        prefs = load_prefs()
        saved_device = prefs.get("input_device", "Windows Default")
        available_names = ["Windows Default"] + [d["name"] for d in self._input_devices]
        if saved_device in available_names:
            self.selected_device = tk.StringVar(value=saved_device)
            logger.info(f"Restored input device from prefs: {saved_device}")
        else:
            self.selected_device = tk.StringVar(value="Windows Default")
            logger.info(f"Saved device '{saved_device}' not available, using Windows Default")
        self._recording_frames = []
        self._recording_lock = threading.Lock()
        self._processing = False
        self._discovered_terminals = []       # list of (name, pid) tuples
        self._selected_terminal = tk.StringVar(value="")

        # VAD state (for toggle-to-talk with voice activity detection)
        self._vad = None              # webrtcvad.Vad instance, created lazily
        self._vad_state = "IDLE"      # IDLE, LISTENING, RECORDING, TRAILING, PROCESSING
        self._vad_buffer = bytearray()  # sub-frame alignment buffer for 30ms chunks
        self._vad_silence_count = 0   # consecutive non-speech 30ms frames
        self._vad_speech_count = 0    # consecutive speech frames (for onset detection)
        self._vad_pre_buffer = []     # rolling buffer of last ~10 frames (300ms)
        self._vad_recording_frames = []  # accumulated 30ms byte chunks during speech

        # Slide-out panel state
        self._active_panel = None  # None, "settings", or "console"
        self._pre_expand_x = None  # saved x position before expand (for correct collapse)

        # Service subprocess handles
        self._whisper_proc = None
        self._alltalk_proc = None

        self._build_ui()
        self._setup_console_logging()
        self._update_state()
        self._start_level_monitor()
        self._refresh_terminals()
        # Retry terminal discovery after a delay in case Claude Code isn't ready yet
        if not self._selected_terminal.get():
            self.root.after(3000, self._refresh_terminals)
            self.root.after(8000, self._refresh_terminals)

        # Auto-start services after UI is ready (non-blocking)
        self.root.after(500, self._auto_start_services)

        # Start with console panel open so service output is visible
        self.root.after(100, lambda: self._toggle_panel("console"))

    def _query_input_devices(self):
        """Query available input devices, filtered to the default host API (MME on Windows)."""
        devices = sd.query_devices()
        default_hostapi = sd.query_hostapis(0)  # MME is typically index 0
        default_hostapi_idx = 0

        input_devices = []
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0 and d["hostapi"] == default_hostapi_idx:
                # Skip the "Microsoft Sound Mapper" which IS the Windows default
                if "sound mapper" in d["name"].lower():
                    continue
                input_devices.append({"index": i, "name": d["name"]})
        return input_devices

    def _get_selected_device_index(self):
        """Resolve the selected device name to a sounddevice index, or None for default."""
        name = self.selected_device.get()
        if name == "Windows Default":
            return None
        for d in self._input_devices:
            if d["name"] == name:
                return d["index"]
        return None

    # -----------------------------------------------------------------------
    # UI Building
    # -----------------------------------------------------------------------
    def _build_ui(self):
        """Build the tkinter UI with slide-out tab system.
        Layout: slide-out panel expands LEFT, mic controls stay on the RIGHT."""
        # Main horizontal container
        self._main_container = tk.Frame(self.root)
        self._main_container.pack(fill=tk.BOTH, expand=True)

        # Left side: slide-out panel (hidden by default, expands LEFT)
        self._slideout_frame = tk.Frame(self._main_container)
        # Not packed initially — shown when a tab is clicked

        # Right side: mic panel controls (always visible)
        self._controls_frame = tk.Frame(self._main_container, width=PANEL_WIDTH_COLLAPSED)
        self._controls_frame.pack(side=tk.RIGHT, fill=tk.Y)
        self._controls_frame.pack_propagate(False)

        # Build the tab bar at top of controls frame
        self._build_tab_bar(self._controls_frame)

        # Build mic controls in controls frame
        self._build_mic_controls(self._controls_frame)

        # Build slide-out panel contents (created but not shown)
        self._settings_frame = tk.Frame(self._slideout_frame)
        self._console_frame = tk.Frame(self._slideout_frame)
        self._build_settings_panel()
        self._build_console_panel()

    def _build_tab_bar(self, parent):
        """Build custom tab button row."""
        tab_frame = tk.Frame(parent, bg="#1a1a2e")
        tab_frame.pack(fill=tk.X)

        btn_style = {
            "font": ("Segoe UI", 8, "bold"),
            "relief": tk.FLAT, "bd": 0, "padx": 6, "pady": 3,
            "cursor": "hand2",
        }

        self._tab_mic = tk.Button(
            tab_frame, text="Mic", bg="#16213e", fg="#e0e0e0",
            activebackground="#0f3460", activeforeground="white",
            command=lambda: self._toggle_panel(None),
            **btn_style
        )
        self._tab_mic.pack(side=tk.LEFT, padx=(2, 1), pady=2)

        self._tab_settings = tk.Button(
            tab_frame, text="Settings", bg="#1a1a2e", fg="#888888",
            activebackground="#0f3460", activeforeground="white",
            command=lambda: self._toggle_panel("settings"),
            **btn_style
        )
        self._tab_settings.pack(side=tk.LEFT, padx=1, pady=2)

        self._tab_console = tk.Button(
            tab_frame, text="Console", bg="#1a1a2e", fg="#888888",
            activebackground="#0f3460", activeforeground="white",
            command=lambda: self._toggle_panel("console"),
            **btn_style
        )
        self._tab_console.pack(side=tk.LEFT, padx=1, pady=2)

    def _build_mic_controls(self, parent):
        """Build all mic panel controls in the given parent frame."""
        # Title ribbon
        title_frame = tk.Frame(parent, bg="#2b2b2b")
        title_frame.pack(fill=tk.X, padx=0, pady=0)
        tk.Label(
            title_frame, text="Claude Code Voice Mode", font=("Segoe UI", 12, "bold"),
            bg="#2b2b2b", fg="white", pady=8
        ).pack()

        # Target Terminal selector
        terminal_frame = tk.LabelFrame(
            parent, text="Target Terminal", font=("Segoe UI", 9), padx=10, pady=5
        )
        terminal_frame.pack(fill=tk.X, padx=10, pady=(5, 0))

        terminal_inner = tk.Frame(terminal_frame)
        terminal_inner.pack(fill=tk.X)

        self.terminal_combo = ttk.Combobox(
            terminal_inner, textvariable=self._selected_terminal,
            values=[], state="readonly", font=("Segoe UI", 8)
        )
        self.terminal_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.terminal_refresh_btn = tk.Button(
            terminal_inner, text="\u21bb", font=("Segoe UI", 10),
            width=3, command=self._refresh_terminals
        )
        self.terminal_refresh_btn.pack(side=tk.RIGHT, padx=(5, 0))

        # Input device selector
        device_frame = tk.LabelFrame(
            parent, text="Input Device", font=("Segoe UI", 9), padx=10, pady=5
        )
        device_frame.pack(fill=tk.X, padx=10, pady=(5, 0))

        device_inner = tk.Frame(device_frame)
        device_inner.pack(fill=tk.X)

        device_names = ["Windows Default"] + [d["name"] for d in self._input_devices]
        self.device_combo = ttk.Combobox(
            device_inner, textvariable=self.selected_device,
            values=device_names, state="readonly", font=("Segoe UI", 8)
        )
        self.device_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_change)

        self.device_refresh_btn = tk.Button(
            device_inner, text="\u21bb", font=("Segoe UI", 10),
            width=3, command=self._refresh_devices
        )
        self.device_refresh_btn.pack(side=tk.RIGHT, padx=(5, 0))

        # Status indicator
        self.status_frame = tk.Frame(parent, bg="#1e1e1e")
        self.status_frame.pack(fill=tk.X, padx=10, pady=(10, 5))
        self.status_label = tk.Label(
            self.status_frame, text="Muted", font=("Segoe UI", 10),
            bg="#1e1e1e", fg="#ff8800", pady=4
        )
        self.status_label.pack()

        # Audio level meter
        level_frame = tk.Frame(parent)
        level_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(level_frame, text="Level:", font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.level_bar = ttk.Progressbar(level_frame, length=200, mode="determinate", maximum=100)
        self.level_bar.pack(side=tk.LEFT, padx=(5, 0), fill=tk.X, expand=True)

        # Mode selection
        mode_frame = tk.LabelFrame(parent, text="Mode", font=("Segoe UI", 9), padx=10, pady=5)
        mode_frame.pack(fill=tk.X, padx=10, pady=5)

        tk.Radiobutton(
            mode_frame, text="Push to Talk", variable=self.mode, value="push_to_talk",
            font=("Segoe UI", 9), command=self._on_mode_change
        ).pack(anchor=tk.W)

        self._rb_toggle = tk.Radiobutton(
            mode_frame, text="Toggle to Talk", variable=self.mode, value="toggle",
            font=("Segoe UI", 9), command=self._on_mode_change
        )
        self._rb_toggle.pack(anchor=tk.W)

        self._update_mode_labels()

        # Main action button
        btn_frame = tk.Frame(parent)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        self.action_btn = tk.Button(
            btn_frame, text="Hold to Talk", font=("Segoe UI", 11, "bold"),
            bg="#4CAF50", fg="white", activebackground="#45a049",
            relief=tk.RAISED, bd=2, height=2
        )
        self.action_btn.pack(fill=tk.X)
        self.action_btn.bind("<ButtonPress-1>", self._on_button_press)
        self.action_btn.bind("<ButtonRelease-1>", self._on_button_release)

        # Mute button
        self.mute_btn = tk.Button(
            btn_frame, text="Unmute", font=("Segoe UI", 9),
            bg="#f44336", fg="white", command=self._toggle_mute
        )
        self.mute_btn.pack(fill=tk.X, pady=(5, 0))

        # Volume slider
        vol_frame = tk.LabelFrame(parent, text="Mic Volume", font=("Segoe UI", 9), padx=10, pady=5)
        vol_frame.pack(fill=tk.X, padx=10, pady=5)

        self.vol_slider = tk.Scale(
            vol_frame, from_=0, to=100, resolution=1,
            orient=tk.HORIZONTAL, variable=self.volume,
            font=("Segoe UI", 8), command=self._on_volume_change
        )
        self.vol_slider.pack(fill=tk.X)

        # TTS Control
        tts_frame = tk.LabelFrame(parent, text="TTS Control", font=("Segoe UI", 9), padx=10, pady=5)
        tts_frame.pack(fill=tk.X, padx=10, pady=5)

        self.tts_pause_btn = tk.Button(
            tts_frame, text="Pause TTS", font=("Segoe UI", 10, "bold"),
            bg="#FF9800", fg="white", activebackground="#F57C00",
            command=self._toggle_tts_pause
        )
        self.tts_pause_btn.pack(fill=tk.X)

        # Three restart buttons in a row
        restart_frame = tk.Frame(parent)
        restart_frame.pack(fill=tk.X, padx=10, pady=(5, 0))
        restart_frame.columnconfigure(0, weight=1)
        restart_frame.columnconfigure(1, weight=1)
        restart_frame.columnconfigure(2, weight=1)

        tk.Button(
            restart_frame, text="Restart\nMic", font=("Segoe UI", 7, "bold"),
            bg="#2196F3", fg="white", activebackground="#1976D2",
            pady=2, command=self._restart
        ).grid(row=0, column=0, sticky="ew", padx=(0, 2))

        tk.Button(
            restart_frame, text="Restart\nWhisper", font=("Segoe UI", 7, "bold"),
            bg="#2196F3", fg="white", activebackground="#1976D2",
            pady=2, command=self._restart_whisper
        ).grid(row=0, column=1, sticky="ew", padx=2)

        tk.Button(
            restart_frame, text="Restart\nAllTalk", font=("Segoe UI", 7, "bold"),
            bg="#2196F3", fg="white", activebackground="#1976D2",
            pady=2, command=self._restart_alltalk
        ).grid(row=0, column=2, sticky="ew", padx=(2, 0))

        # Shutdown Services dropdown
        shutdown_frame = tk.Frame(parent)
        shutdown_frame.pack(fill=tk.X, padx=10, pady=(5, 0))

        self.shutdown_mb = tk.Menubutton(
            shutdown_frame, text="Shutdown Services \u25bc", font=("Segoe UI", 9, "bold"),
            bg="#8e44ad", fg="white", activebackground="#7d3c98",
            relief=tk.RAISED, bd=2, padx=8, pady=4,
        )
        self.shutdown_mb.pack(fill=tk.X)

        shutdown_menu = tk.Menu(self.shutdown_mb, tearoff=0, font=("Segoe UI", 9))
        shutdown_menu.add_command(label="Close All (AllTalk + Whisper + Mic)", command=self._shutdown_all_services)
        shutdown_menu.add_separator()
        shutdown_menu.add_command(label="Close AllTalk Only", command=self._shutdown_alltalk)
        shutdown_menu.add_command(label="Close Whisper Only", command=self._shutdown_whisper)
        self.shutdown_mb.config(menu=shutdown_menu)

        # Bottom buttons
        bottom_frame = tk.Frame(parent)
        bottom_frame.pack(fill=tk.X, padx=10, pady=(5, 10))

        if HAS_TRAY:
            tk.Button(
                bottom_frame, text="Minimize to Tray", font=("Segoe UI", 9),
                padx=8, pady=4, command=self._minimize_to_tray
            ).pack(side=tk.LEFT)

        tk.Button(
            bottom_frame, text="Quit Mic Panel", font=("Segoe UI", 9),
            bg="#c0392b", fg="white", activebackground="#a93226",
            padx=8, pady=4, command=self._quit
        ).pack(side=tk.RIGHT)

    def _build_settings_panel(self):
        """Build the Settings slide-out panel content."""
        # Header
        tk.Label(
            self._settings_frame, text="Settings Guide",
            font=("Segoe UI", 14, "bold"), fg="#e0e0e0", bg="#1e1e1e",
            pady=10, padx=10, anchor="w"
        ).pack(fill=tk.X)

        # Scrollable content
        canvas = tk.Canvas(self._settings_frame, bg="#1e1e1e", highlightthickness=0)
        scrollbar = tk.Scrollbar(self._settings_frame, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg="#1e1e1e")

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(fill=tk.BOTH, expand=True)

        # Content
        settings_text = """
AllTalk TTS vs Claude Code Voice Mode Settings
================================================

These are TWO SEPARATE systems with their own settings.
Changing one does NOT affect the other.

TEMPERATURE
-----------
AllTalk Temperature:
  - Controls TTS voice variation/expressiveness
  - Set via AllTalk Gradio UI (port 7852) or confignew.json
  - Range: 0.1 - 1.5 (default ~0.75)
  - Higher = more varied/expressive speech

Claude Code Voice Mode Temperature:
  - Controls the MCP server's TTS generation temperature
  - Set via the set_temperature MCP tool
  - Affects how the voice sounds when Claude speaks
  - Independent of AllTalk's own temperature setting

VOICE SELECTION
---------------
AllTalk Voice:
  - Set in AllTalk Gradio UI or via API
  - Stored in AllTalk's own config

Claude Code Voice Mode Voice:
  - Set via the set_voice MCP tool (e.g., "Freya.wav")
  - Passed to AllTalk API per-request
  - Overrides AllTalk's default for Claude's speech

SPEED
-----
AllTalk Speed:
  - AllTalk doesn't have a native speed control
  - Speed is determined by the TTS model

Claude Code Voice Mode Speed:
  - Set via the set_speed MCP tool
  - Adjusts playback sample rate (0.5x to 2.0x)
  - Post-processing, not model-level

AUDIO DEVICE & VOLUME
---------------------
  - Input device: Set in the Mic tab (this panel)
  - Volume: Set with the Mic Volume slider
  - These are mic panel settings, not AllTalk settings

TTS PAUSE
---------
  - "Pause TTS" button stops AllTalk mid-generation
  - Uses AllTalk's /api/stop-generation endpoint
  - Does not change any permanent settings
"""
        tk.Label(
            scroll_frame, text=settings_text,
            font=("Consolas", 9), fg="#cccccc", bg="#1e1e1e",
            justify=tk.LEFT, anchor="nw", padx=15, pady=10,
            wraplength=450
        ).pack(fill=tk.X)

    def _build_console_panel(self):
        """Build the Console slide-out panel with 2x2 grid of live consoles."""
        # 2x2 grid
        self._console_frame.rowconfigure(0, weight=1)
        self._console_frame.rowconfigure(1, weight=1)
        self._console_frame.columnconfigure(0, weight=1)
        self._console_frame.columnconfigure(1, weight=1)

        # Top-left: Whisper STT (real terminal emulator)
        whisper_lf = tk.LabelFrame(
            self._console_frame, text="Whisper STT", font=("Segoe UI", 9, "bold"),
            fg="#4fc3f7", padx=3, pady=3
        )
        whisper_lf.grid(row=0, column=0, sticky="nsew", padx=(5, 2), pady=(5, 2))

        self._whisper_term = Terminal(whisper_lf, font_size=9)
        self._whisper_term.pack(fill=tk.BOTH, expand=True)
        self._whisper_term.text.config(bg="#0d1117", fg="#c9d1d9")

        # Top-right: AllTalk TTS (real terminal emulator)
        alltalk_lf = tk.LabelFrame(
            self._console_frame, text="AllTalk TTS", font=("Segoe UI", 9, "bold"),
            fg="#81c784", padx=3, pady=3
        )
        alltalk_lf.grid(row=0, column=1, sticky="nsew", padx=(2, 5), pady=(5, 2))

        self._alltalk_term = Terminal(alltalk_lf, font_size=9)
        self._alltalk_term.pack(fill=tk.BOTH, expand=True)
        self._alltalk_term.text.config(bg="#0d1117", fg="#c9d1d9")

        # Bottom-left: Mic Panel Log
        mic_lf = tk.LabelFrame(
            self._console_frame, text="Mic Panel Log", font=("Segoe UI", 9, "bold"),
            fg="#ffb74d", padx=3, pady=3
        )
        mic_lf.grid(row=1, column=0, sticky="nsew", padx=(5, 2), pady=(2, 5))

        mic_scroll = tk.Scrollbar(mic_lf)
        mic_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.console_text = tk.Text(
            mic_lf, font=("Consolas", 9), bg="#1e1e1e", fg="#cccccc",
            insertbackground="#00ff00", insertwidth=2, wrap=tk.WORD,
            yscrollcommand=mic_scroll.set
        )
        self.console_text.pack(fill=tk.BOTH, expand=True)
        mic_scroll.config(command=self.console_text.yview)
        _make_readonly(self.console_text)
        _setup_link_tags(self.console_text)

        # Bottom-right: Launcher
        launcher_lf = tk.LabelFrame(
            self._console_frame, text="Launcher", font=("Segoe UI", 9, "bold"),
            fg="#ce93d8", padx=3, pady=3
        )
        launcher_lf.grid(row=1, column=1, sticky="nsew", padx=(2, 5), pady=(2, 5))

        self._build_launcher_panel(launcher_lf)

    def _build_launcher_panel(self, parent):
        """Build the launcher panel: interactive repo list + New Terminal button."""
        # Launcher console — editable so user sees a cursor
        launcher_scroll = tk.Scrollbar(parent)
        launcher_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._launcher_text = tk.Text(
            parent, font=("Consolas", 9), bg="#1a1a2e", fg="#e0e0e0",
            insertbackground="#00ff00", insertwidth=2, wrap=tk.WORD,
            yscrollcommand=launcher_scroll.set
        )
        self._launcher_text.pack(fill=tk.BOTH, expand=True)
        launcher_scroll.config(command=self._launcher_text.yview)

        # Track available repos for number-key selection
        self._launcher_repos = []

        # Bind number keys and Enter for interactive repo selection
        self._launcher_text.bind("<Return>", self._on_launcher_enter)
        # Prevent most editing but allow number input at the prompt line
        self._launcher_text.bind("<Key>", self._on_launcher_key)

        # Button bar at bottom
        btn_bar = tk.Frame(parent)
        btn_bar.pack(fill=tk.X, pady=(3, 0))

        self._repo_var = tk.StringVar()
        self._repo_combo = ttk.Combobox(
            btn_bar, textvariable=self._repo_var,
            state="readonly", font=("Segoe UI", 8)
        )
        self._repo_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))

        tk.Button(
            btn_bar, text="New Terminal", font=("Segoe UI", 8, "bold"),
            bg="#7c4dff", fg="white", activebackground="#651fff",
            command=self._open_new_terminal
        ).pack(side=tk.RIGHT)

        # Populate repo list
        self._refresh_repos()

    def _on_launcher_key(self, event):
        """Handle keystrokes in the launcher console.
        Allow digits and backspace at the prompt line; block everything else."""
        # Allow navigation keys
        if event.keysym in ("Up", "Down", "Left", "Right", "Home", "End",
                            "Prior", "Next"):  # PgUp, PgDn
            return
        # Allow digits and backspace on the prompt line
        if event.char and (event.char.isdigit() or event.keysym == "BackSpace"):
            # Only allow editing on the last line (prompt line)
            cursor_line = int(self._launcher_text.index(tk.INSERT).split(".")[0])
            last_line = int(self._launcher_text.index(tk.END + "-1c").split(".")[0])
            if cursor_line == last_line:
                return  # allow the keystroke
        # Block all other input
        return "break"

    def _on_launcher_enter(self, event):
        """Handle Enter in launcher console: open terminal for the typed number."""
        # Get text on the current (last) line after the prompt
        last_line = self._launcher_text.index(tk.END + "-1c").split(".")[0]
        line_text = self._launcher_text.get(f"{last_line}.0", f"{last_line}.end").strip()

        # Extract trailing digits (the user's choice)
        digits = ""
        for ch in reversed(line_text):
            if ch.isdigit():
                digits = ch + digits
            else:
                break

        if not digits:
            return "break"

        choice = int(digits)
        if 1 <= choice <= len(self._launcher_repos):
            repo_dir = self._launcher_repos[choice - 1]
            self._repo_var.set(repo_dir)
            self._launcher_text.insert(tk.END, "\n")
            self._open_new_terminal()
        else:
            self._launcher_text.insert(tk.END, f"\n  Invalid choice: {choice}\n")
            self._show_launcher_prompt()

        return "break"

    def _show_launcher_prompt(self):
        """Show the input prompt at the bottom of the launcher console."""
        self._launcher_text.insert(tk.END, f"Enter choice (1-{len(self._launcher_repos)}): ")
        self._launcher_text.see(tk.END)
        self._launcher_text.mark_set(tk.INSERT, tk.END)

    def _refresh_repos(self):
        """Scan REPOS_DIR for REPO_* directories and update the dropdown."""
        repos = []
        repos_path = Path(REPOS_DIR)
        if repos_path.exists():
            repos.append(str(repos_path))  # Parent dir
            for d in sorted(repos_path.iterdir()):
                if d.is_dir() and d.name.startswith("REPO_"):
                    repos.append(str(d))

        self._launcher_repos = repos
        self._repo_combo['values'] = repos
        if repos:
            default = repos[1] if len(repos) > 1 else repos[0]
            self._repo_var.set(default)

        # Show interactive menu in launcher console
        self._launcher_text.insert(tk.END, "========================================\n")
        self._launcher_text.insert(tk.END, " Select working directory:\n")
        self._launcher_text.insert(tk.END, "========================================\n\n")
        for i, r in enumerate(repos, 1):
            name = Path(r).name
            self._launcher_text.insert(tk.END, f"  {i}. {name}\n")
        self._launcher_text.insert(tk.END, "\n")
        self._show_launcher_prompt()

    def _open_new_terminal(self):
        """Open a new Claude Code terminal for the selected repo."""
        repo_dir = self._repo_var.get()
        if not repo_dir:
            return

        folder_name = Path(repo_dir).name

        # Find first available instance number (01-99)
        try:
            result = subprocess.run(
                ['wmic', 'process', 'where', "name='cmd.exe'", 'get', 'commandline'],
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            wmic_dump = result.stdout
        except Exception:
            wmic_dump = ""

        next_num = "01"
        for i in range(1, 100):
            test_num = f"{i:02d}"
            if f"title {folder_name}_{test_num}" not in wmic_dump.lower():
                next_num = test_num
                break

        terminal_name = f"{folder_name}_{next_num}"

        cmd = (
            f'cmd /k "title {terminal_name} && cd /d {repo_dir} && echo. '
            f'&& echo  Claude Code Voice Mode is ready. '
            f'&& echo  Terminal: {terminal_name} '
            f'&& echo  AllTalk TTS: http://127.0.0.1:{ALLTALK_PORT} '
            f'&& echo  Whisper STT: http://127.0.0.1:{WHISPER_PORT} '
            f'&& echo. && echo  Type your commands below. && echo."'
        )

        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)

        _append_to_text_widget(self._launcher_text,
            f"Opening Terminal in: {repo_dir}\n"
            f" Terminal name: {terminal_name}\n\n"
        )
        logger.info(f"Opened new terminal: {terminal_name} in {repo_dir}")

        # Refresh terminal list after a short delay
        self.root.after(2000, self._refresh_terminals)

    # -----------------------------------------------------------------------
    # Slide-out Panel System
    # -----------------------------------------------------------------------
    def _toggle_panel(self, panel_name):
        """Toggle a slide-out panel. None = collapse to mic only.
        Panels expand to the LEFT by shifting the window position."""
        expand_delta = PANEL_WIDTH_EXPANDED - PANEL_WIDTH_COLLAPSED

        if panel_name is None or self._active_panel == panel_name:
            # Collapse: restore to saved pre-expand position
            self._active_panel = None
            self._slideout_frame.pack_forget()
            self._settings_frame.pack_forget()
            self._console_frame.pack_forget()
            self.root.update_idletasks()
            y = self.root.winfo_y()
            restore_x = self._pre_expand_x if self._pre_expand_x is not None else self.root.winfo_x() + expand_delta
            self._pre_expand_x = None
            self.root.geometry(f"{PANEL_WIDTH_COLLAPSED}x{PANEL_HEIGHT}+{restore_x}+{y}")
            self._update_tab_highlight()
            return

        # If already expanded with a different panel, just switch content (no position change)
        was_expanded = self._active_panel is not None

        # Expand
        self._active_panel = panel_name
        self._settings_frame.pack_forget()
        self._console_frame.pack_forget()

        if panel_name == "settings":
            self._settings_frame.pack(fill=tk.BOTH, expand=True)
        elif panel_name == "console":
            self._console_frame.pack(fill=tk.BOTH, expand=True)

        self._slideout_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        if not was_expanded:
            # Save current position for correct collapse, then shift LEFT
            self.root.update_idletasks()
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            self._pre_expand_x = x  # save for collapse
            new_x = max(0, x - expand_delta)
            self.root.geometry(f"{PANEL_WIDTH_EXPANDED}x{PANEL_HEIGHT}+{new_x}+{y}")
        else:
            self.root.geometry(f"{PANEL_WIDTH_EXPANDED}x{PANEL_HEIGHT}")
        self._update_tab_highlight()

    def _update_tab_highlight(self):
        """Update tab button colors to show active state."""
        inactive = {"bg": "#1a1a2e", "fg": "#888888"}
        active = {"bg": "#16213e", "fg": "#e0e0e0"}

        self._tab_mic.config(**(active if self._active_panel is None else inactive))
        self._tab_settings.config(**(active if self._active_panel == "settings" else inactive))
        self._tab_console.config(**(active if self._active_panel == "console" else inactive))

    # -----------------------------------------------------------------------
    # Service Process Management
    # -----------------------------------------------------------------------
    def _is_service_running(self, port, health_path):
        """Check if a service is already running by hitting its health endpoint."""
        try:
            resp = requests.get(f"http://127.0.0.1:{port}{health_path}", timeout=0.5)
            return resp.status_code == 200
        except Exception:
            return False

    def _auto_start_services(self):
        """Auto-start Whisper and AllTalk if not already running.
        Boot sequence: launcher console (already populated) → Whisper → AllTalk.
        Runs detection in a background thread to avoid blocking UI."""
        logger.info("Auto-start services triggered")
        def _do_auto_start():
            try:
                # Check and start Whisper
                if self._is_service_running(WHISPER_PORT, WHISPER_HEALTH_PATH):
                    logger.info("Whisper STT already running — skipping auto-start")
                else:
                    logger.info("Whisper STT not running — auto-starting...")
                    self.root.after(0, self._start_whisper)
                    # Wait for Whisper to be ready before starting AllTalk
                    self._wait_for_service_ready(WHISPER_PORT, WHISPER_HEALTH_PATH, timeout=60)

                # Check and start AllTalk
                if self._is_service_running(ALLTALK_PORT, ALLTALK_HEALTH_PATH):
                    logger.info("AllTalk TTS already running — skipping auto-start")
                else:
                    logger.info("AllTalk TTS not running — auto-starting...")
                    self.root.after(0, self._start_alltalk)
            except Exception:
                logger.exception("Auto-start services failed")

        threading.Thread(target=_do_auto_start, daemon=True).start()

    def _start_whisper(self):
        """Start Whisper STT via the embedded terminal emulator."""
        if self._whisper_proc:
            logger.info("Whisper already running via terminal")
            return
        cmd = f'cd /d {WHISPER_CWD} && call venv\\Scripts\\activate.bat && python server.py\r\n'
        logger.info(f"Launching Whisper STT: {cmd.strip()}")
        self._whisper_term.winpty.send_command(cmd)
        self._whisper_proc = True  # Mark as running (PTY manages the process)

    def _start_alltalk(self):
        """Start AllTalk TTS via the embedded terminal emulator."""
        if self._alltalk_proc:
            logger.info("AllTalk already running via terminal")
            return
        cmd = f'cd /d {ALLTALK_CWD} && call start_alltalk.bat\r\n'
        logger.info(f"Launching AllTalk TTS: {cmd.strip()}")
        self._alltalk_term.winpty.send_command(cmd)
        self._alltalk_proc = True  # Mark as running (PTY manages the process)

    def _wait_for_service_ready(self, port, health_path, timeout=60):
        """Poll a service's health endpoint until it responds 200, or timeout."""
        url = f"http://127.0.0.1:{port}{health_path}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = requests.get(url, timeout=2)
                if resp.status_code == 200:
                    logger.info(f"Service on port {port} is ready")
                    return True
            except requests.ConnectionError:
                pass
            except Exception as e:
                logger.warning(f"Health check for port {port} failed: {e}")
            time.sleep(2)
        logger.warning(f"Service on port {port} did not become ready within {timeout}s")
        return False

    def _kill_service_on_port(self, port, service_name):
        """Kill whatever is running on the given port."""
        pid = self._find_pid_on_port(port)
        if pid:
            logger.info(f"Killing {service_name} (PID {pid}) on port {port}")
            try:
                subprocess.run(
                    ['taskkill', '/pid', str(pid), '/t', '/f'],
                    capture_output=True, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as e:
                logger.error(f"Failed to kill {service_name}: {e}")
            # Also kill our subprocess handle if we have one
            time.sleep(1)

    def _restart_whisper(self):
        """Restart Whisper STT: kill existing service and relaunch via terminal."""
        def do_restart():
            logger.info("Restarting Whisper STT...")
            self.root.after(0, self._set_status, "Restarting Whisper...", "#ffcc00")

            # Kill existing service on the port
            self._kill_service_on_port(WHISPER_PORT, "Whisper STT")
            self._whisper_proc = None  # Reset so _start_whisper will run
            time.sleep(2)

            # Send Ctrl+C to the terminal PTY to stop any running command, then relaunch
            self._whisper_term.winpty.send_command('\x03\r\n')
            time.sleep(1)
            self._start_whisper()
            if self._wait_for_service_ready(WHISPER_PORT, WHISPER_HEALTH_PATH, timeout=30):
                logger.info("Whisper STT restarted successfully")
                self.root.after(0, self._set_status, "Whisper restarted", "#00cc00")
            else:
                self.root.after(0, self._set_status, "Whisper not responding", "#ff4444")
            self.root.after(3000, self._reset_status_if_idle)

        threading.Thread(target=do_restart, daemon=True).start()

    def _restart_alltalk(self):
        """Restart AllTalk TTS: kill existing service and relaunch via terminal."""
        def do_restart():
            logger.info("Restarting AllTalk TTS...")
            self.root.after(0, self._set_status, "Restarting AllTalk...", "#ffcc00")

            # Kill existing service on the port
            self._kill_service_on_port(ALLTALK_PORT, "AllTalk TTS")
            self._alltalk_proc = None  # Reset so _start_alltalk will run
            time.sleep(2)

            # Send Ctrl+C to the terminal PTY to stop any running command, then relaunch
            self._alltalk_term.winpty.send_command('\x03\r\n')
            time.sleep(1)
            self._start_alltalk()
            if self._wait_for_service_ready(ALLTALK_PORT, ALLTALK_HEALTH_PATH, timeout=60):
                logger.info("AllTalk TTS restarted successfully")
                self.root.after(0, self._set_status, "AllTalk restarted", "#00cc00")
            else:
                self.root.after(0, self._set_status, "AllTalk not responding", "#ff4444")
            self.root.after(3000, self._reset_status_if_idle)

        threading.Thread(target=do_restart, daemon=True).start()

    # -----------------------------------------------------------------------
    # Mode, device, recording handlers (unchanged from original)
    # -----------------------------------------------------------------------
    def _on_mode_change(self):
        """Handle mode radio button change."""
        mode = self.mode.get()
        logger.info(f"Mode changed to: {mode}")

        if mode == "push_to_talk":
            self._stop_vad_listening()
            self.action_btn.config(text="Hold to Talk")
            self._stop_recording()
        elif mode == "toggle":
            self._stop_recording()  # stop any manual recording
            if not self.is_muted:
                self._start_vad_listening()
            else:
                self.action_btn.config(text="Click to Talk")

        self._update_state()
        self._update_mode_labels()

    def _on_device_change(self, event=None):
        """Handle input device dropdown change."""
        device_name = self.selected_device.get()
        logger.info(f"Input device changed to: {device_name}")
        # Persist the choice for next session
        prefs = load_prefs()
        prefs["input_device"] = device_name
        save_prefs(prefs)
        self._update_state()
        # Restart level monitor with the new device
        self._level_monitor_stop.set()
        self._level_monitor_stop = threading.Event()
        self._start_level_monitor()

    def _refresh_devices(self):
        """Re-query input devices and update the dropdown."""
        self._input_devices = self._query_input_devices()
        device_names = ["Windows Default"] + [d["name"] for d in self._input_devices]
        self.device_combo['values'] = device_names

        current = self.selected_device.get()
        if current not in device_names:
            self.selected_device.set("Windows Default")
            logger.info("Previously selected device no longer available, reset to Windows Default")
            self._on_device_change()

        logger.info(f"Refreshed input devices: {len(self._input_devices)} found")

    def _on_button_press(self, event=None):
        mode = self.mode.get()
        if mode == "push_to_talk":
            self._start_recording()
        elif mode == "toggle":
            # In VAD toggle mode, button press = "force send now"
            if self._vad_state in ("RECORDING", "TRAILING"):
                frames_copy = self._vad_recording_frames
                self._vad_recording_frames = []
                self._vad_state = "PROCESSING"
                self._on_vad_silence_timeout(frames_copy)
            elif self._vad_state == "IDLE":
                # Fallback: old click-to-toggle behavior if VAD not active
                if self.is_recording:
                    self._stop_recording()
                else:
                    self._start_recording()

    def _on_button_release(self, event=None):
        mode = self.mode.get()
        if mode == "push_to_talk":
            self._stop_recording()

    def _start_recording(self):
        """Start capturing microphone audio via the shared stream."""
        if self.is_recording:
            return
        if self._processing:
            logger.warning("Still processing previous recording, ignoring")
            return

        with self._recording_lock:
            self._recording_frames.clear()
        self._rms_log_counter = 0

        # In push-to-talk mode, holding the button unmutes the mic
        if self.mode.get() == "push_to_talk":
            self.is_muted = False
            self.mute_btn.config(text="Mute", bg="#666")

        self.is_recording = True
        self.action_btn.config(bg="#f44336")
        self.status_label.config(text="Recording...", fg="#ff4444")
        self._update_state()
        logger.info("Recording started (using shared stream)")

    def _stop_recording(self):
        """Stop capturing microphone audio and begin processing."""
        if not self.is_recording:
            return
        self.is_recording = False

        mode = self.mode.get()
        if mode == "toggle":
            self.action_btn.config(bg="#4CAF50", text="Click to Talk")
        else:
            self.action_btn.config(bg="#4CAF50", text="Hold to Talk")

        # In push-to-talk mode, releasing the button mutes the mic
        if mode == "push_to_talk":
            self.is_muted = True
            self.mute_btn.config(text="Unmute", bg="#f44336")

        self._update_state()

        with self._recording_lock:
            frame_count = len(self._recording_frames)
            has_frames = frame_count > 0

        if has_frames:
            self._processing = True
            self.status_label.config(text="Processing...", fg="#ffcc00")
            logger.info(f"Recording stopped, {frame_count} frames captured, processing...")
            thread = threading.Thread(target=self._process_recording, daemon=True)
            thread.start()
        else:
            self.status_label.config(text="Ready", fg="#00cc00")
            logger.info("Recording stopped (no frames captured)")

    # -------------------------------------------------------------------
    # VAD Toggle-to-Talk: voice activity detection methods
    # -------------------------------------------------------------------
    def _init_vad(self):
        """Lazy initialization of webrtcvad."""
        if self._vad is None:
            try:
                import webrtcvad
                self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
                logger.info(f"VAD initialized (aggressiveness={VAD_AGGRESSIVENESS})")
            except ImportError:
                logger.error("webrtcvad not installed — VAD unavailable, falling back to manual toggle")
                self._vad = False  # sentinel: tried and failed

    def _start_vad_listening(self):
        """Enter VAD LISTENING state — mic stays live, watching for speech."""
        self._init_vad()
        if self._vad is False:
            return  # webrtcvad not available
        self._vad_buffer = bytearray()
        self._vad_pre_buffer = []
        self._vad_recording_frames = []
        self._vad_silence_count = 0
        self._vad_speech_count = 0
        self._vad_state = "LISTENING"
        self.status_label.config(text="Listening...", fg="#00cc00")
        self.action_btn.config(text="Listening...", bg="#4CAF50")
        logger.info("VAD listening started")

    def _stop_vad_listening(self):
        """Return to IDLE — stop all VAD processing."""
        self._vad_state = "IDLE"
        self._vad_buffer = bytearray()
        self._vad_pre_buffer = []
        self._vad_recording_frames = []
        self._vad_silence_count = 0
        self._vad_speech_count = 0
        logger.info("VAD listening stopped")

    def _process_vad_frame(self, indata):
        """Process audio through VAD. Called from audio callback — must be lightweight."""
        multiplier = self.volume.get() / 50.0
        scaled = (indata.copy().astype(np.float32) * multiplier).astype(np.int16)
        raw_bytes = scaled.tobytes()

        # Add to alignment buffer
        self._vad_buffer.extend(raw_bytes)

        pre_buffer_max = int(VAD_PRE_BUFFER_MS / 30)  # ~10 frames

        # Process all complete 30ms sub-frames
        while len(self._vad_buffer) >= VAD_FRAME_BYTES:
            frame_bytes = bytes(self._vad_buffer[:VAD_FRAME_BYTES])
            del self._vad_buffer[:VAD_FRAME_BYTES]

            try:
                is_speech = self._vad.is_speech(frame_bytes, SAMPLE_RATE)
            except Exception:
                continue

            if self._vad_state == "LISTENING":
                # Maintain pre-buffer (rolling window of recent audio)
                self._vad_pre_buffer.append(frame_bytes)
                if len(self._vad_pre_buffer) > pre_buffer_max:
                    self._vad_pre_buffer.pop(0)

                if is_speech:
                    self._vad_speech_count += 1
                    if self._vad_speech_count >= VAD_SPEECH_ONSET_FRAMES:
                        # Speech confirmed — start recording
                        self._vad_state = "RECORDING"
                        self._vad_recording_frames = list(self._vad_pre_buffer)
                        self._vad_recording_frames.append(frame_bytes)
                        self._vad_silence_count = 0
                        self.root.after(0, self._on_vad_recording_start)
                else:
                    self._vad_speech_count = 0

            elif self._vad_state == "RECORDING":
                self._vad_recording_frames.append(frame_bytes)
                if not is_speech:
                    self._vad_state = "TRAILING"
                    self._vad_silence_count = 1
                self._vad_speech_count = 0

            elif self._vad_state == "TRAILING":
                self._vad_recording_frames.append(frame_bytes)
                if is_speech:
                    self._vad_state = "RECORDING"
                    self._vad_silence_count = 0
                else:
                    self._vad_silence_count += 1
                    threshold = int(VAD_SILENCE_TIMEOUT * 1000 / 30)
                    if self._vad_silence_count >= threshold:
                        self._vad_state = "PROCESSING"
                        frames_copy = self._vad_recording_frames
                        self._vad_recording_frames = []
                        self.root.after(0, self._on_vad_silence_timeout, frames_copy)

    def _on_vad_recording_start(self):
        """UI update when VAD detects speech onset. Called on main thread."""
        self.status_label.config(text="Speech detected...", fg="#ff4444")
        self.action_btn.config(text="Speaking...", bg="#f44336")
        logger.info("VAD: speech onset detected")

    def _on_vad_silence_timeout(self, vad_frames):
        """Process captured audio after VAD silence timeout. Called on main thread."""
        if self._processing:
            logger.warning("Still processing previous VAD recording, dropping this one")
            if self.mode.get() == "toggle" and not self.is_muted:
                self._vad_state = "LISTENING"
                self.status_label.config(text="Listening...", fg="#00cc00")
                self.action_btn.config(text="Listening...", bg="#4CAF50")
            else:
                self._vad_state = "IDLE"
            return

        # Concatenate 30ms frame byte chunks into a single int16 array
        raw_bytes = b"".join(vad_frames)
        audio = np.frombuffer(raw_bytes, dtype=np.int16)
        duration = len(audio) / SAMPLE_RATE

        if duration < VAD_MIN_RECORDING_S:
            logger.info(f"VAD recording too short ({duration:.2f}s), skipping")
            if self.mode.get() == "toggle" and not self.is_muted:
                self._vad_state = "LISTENING"
                self.status_label.config(text="Listening...", fg="#00cc00")
                self.action_btn.config(text="Listening...", bg="#4CAF50")
            else:
                self._vad_state = "IDLE"
            return

        self._processing = True
        self.status_label.config(text="Processing...", fg="#ffcc00")
        self.action_btn.config(text="Processing...", bg="#FF9800")

        thread = threading.Thread(
            target=self._process_vad_recording, args=(audio,), daemon=True
        )
        thread.start()

    def _process_vad_recording(self, audio):
        """Transcribe VAD-captured audio and inject into terminal. Runs in background thread."""
        try:
            duration = len(audio) / SAMPLE_RATE
            rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
            peak = np.max(np.abs(audio.astype(np.float32)))
            logger.info(
                f"VAD processing {duration:.1f}s of audio "
                f"(RMS={rms:.1f}, peak={peak:.0f})"
            )

            if rms < 50:
                logger.info(f"VAD recording was silence (RMS {rms:.1f} < 50), skipping")
                self.root.after(0, self._set_status, "No speech detected", "#ff8800")
                return

            self.root.after(0, self._set_status, "Transcribing...", "#ffcc00")
            wav_bytes = self._numpy_to_wav_bytes(audio)
            logger.info(f"VAD WAV size: {len(wav_bytes)} bytes")
            text = self._transcribe_audio_direct(wav_bytes)

            if not text:
                logger.warning("VAD transcription returned empty")
                self.root.after(0, self._set_status, "No speech recognized", "#ff8800")
                return

            self.root.after(0, self._set_status, f"Sending: {text[:40]}...", "#00ccff")
            success = self._inject_text_into_terminal(text)

            if success:
                self.root.after(0, self._set_status, "Sent!", "#00cc00")
                logger.info(f"VAD: sent to terminal: '{text}'")
            else:
                self.root.after(0, self._set_status, "No terminal selected", "#ff4444")
                logger.error("VAD: failed to inject text into terminal")
        except Exception as e:
            logger.error(f"VAD processing failed: {e}")
            self.root.after(0, self._set_status, f"Error: {e}", "#ff4444")
        finally:
            self._processing = False
            if self.mode.get() == "toggle" and not self.is_muted:
                self._vad_state = "LISTENING"
                self.root.after(0, self._set_status, "Listening...", "#00cc00")
                self.root.after(0, lambda: self.action_btn.config(text="Listening...", bg="#4CAF50"))
            else:
                self._vad_state = "IDLE"
                self.root.after(3000, self._reset_status_if_idle)

    def _toggle_mute(self):
        """Toggle microphone mute."""
        self.is_muted = not self.is_muted
        if self.is_muted:
            self.mute_btn.config(text="Unmute", bg="#f44336")
            self.status_label.config(text="Muted", fg="#ff8800")
            if self.mode.get() == "toggle":
                self._stop_vad_listening()
        else:
            self.mute_btn.config(text="Mute", bg="#666")
            if self.mode.get() == "toggle":
                self._start_vad_listening()
            else:
                self.status_label.config(text="Ready", fg="#00cc00")
        self._update_state()
        self._update_mode_labels()
        logger.info(f"Mute: {self.is_muted}")

    def _update_mode_labels(self):
        """Update Toggle to Talk radio button label to show mute/VAD state."""
        if self.is_muted:
            state = "(Muted)"
        elif self.mode.get() == "toggle":
            state = "(VAD Active)"
        else:
            state = "(Unmuted)"
        self._rb_toggle.config(text=f"Toggle to Talk {state}")

    def _on_volume_change(self, value):
        """Handle volume slider change."""
        self._update_state()

    def _toggle_tts_pause(self):
        """Toggle TTS pause state."""
        self.tts_paused = not self.tts_paused
        if self.tts_paused:
            self.tts_pause_btn.config(text="Continue TTS", bg="#4CAF50")
            logger.info("TTS paused")
            try:
                requests.put("http://127.0.0.1:7851/api/stop-generation", timeout=2)
            except Exception as e:
                logger.warning(f"Failed to stop AllTalk generation: {e}")
        else:
            self.tts_pause_btn.config(text="Pause TTS", bg="#FF9800")
            logger.info("TTS resumed")
        self._update_state()

    def _setup_console_logging(self):
        """Attach a TextHandler to the logger so logs appear in the embedded console."""
        handler = TextHandler(self.console_text)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)

    def _update_state(self):
        """Save current state to file for MCP server."""
        device_name = self.selected_device.get()
        state = {
            "mode": self.mode.get(),
            "recording": self.is_recording,
            "muted": self.is_muted,
            "volume": self.volume.get(),
            "tts_paused": self.tts_paused,
            "input_device": None if device_name == "Windows Default" else device_name,
            "vad_state": self._vad_state,
        }
        save_mic_state(state)

    # -----------------------------------------------------------------------
    # Hold to Talk: recording, transcription, and terminal injection
    # -----------------------------------------------------------------------
    def _set_status(self, text, color):
        """Update the status label (must be called from main thread)."""
        self.status_label.config(text=text, fg=color)

    def _reset_status_if_idle(self):
        """Reset status to Ready if not recording or processing."""
        if not self.is_recording and not self._processing:
            if self.is_muted:
                self.status_label.config(text="Muted", fg="#ff8800")
            else:
                self.status_label.config(text="Ready", fg="#00cc00")

    def _numpy_to_wav_bytes(self, audio_int16):
        """Convert numpy int16 audio array to WAV bytes."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_int16.tobytes())
        return buf.getvalue()

    def _transcribe_audio_direct(self, wav_bytes):
        """Send WAV bytes to Whisper STT and return transcribed text."""
        try:
            response = requests.post(
                f"{WHISPER_URL}/v1/audio/transcriptions",
                files={"file": ("recording.wav", wav_bytes, "audio/wav")},
                data={"model": "whisper-1", "language": "en"},
                timeout=30,
            )
            if response.status_code == 200:
                result = response.json()
                text = result.get("text", "").strip()
                logger.info(f"Transcribed: '{text}'")
                return text
            else:
                logger.error(f"Whisper returned {response.status_code}: {response.text}")
                return ""
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return ""

    def _discover_claude_terminals(self):
        """Discover all Claude Code terminals by searching cmd.exe command lines.
        Returns list of (terminal_name, pid) tuples."""
        terminals = []
        try:
            result = subprocess.run(
                ['wmic', 'process', 'where', "name='cmd.exe'", 'get', 'processid,commandline'],
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                # Match "title FOLDER_NN" pattern in command line
                match = re.search(r'title\s+(\w+_\d{2})\b', line, re.IGNORECASE)
                if match:
                    terminal_name = match.group(1)
                    # PID is the last number sequence on the line (wmic default format)
                    pid_match = re.search(r'(\d+)\s*$', line)
                    if pid_match:
                        pid = int(pid_match.group(1))
                        terminals.append((terminal_name, pid))
                        logger.info(f"Discovered terminal: {terminal_name} (PID {pid})")
        except Exception as e:
            logger.error(f"Terminal discovery failed: {e}")
        return terminals

    def _get_selected_terminal_pid(self):
        """Get the PID of the currently selected terminal.
        Returns int PID or None."""
        name = self._selected_terminal.get()
        if not name:
            return None
        for term_name, pid in self._discovered_terminals:
            if term_name == name:
                return pid
        return None

    def _refresh_terminals(self):
        """Re-scan for Claude Code terminals and update the dropdown."""
        self._discovered_terminals = self._discover_claude_terminals()
        names = [name for name, pid in self._discovered_terminals]
        self.terminal_combo['values'] = names

        current = self._selected_terminal.get()
        if names:
            if current not in names:
                self._selected_terminal.set(names[0])
                logger.info(f"Auto-selected terminal: {names[0]}")
        else:
            self._selected_terminal.set("")
            logger.info("No Claude Code terminals found")

    def _inject_text_into_terminal(self, text):
        """Inject transcribed text into the selected Claude Code Terminal and press Enter.
        Returns True on success, False on failure."""
        if not text.strip():
            logger.warning("No text to inject")
            return False

        pid = self._get_selected_terminal_pid()
        if pid is None:
            # Try refreshing terminals first
            self.root.after(0, self._refresh_terminals)
            time.sleep(0.5)
            pid = self._get_selected_terminal_pid()
            if pid is None:
                logger.error("No Claude Code Terminal selected or found")
                return False

        kernel32 = ctypes.windll.kernel32
        kernel32.FreeConsole()

        if not kernel32.AttachConsole(pid):
            error_code = ctypes.GetLastError()
            logger.warning(f"AttachConsole({pid}) failed (error {error_code})")
            # Terminal might have closed — refresh and retry
            self.root.after(0, self._refresh_terminals)
            time.sleep(0.5)
            pid = self._get_selected_terminal_pid()
            if pid is None or not kernel32.AttachConsole(pid):
                logger.error("Cannot attach to Claude Code Terminal after retry")
                return False

        try:
            ok = self._write_console_keys(text)
            if not ok:
                logger.error("Failed to write text characters to console")
                return False

            time.sleep(0.05)

            ok = self._write_console_keys("\r")
            if not ok:
                logger.error("Failed to write Enter key to console")
                return False

            terminal_name = self._selected_terminal.get()
            logger.info(f"Injected {len(text)} chars + Enter into {terminal_name} (PID {pid})")
            return True
        finally:
            self._detach_console()

    def _process_recording(self):
        """Process accumulated recording: transcribe and inject into Claude Code terminal.
        Runs in a background thread."""
        try:
            with self._recording_lock:
                frames = self._recording_frames.copy()
                self._recording_frames.clear()

            if not frames:
                logger.warning("No audio frames captured")
                self.root.after(0, self._set_status, "No audio captured", "#ff8800")
                return

            self.root.after(0, self._set_status, "Transcribing...", "#ffcc00")

            audio = np.concatenate(frames).flatten()
            duration = len(audio) / SAMPLE_RATE
            rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
            peak = np.max(np.abs(audio.astype(np.float32)))
            logger.info(
                f"Processing {duration:.1f}s of recorded audio "
                f"({len(frames)} frames, RMS={rms:.1f}, peak={peak:.0f})"
            )

            if rms < 50:
                logger.info(f"Recording was silence (RMS {rms:.1f} < 50), skipping transcription")
                self.root.after(0, self._set_status, "No speech detected", "#ff8800")
                return

            wav_bytes = self._numpy_to_wav_bytes(audio)
            logger.info(f"WAV size: {len(wav_bytes)} bytes")

            text = self._transcribe_audio_direct(wav_bytes)

            if not text:
                logger.warning("Transcription returned empty text")
                self.root.after(0, self._set_status, "No speech recognized", "#ff8800")
                return

            self.root.after(0, self._set_status, f"Sending: {text[:40]}...", "#00ccff")

            success = self._inject_text_into_terminal(text)

            if success:
                self.root.after(0, self._set_status, "Sent!", "#00cc00")
                logger.info(f"Successfully sent to terminal: '{text}'")
            else:
                self.root.after(0, self._set_status, "No terminal selected", "#ff4444")
                logger.error("Failed to inject text into terminal")

        except Exception as e:
            logger.error(f"Recording processing failed: {e}")
            self.root.after(0, self._set_status, f"Error: {e}", "#ff4444")
        finally:
            self._processing = False
            self.root.after(3000, self._reset_status_if_idle)

    def _start_level_monitor(self):
        """Start a background thread with a shared InputStream for level metering AND recording."""
        stop_event = self._level_monitor_stop
        device_index = self._get_selected_device_index()
        device_name = self.selected_device.get()
        self._rms_log_counter = 0

        def shared_callback(indata, frames, time_info, status):
            if status:
                logger.warning(f"Audio stream status: {status}")
            if indata is None or len(indata) == 0:
                return

            multiplier = self.volume.get() / 50.0

            # Always: update level meter
            level = np.sqrt(np.mean(indata.astype(np.float32) ** 2)) * multiplier
            self.level_value = min(100, level * 500)

            # When manually recording (push_to_talk or legacy toggle): accumulate frames
            if self.is_recording:
                if self.is_muted:
                    return
                scaled = (indata.copy().astype(np.float32) * multiplier).astype(np.int16)
                with self._recording_lock:
                    self._recording_frames.append(scaled)

                # Diagnostic: log RMS every ~1 second (every 16 callbacks at 1024 blocksize / 16kHz)
                self._rms_log_counter += 1
                if self._rms_log_counter % 16 == 0:
                    rms = np.sqrt(np.mean(scaled.astype(np.float32) ** 2))
                    logger.debug(f"Recording RMS: {rms:.1f}")
                return  # Don't also run VAD when manually recording

            # VAD processing for toggle-to-talk mode
            if (self._vad_state in ("LISTENING", "RECORDING", "TRAILING")
                    and not self.is_muted):
                self._process_vad_frame(indata)

        def monitor():
            try:
                kwargs = {
                    "samplerate": SAMPLE_RATE, "channels": CHANNELS,
                    "dtype": "int16", "blocksize": 1024, "callback": shared_callback,
                }
                if device_index is not None:
                    kwargs["device"] = device_index

                logger.info(f"Shared audio stream opened on device: {device_name} (index={device_index})")
                with sd.InputStream(**kwargs):
                    while not stop_event.is_set():
                        sd.sleep(50)
                logger.info("Shared audio stream closed")
            except Exception as e:
                logger.error(f"Shared audio stream error: {e}")

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()

        def update_meter():
            self.level_bar["value"] = self.level_value
            self.root.after(50, update_meter)

        # Only start the meter updater once (first call)
        if not hasattr(self, '_meter_updater_started'):
            self._meter_updater_started = True
            self.root.after(50, update_meter)

    # -----------------------------------------------------------------------
    # System tray
    # -----------------------------------------------------------------------
    def _minimize_to_tray(self):
        """Hide window and show system tray icon."""
        if not HAS_TRAY:
            self.root.iconify()
            return

        self.root.withdraw()
        self.hidden = True

        icon_image = create_tray_icon_image("green")
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._restore_from_tray),
            pystray.MenuItem("Quit", self._quit_from_tray),
        )
        self.tray_icon = pystray.Icon("claude_code_voice_mode", icon_image, "Claude Code Voice Mode", menu)

        tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        tray_thread.start()

    def _restore_from_tray(self, icon=None, item=None):
        """Restore window from system tray."""
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.hidden = False
        self.root.after(0, self.root.deiconify)

    def _quit_from_tray(self, icon=None, item=None):
        """Quit from tray icon menu."""
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.after(0, self._quit)

    def on_close(self):
        """Handle window close button — minimize to tray instead of quitting."""
        if HAS_TRAY:
            self._minimize_to_tray()
        else:
            self._quit()

    # -----------------------------------------------------------------------
    # Service shutdown helpers
    # -----------------------------------------------------------------------
    def _find_pid_on_port(self, port):
        """Find the PID of the process listening on the given port."""
        try:
            result = subprocess.run(
                ['netstat', '-ano'],
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            for line in result.stdout.splitlines():
                if f':{port} ' in line and 'LISTENING' in line:
                    return line.strip().split()[-1]
        except Exception as e:
            logger.error(f"Failed to find PID on port {port}: {e}")
        return None

    def _find_console_ancestor_pid(self, pid):
        """Walk up the process tree (max 5 levels) to find the ancestor cmd.exe."""
        current_pid = str(pid)
        for _ in range(5):
            try:
                result = subprocess.run(
                    ['wmic', 'process', 'where', f'processid={current_pid}', 'get', 'parentprocessid'],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                parent_pid = None
                for line in result.stdout.strip().splitlines()[1:]:
                    line = line.strip()
                    if line.isdigit():
                        parent_pid = line
                        break
                if not parent_pid:
                    return None, None

                # Check parent's process name
                name_result = subprocess.run(
                    ['wmic', 'process', 'where', f'processid={parent_pid}', 'get', 'name'],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                parent_name = ""
                for name_line in name_result.stdout.strip().splitlines()[1:]:
                    name_line = name_line.strip()
                    if name_line:
                        parent_name = name_line.lower()
                        break

                if parent_name == 'cmd.exe':
                    return parent_pid, parent_name

                # Not cmd.exe yet — continue up the tree
                current_pid = parent_pid
            except Exception as e:
                logger.error(f"Failed to find ancestor of PID {current_pid}: {e}")
                return None, None
        logger.warning(f"No cmd.exe ancestor found within 5 levels of PID {pid}")
        return None, None

    def _attach_and_send_ctrl_c(self, pid):
        """Attach to a process's console and send Ctrl+C."""
        kernel32 = ctypes.windll.kernel32

        # Detach from any previous console first
        kernel32.FreeConsole()

        if not kernel32.AttachConsole(int(pid)):
            logger.warning(f"Could not attach to console of PID {pid} (error {ctypes.GetLastError()})")
            return False

        # Prevent Ctrl+C from killing our own process
        kernel32.SetConsoleCtrlHandler(None, True)

        # Send Ctrl+C to all processes on that console
        kernel32.GenerateConsoleCtrlEvent(0, 0)
        logger.info(f"Ctrl+C sent to console of PID {pid}")
        return True

    def _detach_console(self):
        """Detach from the currently attached console."""
        kernel32 = ctypes.windll.kernel32
        kernel32.FreeConsole()
        kernel32.SetConsoleCtrlHandler(None, False)

    def _write_console_keys(self, text):
        """Write keystrokes to the attached console's input buffer via CONIN$."""
        kernel32 = ctypes.windll.kernel32

        # Open CONIN$ directly — works for pythonw which has no std handles
        GENERIC_READ_WRITE = 0x80000000 | 0x40000000
        FILE_SHARE_READ_WRITE = 0x01 | 0x02
        OPEN_EXISTING = 3

        kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
            ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.HANDLE,
        ]
        kernel32.WriteConsoleInputW.restype = ctypes.wintypes.BOOL
        kernel32.WriteConsoleInputW.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.c_void_p,
            ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]

        conin = kernel32.CreateFileW("CONIN$", GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
        INVALID_HANDLE = ctypes.wintypes.HANDLE(-1).value
        if conin == INVALID_HANDLE:
            logger.warning(f"CreateFileW CONIN$ failed (error {ctypes.GetLastError()})")
            return False

        KEY_EVENT = 0x0001

        class KEY_EVENT_RECORD(ctypes.Structure):
            _fields_ = [
                ("bKeyDown", ctypes.wintypes.BOOL),
                ("wRepeatCount", ctypes.wintypes.WORD),
                ("wVirtualKeyCode", ctypes.wintypes.WORD),
                ("wVirtualScanCode", ctypes.wintypes.WORD),
                ("uChar", ctypes.c_wchar),
                ("dwControlKeyState", ctypes.wintypes.DWORD),
            ]

        class INPUT_RECORD(ctypes.Structure):
            _fields_ = [
                ("EventType", ctypes.wintypes.WORD),
                ("_padding", ctypes.wintypes.WORD),
                ("Event", KEY_EVENT_RECORD),
            ]

        written = ctypes.wintypes.DWORD()
        ok = True
        for ch in text:
            vk = 0x0D if ch == '\r' else 0  # VK_RETURN for Enter, 0 for others
            for key_down in (True, False):
                record = INPUT_RECORD()
                record.EventType = KEY_EVENT
                record._padding = 0
                record.Event.bKeyDown = key_down
                record.Event.wRepeatCount = 1
                record.Event.wVirtualKeyCode = vk
                record.Event.wVirtualScanCode = 0
                record.Event.uChar = ch
                record.Event.dwControlKeyState = 0
                success = kernel32.WriteConsoleInputW(conin, ctypes.byref(record), 1, ctypes.byref(written))
                if not success:
                    logger.warning(f"WriteConsoleInputW failed for '{ch}' (error {ctypes.GetLastError()})")
                    ok = False

        kernel32.CloseHandle(conin)
        return ok

    def _wait_for_port_free(self, port, timeout=10):
        """Poll until nothing is listening on the port, or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                result = sock.connect_ex(('127.0.0.1', port))
                if result != 0:
                    return True  # Port is free
            finally:
                sock.close()
            time.sleep(0.5)
        return False  # Timed out, port still in use

    def _graceful_shutdown_service(self, port, service_name, timeout=10):
        """Gracefully shut down a service by port: Ctrl+C, wait, answer batch prompt, close terminal."""
        pid = self._find_pid_on_port(port)
        if not pid:
            logger.warning(f"No process found on port {port} for {service_name}")
            return

        logger.info(f"Shutting down {service_name} (PID {pid} on port {port})...")

        # Find ancestor cmd.exe before killing — we'll need it to close the terminal
        parent_pid, parent_name = self._find_console_ancestor_pid(pid)

        # Attach to the server's console and send Ctrl+C (stays attached)
        attached = self._attach_and_send_ctrl_c(int(pid))

        if attached:
            # Wait for the port to become free (server shutting down gracefully)
            if self._wait_for_port_free(port, timeout):
                logger.info(f"{service_name} shut down gracefully")
                time.sleep(1)
                self._write_console_keys("y\r")
                time.sleep(0.5)
            else:
                # Force kill as fallback
                logger.warning(f"{service_name} did not stop in {timeout}s, force-killing PID {pid}")
                subprocess.run(
                    ['taskkill', '/pid', pid, '/t', '/f'],
                    capture_output=True, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )

            # Detach from the console
            self._detach_console()
        else:
            # Couldn't attach to console — force kill
            logger.warning(f"Could not send Ctrl+C to {service_name}, force-killing PID {pid}")
            subprocess.run(
                ['taskkill', '/pid', pid, '/t', '/f'],
                capture_output=True, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

        # Close the ancestor cmd.exe terminal window if found
        if parent_pid and parent_name == 'cmd.exe':
            logger.info(f"Closing terminal window (cmd.exe PID {parent_pid})")
            subprocess.run(
                ['taskkill', '/pid', parent_pid, '/t', '/f'],
                capture_output=True, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

        logger.info(f"{service_name} shutdown complete")

    def _shutdown_alltalk(self):
        """Gracefully shut down AllTalk TTS and its console window."""
        if not messagebox.askyesno("Confirm", "Close AllTalk TTS server and its console?"):
            return
        threading.Thread(target=self._graceful_shutdown_service, args=(7851, "AllTalk TTS"), daemon=True).start()

    def _shutdown_whisper(self):
        """Gracefully shut down Whisper STT and its console window."""
        if not messagebox.askyesno("Confirm", "Close Whisper STT server and its console?"):
            return
        threading.Thread(target=self._graceful_shutdown_service, args=(8787, "Whisper STT"), daemon=True).start()

    def _shutdown_all_services(self):
        """Gracefully shut down AllTalk, Whisper, and their consoles, then quit mic panel."""
        if not messagebox.askyesno("Confirm", "Close ALL voice services (AllTalk + Whisper + Mic Panel)?"):
            return

        def shutdown_all():
            self._graceful_shutdown_service(7851, "AllTalk TTS")
            self._graceful_shutdown_service(8787, "Whisper STT")
            logger.info("All services shut down — closing mic panel")
            self.root.after(0, self._quit)

        threading.Thread(target=shutdown_all, daemon=True).start()

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------
    def _restart(self):
        """Restart the mic panel with updated code."""
        logger.info("Restarting mic panel...")
        self._level_monitor_stop.set()
        if self.tray_icon:
            self.tray_icon.stop()
        # Launch a new instance of this script, then exit
        subprocess.Popen(
            [sys.executable, __file__],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.root.destroy()

    def _quit(self):
        """Clean shutdown."""
        logger.info("Mic panel shutting down")
        self._level_monitor_stop.set()
        # Close embedded terminal emulators (shuts down their WinPTY processes)
        for term in (self._whisper_term, self._alltalk_term):
            try:
                term.destroy()
            except Exception:
                pass
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()

    def run(self):
        """Start the tkinter main loop."""
        logger.info("Mic Control Panel starting")
        self.root.mainloop()


if __name__ == "__main__":
    panel = MicControlPanel()
    # Redirect tkinter callback errors to log (pythonw.exe has no stderr)
    panel.root.report_callback_exception = lambda exc_type, exc_value, exc_tb: (
        logger.error("Tkinter callback error:\n"
                     + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    )
    panel.run()
