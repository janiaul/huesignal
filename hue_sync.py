import json
import traceback
import threading
import time
import subprocess
import re
import configparser

import urllib3
import requests
from flask import Flask
from flask_sock import Sock
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# CONFIG
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
HUESYNC_HTML = BASE_DIR / "HueSync.html"

target = HUESYNC_HTML
link = Path.home() / "Documents" / "WhirlwindFX" / "Effects" / "HueSync.html"
link.parent.mkdir(parents=True, exist_ok=True)
if not link.exists():
    link.symlink_to(target)

FLASK_PORT = 5123

_config = configparser.ConfigParser()
_config.read(BASE_DIR / "config.ini")

BRIDGE_IP = _config["hue"]["bridge_ip"]
APPLICATION_KEY = _config["hue"]["application_key"]
ENTERTAINMENT_ZONE_NAME = _config["hue"]["entertainment_zone_name"]
ENTERTAINMENT_ID = _config["hue"].get("entertainment_id", "")

# ==========================================
# FLASK / WEBSOCKET
# ==========================================

app = Flask(__name__)
sock = Sock(app)

_connected_clients: set = set()
_clients_lock = threading.Lock()
_latest_colors: list = [{"r": 0, "g": 0, "b": 0}]
_colors_lock = threading.Lock()


@sock.route("/ws")
def ws_handler(ws):
    """Accept a new WebSocket client, send the current color immediately, then keep alive."""
    with _clients_lock:
        _connected_clients.add(ws)
    print(f"  [ws] Client connected ({len(_connected_clients)} total)")
    try:
        with _colors_lock:
            ws.send(json.dumps(_latest_colors, separators=(",", ":")))
        while True:
            ws.receive(timeout=None)
    except Exception:
        pass
    finally:
        with _clients_lock:
            _connected_clients.discard(ws)
        print(f"  [ws] Client disconnected ({len(_connected_clients)} total)")


def broadcast(msg: str) -> None:
    """Send a message to all connected WebSocket clients, dropping any dead connections."""
    with _clients_lock:
        dead = set()
        for ws in _connected_clients:
            try:
                ws.send(msg)
            except Exception:
                dead.add(ws)
        _connected_clients.difference_update(dead)


# ==========================================
# ZONE / LIGHT RESOLUTION
# ==========================================


def resolve_zone_id(bridge_ip, api_key, zone_name):
    """Find the entertainment zone ID matching the configured zone name."""
    url = f"https://{bridge_ip}/clip/v2/resource/entertainment_configuration"
    resp = requests.get(
        url, headers={"hue-application-key": api_key}, verify=False, timeout=5
    )
    resp.raise_for_status()
    for zone in resp.json().get("data", []):
        if zone.get("name", "").lower() == zone_name.lower():
            return zone["id"]
    available = [z.get("name") for z in resp.json().get("data", [])]
    raise ValueError(f"Zone '{zone_name}' not found. Available: {available}")


def resolve_light_ids_in_zone(bridge_ip, api_key, zone_id):
    """Walk the zone's channel/entertainment/device graph to collect all light resource IDs."""
    headers = {"hue-application-key": api_key}
    url = f"https://{bridge_ip}/clip/v2/resource/entertainment_configuration/{zone_id}"
    resp = requests.get(url, headers=headers, verify=False, timeout=5)
    resp.raise_for_status()
    config = resp.json().get("data", [{}])[0]

    entertainment_rids = set()
    for channel in config.get("channels", []):
        for member in channel.get("members", []):
            svc = member.get("service", {})
            if svc.get("rtype") == "entertainment":
                entertainment_rids.add(svc["rid"])

    device_rids = set()
    for ent_rid in entertainment_rids:
        url = f"https://{bridge_ip}/clip/v2/resource/entertainment/{ent_rid}"
        resp = requests.get(url, headers=headers, verify=False, timeout=5)
        resp.raise_for_status()
        owner = resp.json().get("data", [{}])[0].get("owner", {})
        if owner.get("rtype") == "device":
            device_rids.add(owner["rid"])

    light_rids = []
    for device_rid in device_rids:
        url = f"https://{bridge_ip}/clip/v2/resource/device/{device_rid}"
        resp = requests.get(url, headers=headers, verify=False, timeout=5)
        resp.raise_for_status()
        for svc in resp.json().get("data", [{}])[0].get("services", []):
            if svc.get("rtype") == "light":
                light_rids.append(svc["rid"])
    return light_rids


