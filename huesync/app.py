"""HueSyncApp — owns the full application lifecycle.

Startup sequence
----------------
1. Load and validate config
2. Resolve zone ID (cached in config.ini after first run)
3. Resolve light IDs for the zone
4. Fetch initial light colors
5. Set up SignalRGB (write HTML, symlink, patch cacert)
6. Start Hue SSE stream thread
7. Start Windows power monitor thread
8. Start Flask/WSS server (blocks main thread)
"""

from __future__ import annotations

import logging
import subprocess
import threading
import tkinter as tk
import tkinter.messagebox as tkmb
from pathlib import Path

from .config import AppConfig, ConfigError, setup_logging
from .color import Color, rgb_preview
from .hue import (
    HueStreamThread,
    fetch_initial_colors,
    resolve_light_ids,
    resolve_zone_id,
)
from .power import PowerMonitor, make_wake_handler
from .server import ColorServer
from .signalrgb import setup_signalrgb

logger = logging.getLogger("huesync")


class StartupError(Exception):
    """Raised (and displayed to the user) when a fatal startup step fails."""


class HueSyncApp:
    def __init__(self) -> None:
        self._cfg: AppConfig | None = None
        self._server: ColorServer | None = None
        self._stream_interrupt = threading.Event()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Load config and start all subsystems. Calls sys.exit on fatal error."""
        try:
            self._startup()
        except StartupError as exc:
            _fatal(str(exc))

    # ------------------------------------------------------------------
    # Startup steps
    # ------------------------------------------------------------------

    def _startup(self) -> None:
        # 1. Config
        cfg = self._load_config()
        setup_logging(cfg)
        logger.info("=" * 60)
        logger.info("HueSync starting up")
        logger.info("=" * 60)

        # 2. mkcert CA path (needed for SignalRGB patching)
        mkcert_ca = self._find_mkcert_ca()

        # 3. Zone resolution
        cfg = self._resolve_zone(cfg)

        # 4. Light IDs
        cfg = self._resolve_lights(cfg)

        # 5. Initial colors
        initial_colors = self._fetch_initial_colors(cfg)

        # 6. Server
        server = ColorServer(cfg)
        server.push_colors(initial_colors)
        self._server = server

        # 7. SignalRGB
        try:
            setup_signalrgb(mkcert_ca)
        except Exception as exc:
            # Non-fatal: log and continue; SignalRGB may not be running
            logger.warning("[signalrgb] Setup failed (non-fatal): %s", exc)

        # 8. Hue SSE stream thread
        stream = HueStreamThread(
            cfg=cfg,
            on_colors=server.push_colors,
            interrupt=self._stream_interrupt,
        )
        stream.start()

        # 9. Power monitor thread
        wake_handler = make_wake_handler(
            cfg=cfg,
            stream_interrupt=self._stream_interrupt,
            on_colors=server.push_colors,
            fetch_colors=fetch_initial_colors,
        )
        power = PowerMonitor(on_wake=wake_handler)
        power.start()

        logger.info("All subsystems started. Running.")

        # 10. Flask server — blocks until process exits
        server.run()

    # ------------------------------------------------------------------
    # Individual startup steps (each raises StartupError on failure)
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
            # Non-fatal: start with black; the stream will correct it
            logger.warning(
                "[hue] Could not fetch initial colors (starting with black): %s", exc
            )
            return [{"r": 0, "g": 0, "b": 0}]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _fatal(message: str) -> None:
    """Log a fatal error, show a GUI dialog, and exit."""
    logger.critical("FATAL: %s", message)
    try:
        root = tk.Tk()
        root.withdraw()
        tkmb.showerror("HueSync — Fatal Error", message)
        root.destroy()
    except Exception:
        pass  # If tkinter isn't available, the log entry is sufficient
    raise SystemExit(1)
