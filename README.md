# HueSignal

A lightweight bridge that mirrors your Philips Hue lighting effects into SignalRGB in real time. Whatever color or gradient your Hue lights are showing, your SignalRGB setup will follow.

---

## How it works

HueSignal connects to your Hue bridge's event stream and listens for light changes in a configured entertainment zone. When colors change, it converts them from Hue's CIE xy color space to RGB and pushes them over a local WebSocket to a SignalRGB effect (an HTML canvas file). SignalRGB renders the colors as a gradient across your devices.

---

## Requirements

- Windows 10+
- Python 3.10+
- A Philips Hue Bridge (v2) with at least one entertainment zone configured
- [SignalRGB](https://signalrgb.com/) installed and running
- [mkcert](https://github.com/FiloSottile/mkcert) — for generating a trusted local SSL certificate

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Generate a local SSL certificate

SignalRGB requires HTTPS, so you'll need a locally-trusted certificate:

```bash
mkcert 127.0.0.1 localhost
```

This produces `localhost+1.pem` and `localhost+1-key.pem`. Place both in the `certs/` folder inside the project directory.

### 3. Create a `config.ini`

Copy `config.ini.example` to `config.ini` and fill in your details:

```ini
[general]
logging = false
tray_icon = true

[hue]
bridge_ip = 192.168.x.x
application_key = your-hue-app-key
entertainment_zone_name = Your Zone Name
entertainment_id =
```

You can leave `entertainment_id` blank — it will be resolved and cached automatically on first run.

To get your `application_key`, follow [Philips Hue's API getting started guide](https://developers.meethue.com/develop/get-started-2/).

### 4. Run it
```bash
pythonw -m huesignal
```

For normal use, `pythonw` runs HueSignal without a console window. If you want a console for troubleshooting:
```bash
python -m huesignal
```

To disable the system tray icon entirely, set `tray_icon = false` in `config.ini`. Press Ctrl+C in the console to stop.

On first run HueSignal will:

- Resolve your entertainment zone and light IDs (cached in `config.ini` for subsequent runs)
- Patch SignalRGB's `cacert.pem` to trust your local certificate — requires SignalRGB to already be running
- Write the `HueSignal.html` effect file and symlink it into SignalRGB's effects folder

### 5. Load the effect in SignalRGB

Open SignalRGB, go to **Library**, and load **Hue Signal**. Done.

---

## System tray

When running with `tray_icon = true`, HueSignal appears in the system tray with a status dot:

| Colour | Meaning |
|--------|---------|
| Grey   | Starting |
| Amber  | Connecting to bridge |
| Green  | Connected, stream live |
| Red    | Reconnecting |

Right-clicking the tray icon gives access to:

- **Color preview** — shows the current RGB values for each watched light
- **Settings** — toggle file logging and the tray icon on/off (tray icon change requires restart)
- **Restart stream** — manually reconnect to the Hue bridge
- **Open log** — opens `logs/huesignal.log` if logging is enabled
- **Exit**

---

## Notes

- The server runs at `wss://127.0.0.1:5123/ws` — everything stays local, nothing goes to the cloud.
- HueSignal handles Windows sleep/wake events and reconnects the stream automatically after resume.
- If SignalRGB isn't running when you start HueSignal, the cacert patch is skipped — restart HueSignal once SignalRGB is open.
- Gradient lights (such as the Hue Play gradient lightstrip) are fully supported and display as a multi-stop gradient in SignalRGB.
- Only a single entertainment zone is supported by design. Mixing zones produces unpredictable gradient colors.