def fetch_initial_colors(bridge_ip, api_key, light_ids):
    """Fetch current color state of each light at startup; returns black if all lights are off."""
    headers = {"hue-application-key": api_key}
    colors = []

    for light_id in light_ids:
        url = f"https://{bridge_ip}/clip/v2/resource/light/{light_id}"
        resp = requests.get(url, headers=headers, verify=False, timeout=5)
        resp.raise_for_status()
        data = resp.json().get("data", [{}])[0]

        if not data.get("on", {}).get("on", False):
            colors.append({"r": 0, "g": 0, "b": 0})
            continue

        bri = data.get("dimming", {}).get("brightness", 100.0) / 100.0

        if "gradient" in data and data["gradient"].get("points"):
            for point in data["gradient"]["points"]:
                xy = point["color"]["xy"]
                r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
                colors.append({"r": r, "g": g, "b": b})
        elif "color" in data and "xy" in data["color"]:
            xy = data["color"]["xy"]
            r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
            colors.append({"r": r, "g": g, "b": b})
        else:
            colors.append({"r": 0, "g": 0, "b": 0})

    return colors if colors else [{"r": 0, "g": 0, "b": 0}]


def fetch_current_colors(bridge_ip, api_key, light_id):
    """Fetch the current color of a single light; used when a toggle-on event carries no color."""
    headers = {"hue-application-key": api_key}
    url = f"https://{bridge_ip}/clip/v2/resource/light/{light_id}"
    resp = requests.get(url, headers=headers, verify=False, timeout=5)
    resp.raise_for_status()
    data = resp.json().get("data", [{}])[0]
    bri = data.get("dimming", {}).get("brightness", 100.0) / 100.0
    if "gradient" in data and data["gradient"].get("points"):
        colors = []
        for point in data["gradient"]["points"]:
            xy = point["color"]["xy"]
            r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
            colors.append({"r": r, "g": g, "b": b})
        return colors
    elif "color" in data and "xy" in data["color"]:
        xy = data["color"]["xy"]
        r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
        return [{"r": r, "g": g, "b": b}]
    return [{"r": 0, "g": 0, "b": 0}]


# ==========================================
# COLOR CONVERSION
# ==========================================


def _clamp(v, lo=0.0, hi=1.0):
    """Clamp a value between lo and hi."""
    return max(lo, min(hi, v))


def _srgb_gamma(linear):
    """Apply sRGB gamma correction to a linear light value."""
    if linear <= 0.0031308:
        return 12.92 * linear
    return 1.055 * (linear ** (1.0 / 2.4)) - 0.055


def xy_bri_to_rgb(x, y, bri=1.0):
    """Convert Hue CIE xy + brightness to an sRGB (r, g, b) tuple in 0-255 range."""
    if y == 0:
        return 0, 0, 0
    Y = bri
    X = (Y / y) * x
    Z = (Y / y) * (1.0 - x - y)
    r_lin = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g_lin = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b_lin = X * 0.051713 - Y * 0.121364 + Z * 1.011530
    min_lin = min(r_lin, g_lin, b_lin)
    if min_lin < 0:
        r_lin -= min_lin
        g_lin -= min_lin
        b_lin -= min_lin
    max_lin = max(r_lin, g_lin, b_lin)
    if max_lin > 1.0:
        r_lin /= max_lin
        g_lin /= max_lin
        b_lin /= max_lin
    r_lin *= bri
    g_lin *= bri
    b_lin *= bri
    return (
        int(_clamp(_srgb_gamma(_clamp(r_lin))) * 255 + 0.5),
        int(_clamp(_srgb_gamma(_clamp(g_lin))) * 255 + 0.5),
        int(_clamp(_srgb_gamma(_clamp(b_lin))) * 255 + 0.5),
    )


# ==========================================
# CLOUDFLARED
# ==========================================


