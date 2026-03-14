"""HueSignalApp - owns the full application lifecycle.

Startup sequence
----------------
0.  Single-instance guard (named mutex)
1.  Load and validate config
2.  Locate mkcert CA certificate
3.  Resolve zone ID (cached in config.ini after first run)
4.  Resolve light IDs for the zone
5.  Fetch initial light colors
6.  Create ColorServer and seed initial colors
7.  SignalRGB setup (background thread - non-blocking)
8.  Create tray icon object (if enabled)
9.  Start Hue SSE stream thread
10. Start Windows power monitor thread
11. Start bridge monitor thread
12. Start Flask/WSS server thread
13. Start tray icon thread (if enabled); main thread waits on shutdown event
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import threading
from pathlib import Path

from .config import AppConfig, ConfigError, setup_logging
from .color import Color, rgb_preview
from .hue import (
    HueStreamThread,
    fetch_initial_colors,
    resolve_light_ids,
    resolve_zone_id,
)
from .watchdog import BridgeMonitor
from .power import PowerMonitor, make_wake_handler
from .server import ColorServer
from .signalrgb import setup_signalrgb
from .tray import TrayIcon, STATUS_MAP, StreamStatus

logger = logging.getLogger("huesignal")


class StartupError(Exception):
    """Raised (and displayed to the user) when a fatal startup step fails."""


_ERROR_ALREADY_EXISTS = 183


class HueSignalApp:
    def __init__(self) -> None:
        self._cfg: AppConfig | None = None
        self._server: ColorServer | None = None
        self._tray: TrayIcon | None = None
        self._stream: HueStreamThread | None = None
        self._monitor: BridgeMonitor | None = None
        self._stream_interrupt = threading.Event()
        self._shutdown_event = threading.Event()
        self._paused = False
        self._instance_mutex = None  # held for process lifetime to enforce single instance

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Load config, start all subsystems, then hand control to the tray icon."""
        try:
            self._startup()
        except StartupError as exc:
            _fatal(str(exc))

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _startup(self) -> None:
        # 0. Single-instance guard
        self._instance_mutex = ctypes.windll.kernel32.CreateMutexW(
            None, True, "HueSignal_SingleInstance"
        )
        if ctypes.windll.kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            raise StartupError(
                "HueSignal is already running.\n\n"
                "Check the system tray or Task Manager for the existing instance."
            )

        # 1. Config
        cfg = self._load_config()
        setup_logging(cfg)
        logger.info("=" * 60)
        logger.info("HueSignal starting up")
        logger.info("=" * 60)

        # 2. mkcert CA
        mkcert_ca = self._find_mkcert_ca()

        # 3. Zone
        cfg = self._resolve_zone(cfg)

        # 4. Lights
        cfg = self._resolve_lights(cfg)

        # 5. Initial colors
        initial_colors = self._fetch_initial_colors(cfg)

        # 6. Server
        server = ColorServer(cfg)
        server.push_colors(initial_colors)
        self._server = server

        # 7. SignalRGB - runs in background so the optional post-restart sleep
        # (up to 6 s) doesn't block the tray icon and server from starting.
        def _do_signalrgb_setup() -> None:
            try:
                setup_signalrgb(mkcert_ca)
            except Exception as exc:
                logger.warning("[signalrgb] Setup failed (non-fatal): %s", exc)

        threading.Thread(
            target=_do_signalrgb_setup, name="signalrgb-setup", daemon=True
        ).start()

        # 8. Tray icon (optional - controlled by [general] tray_icon in config.ini)
        if cfg.tray_icon:
            tray = TrayIcon(
                on_restart_stream=self._restart_stream,
                on_exit=self._on_exit,
                get_latest_colors=lambda: server.latest_colors,
                on_resume=self._reseed_colors,
            )
            self._tray = tray

        # 9. Hue SSE stream thread
        stream = HueStreamThread(
            cfg=cfg,
            on_colors=self._on_colors,
            interrupt=self._stream_interrupt,
            on_status=self._on_stream_status,
            on_reseed=self._reseed_colors,
        )
        stream.start()
        self._stream = stream

        # 10. Power monitor thread
        wake_handler = make_wake_handler(
            cfg=cfg,
            stream_interrupt=self._stream_interrupt,
            on_colors=self._on_colors,
            fetch_colors=fetch_initial_colors,
        )
        PowerMonitor(on_wake=wake_handler).start()

        # 11. Bridge monitor - periodic ping for toast notifications and tray status
        monitor = BridgeMonitor(
            cfg=cfg,
            on_lost=self._on_bridge_lost,
            on_restored=self._on_bridge_restored,
        )
        monitor.start()
        self._monitor = monitor

        # 12. Flask on a background thread - frees main thread for pystray
        flask_thread = threading.Thread(
            target=server.run,
            name="flask",
            daemon=True,
        )
        flask_thread.start()

        logger.info("All subsystems started.")

        # 13. Tray icon on a daemon thread (only if enabled in config)
        if cfg.tray_icon:
            tray_thread = threading.Thread(
                target=self._tray.run, name="tray", daemon=True
            )
            tray_thread.start()
        else:
            logger.info(
                "[app] Tray icon disabled - running headless. Use Ctrl+C to stop."
            )

        # 13. Main thread blocks here - interruptible by Ctrl+C or by
        # _shutdown_event.set() from the tray Exit menu item.
        try:
            self._shutdown_event.wait()
        except KeyboardInterrupt:
            logger.info("[app] Interrupted.")
        finally:
            logger.info("[app] Shutting down.")
            if self._tray:
                self._tray.stop()

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_colors(self, colors: list[Color]) -> None:
        """Receive new colors from the Hue stream and push to server."""
        if self._tray and self._tray.is_paused:
            logger.debug("[app] Push suppressed - sync paused.")
            return
        if self._tray and self._tray.current_status == StreamStatus.RECONNECTING:
            self._tray.set_status(StreamStatus.CONNECTED)
        if self._server:
            self._server.push_colors(colors)

    def _on_stream_status(self, status_str: str) -> None:
        """Bridge the string status tokens from HueStreamThread to TrayIcon."""
        if self._tray is None:
            return
        status = STATUS_MAP.get(status_str)
        if status:
            self._tray.set_status(status)

    def _on_bridge_lost(self) -> None:
        """Called by BridgeMonitor when bridge becomes unreachable."""
        if self._tray is not None:
            self._tray.set_status(StreamStatus.RECONNECTING)

    def _on_bridge_restored(self) -> None:
        """Called by BridgeMonitor when bridge becomes reachable again."""
        if self._stream is not None:
            self._stream.interrupt()
        threading.Thread(target=self._reseed_colors, daemon=True).start()

    def _reseed_colors(self) -> None:
        """Fetch current light state from bridge and push to all clients."""
        if self._cfg is None or self._server is None:
            return
        try:
            colors = fetch_initial_colors(self._cfg)
            self._server.push_colors(colors)
            logger.info("[app] Colors re-seeded.")
        except Exception as exc:
            logger.warning("[app] Could not re-seed colors: %s", exc)

    def _restart_stream(self) -> None:
        """Interrupt the SSE stream and immediately re-seed clients with current light state."""
        logger.info("[app] Stream restart requested.")
        if self._tray is not None:
            self._tray.set_status(StreamStatus.CONNECTING)
        if self._stream is not None:
            self._stream.interrupt()
        if self._cfg is not None and self._server is not None:
            try:
                colors = fetch_initial_colors(self._cfg)
                self._server.push_colors(colors)
                logger.info("[app] Colors re-seeded after restart.")
                if self._tray is not None:
                    self._tray.set_status(StreamStatus.CONNECTED)
            except Exception as exc:
                logger.warning("[app] Could not re-seed colors after restart: %s", exc)
                if self._tray is not None:
                    self._tray.set_status(StreamStatus.RECONNECTING)

    def _shutdown(self) -> None:
        """Set the shutdown event, unblocking the main thread's Event.wait()."""
        self._shutdown_event.set()

    def _on_exit(self) -> None:
        """Called when the user clicks Exit in the tray menu."""
        self._shutdown()

    # ------------------------------------------------------------------
    # Startup helpers
    # ------------------------------------------------------------------

    def _load_config(self) -> AppConfig:
        try:
            cfg = AppConfig.load()
            self._cfg = cfg
            return cfg
        except ConfigError as exc:
            raise StartupError(str(exc)) from exc

    def _find_mkcert_ca(self) -> Path:
        try:
            caroot = Path(
                subprocess.check_output(
                    ["mkcert", "-CAROOT"],
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                ).strip()
            )
            ca_cert = caroot / "rootCA.pem"
            if not ca_cert.exists():
                raise FileNotFoundError(f"rootCA.pem not found in {caroot}")
            return ca_cert
        except FileNotFoundError as exc:
            raise StartupError(
                "mkcert is not installed or not on PATH.\n\n"
                "Install it from https://github.com/FiloSottile/mkcert and run:\n"
                "  mkcert -install\n"
                "  mkcert 127.0.0.1 localhost"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise StartupError(f"mkcert -CAROOT failed: {exc}") from exc

    def _resolve_zone(self, cfg: AppConfig) -> AppConfig:
        if cfg.entertainment_id:
            logger.info("[hue] Using cached zone ID: %s", cfg.entertainment_id)
            return cfg
        logger.info("[hue] Resolving zone ID for '%s' ...", cfg.entertainment_zone_name)
        try:
            cfg.entertainment_id = resolve_zone_id(cfg)
            cfg.save_entertainment_id()
            logger.info("[hue] Zone ID: %s (saved to config)", cfg.entertainment_id)
            return cfg
        except Exception as exc:
            raise StartupError(
                f"Could not find entertainment zone '{cfg.entertainment_zone_name}'.\n\n{exc}\n\n"
                f"Check that bridge_ip ({cfg.bridge_ip}) and application_key are correct "
                "and that the Hue bridge is reachable."
            ) from exc

    def _resolve_lights(self, cfg: AppConfig) -> AppConfig:
        logger.info("[hue] Resolving light IDs for zone %s ...", cfg.entertainment_id)
        try:
            cfg.resolved_light_ids = resolve_light_ids(cfg)
            if not cfg.resolved_light_ids:
                raise ValueError("No lights found in zone.")
            logger.info(
                "[hue] Watching %d light(s): %s",
                len(cfg.resolved_light_ids),
                cfg.resolved_light_ids,
            )
            return cfg
        except Exception as exc:
            raise StartupError(
                f"Could not resolve lights in zone '{cfg.entertainment_zone_name}'.\n\n{exc}"
            ) from exc

    def _fetch_initial_colors(self, cfg: AppConfig) -> list[Color]:
        logger.info("[hue] Fetching initial light state ...")
        try:
            colors = fetch_initial_colors(cfg)
            logger.info("[hue] Initial colors: %s", rgb_preview(colors))
            return colors
        except Exception as exc:
            logger.warning(
                "[hue] Could not fetch initial colors (starting with black): %s", exc
            )
            return [{"r": 0, "g": 0, "b": 0}]


# ------------------------------------------------------------------
# Fatal error helper
# ------------------------------------------------------------------


def _fatal(message: str) -> None:
    """Log, show a native Win32 error dialog, and exit."""
    logger.critical("FATAL: %s", message)
    ctypes.windll.user32.MessageBoxW(
        0,
        message,
        "HueSignal - Fatal Error",
        0x10 | 0x10000,  # MB_ICONERROR | MB_SETFOREGROUND
    )
    raise SystemExit(1)