def start_cloudflared(local_port: int, timeout: int = 60) -> str:
    """Kill any existing cloudflared processes, start a fresh tunnel, and return the wss:// URL."""
    subprocess.run(
        ["taskkill", "/f", "/im", "cloudflared.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
    found_url = []

    def read_stderr(pipe):
        for line in pipe:
            match = url_pattern.search(line)
            if match and not found_url:
                found_url.append(line.strip())

    cmd = ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{local_port}"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stderr_thread = threading.Thread(
        target=read_stderr, args=(proc.stderr,), daemon=True
    )
    stderr_thread.start()

    print("Starting cloudflared tunnel ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if found_url:
            match = url_pattern.search(found_url[0])
            https_url = match.group(0)
            wss_url = https_url.replace("https://", "wss://") + "/ws"
            print(f"  -> Tunnel URL: {wss_url}")
            return wss_url
        if proc.poll() is not None:
            raise RuntimeError(
                f"cloudflared exited unexpectedly with code {proc.returncode}"
            )
        time.sleep(0.5)

    proc.terminate()
    raise RuntimeError("Timed out waiting for cloudflared tunnel URL")


# ==========================================
# SIGNALRGB EFFECT RELOAD
# ==========================================


def reload_signalrgb_effect(effect_name: str) -> None:
    """Trigger SignalRGB to reload a named effect via its protocol URL handler."""
    from urllib.parse import quote

    effect_url = quote(effect_name, safe="")
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    subprocess.Popen(
        [
            "cmd",
            "/c",
            f"start /min signalrgb://effect/apply/{effect_url}?-silentlaunch-",
        ],
        shell=True,
        startupinfo=startupinfo,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print(f"  -> Reloading '{effect_name}' in SignalRGB ...")


# ==========================================
# HTML
# ==========================================


def write_html(file_path: Path, wss_url: str) -> None:
    """Write the SignalRGB effect HTML file with the current tunnel URL baked in."""
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hue Sync</title>
  <style>html,body{{margin:0;padding:0;overflow:hidden}}</style>
</head>
<body>
<canvas id="c"></canvas>
<script>
(function () {{
  const canvas = document.getElementById("c");
  const ctx    = canvas.getContext("2d");
  let colors   = [{{r:0,g:0,b:0}}];

  function resize() {{
    canvas.width  = window.innerWidth  || 320;
    canvas.height = window.innerHeight || 200;
    draw();
  }}

  function draw() {{
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    if (colors.length === 1) {{
      ctx.fillStyle = `rgb(${{colors[0].r}},${{colors[0].g}},${{colors[0].b}})`;
      ctx.fillRect(0, 0, W, H);
      return;
    }}
    const grad = ctx.createLinearGradient(0, 0, W, 0);
    colors.forEach((c, i) => {{
      grad.addColorStop(i / (colors.length - 1), `rgb(${{c.r}},${{c.g}},${{c.b}})`);
    }});
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, W, H);
  }}

  function connect() {{
    const ws = new WebSocket("{wss_url}");
    ws.onmessage = (e) => {{
      try {{
        colors = JSON.parse(e.data);
        draw();
      }} catch(_) {{}}
    }};
    ws.onclose = () => setTimeout(connect, 2000);
    ws.onerror = () => ws.close();
  }}

  window.addEventListener("resize", resize);
  resize();
  connect();
}})();
</script>
</body>
</html>
"""
    file_path.write_text(html, encoding="utf-8")


# ==========================================
# HUE EVENT PARSER
# ==========================================


def extract_colors_from_event(data, watched_ids):
    """
    Parse SSE event data into a list of RGB dicts for watched lights.
    Returns black on light-off; fetches current state from bridge on toggle-on with no color data.
    """
    colors = []
    for event in data:
        if event.get("type") != "update":
            continue
        for item in event.get("data", []):
            if item.get("type") != "light":
                continue
            light_id = item.get("id")
            if light_id not in watched_ids:
                continue

            on_state = item.get("on", {})

            # Light turned off — push black
            if "on" in on_state and not on_state["on"]:
                colors.append({"r": 0, "g": 0, "b": 0})
                continue

            bri = item.get("dimming", {}).get("brightness", 100.0) / 100.0

            has_color = False
            if "gradient" in item and item["gradient"].get("points"):
                has_color = True
                for point in item["gradient"]["points"]:
                    xy = point["color"]["xy"]
                    r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
                    colors.append({"r": r, "g": g, "b": b})
            elif "color" in item and "xy" in item["color"]:
                has_color = True
                xy = item["color"]["xy"]
                r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
                colors.append({"r": r, "g": g, "b": b})

            if not has_color:
                if "on" in on_state and on_state["on"]:
                    # Toggle-on with no color — fetch from bridge
                    print(
                        f"  [hue] Toggle-on with no color data, fetching state for {light_id} ..."
                    )
                    colors.extend(
                        fetch_current_colors(BRIDGE_IP, APPLICATION_KEY, light_id)
                    )
                elif "dimming" in item:
                    # Brightness-only event — fetch current colors from bridge at new brightness
                    print(
                        f"  [hue] Brightness change, fetching state for {light_id} ..."
                    )
                    colors.extend(
                        fetch_current_colors(BRIDGE_IP, APPLICATION_KEY, light_id)
                    )

    return colors


# ==========================================
# HUE SSE STREAM
# ==========================================


def hue_stream_thread(bridge_ip, api_key, watched_ids):
    """Listen to the Hue bridge SSE event stream and broadcast color updates to WebSocket clients."""
    global _latest_colors
    url = f"https://{bridge_ip}/eventstream/clip/v2"
    headers = {"hue-application-key": api_key, "Accept": "text/event-stream"}
    backoff = 3

    while True:
        try:
            print(f"Connecting to Hue bridge at {bridge_ip} ...")
            with requests.get(
                url, headers=headers, stream=True, verify=False, timeout=None
            ) as resp:
                resp.raise_for_status()
                backoff = 3
                print("Connected. Listening for Hue events ...")
                buffer = []
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if raw_line.startswith("data:"):
                        buffer.append(raw_line[5:].strip())
                    elif raw_line == "" and buffer:
                        payload = " ".join(buffer)
                        buffer = []
                        try:
                            events = json.loads(payload)
                            colors = extract_colors_from_event(events, watched_ids)
                            if colors:
                                with _colors_lock:
                                    _latest_colors = colors
                                msg = json.dumps(colors, separators=(",", ":"))
                                rgb_preview = ", ".join(
                                    f"rgb({c['r']},{c['g']},{c['b']})"
                                    for c in colors[:4]
                                )
                                print(f"  Push -> {rgb_preview}")
                                broadcast(msg)
                        except json.JSONDecodeError:
                            pass
        except requests.RequestException as exc:
            print(f"Stream error: {exc}")
        except Exception:
            traceback.print_exc()

        print(f"Reconnecting in {backoff}s ...")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ==========================================
# MAIN
# ==========================================


def main():
    """Resolve zone/lights, seed initial color state, start cloudflared and Flask."""
    global ENTERTAINMENT_ID, _latest_colors

    if not ENTERTAINMENT_ID:
        print(f"Resolving zone ID for '{ENTERTAINMENT_ZONE_NAME}' ...")
        ENTERTAINMENT_ID = resolve_zone_id(
            BRIDGE_IP, APPLICATION_KEY, ENTERTAINMENT_ZONE_NAME
        )
        print(f"  -> Zone ID: {ENTERTAINMENT_ID}")

    print("Fetching light IDs in zone ...")
    light_ids = resolve_light_ids_in_zone(BRIDGE_IP, APPLICATION_KEY, ENTERTAINMENT_ID)
    print(f"  -> Watching {len(light_ids)} light(s): {light_ids}")
    watched_ids = set(light_ids)

    print("Fetching initial light state ...")
    with _colors_lock:
        _latest_colors = fetch_initial_colors(BRIDGE_IP, APPLICATION_KEY, light_ids)
    rgb_preview = ", ".join(
        f"rgb({c['r']},{c['g']},{c['b']})" for c in _latest_colors[:4]
    )
    print(f"  -> Initial colors: {rgb_preview}")

    wss_url = start_cloudflared(FLASK_PORT)

    write_html(HUESYNC_HTML, wss_url)
    print(f"Effect file written: {HUESYNC_HTML}")

    print("Waiting for tunnel to be reachable ...")
    time.sleep(5)

    # reload_signalrgb_effect("Hue Sync")

    hue_thread = threading.Thread(
        target=hue_stream_thread,
        args=(BRIDGE_IP, APPLICATION_KEY, watched_ids),
        daemon=True,
    )
    hue_thread.start()

    app.run(host="127.0.0.1", port=FLASK_PORT)


if __name__ == "__main__":
    main()
