# litra-mqtt

Control a USB-connected **Logitech Litra Glow** (or Litra Beam) from Home Assistant, without
G HUB or Logi Options+.

A small Python bridge runs on the computer the light is plugged into. It talks to the light
over USB and announces it to Home Assistant through MQTT discovery, where it shows up as a
regular light with on/off, brightness and colour temperature. One file, two dependencies.

## Why this exists

The Litra Glow has no Bluetooth or Wi-Fi, only USB, so Home Assistant cannot reach it directly:
it hangs off your desk computer, not the HA server. The existing tools each cover a different
case:

- [litra-rs](https://github.com/timrogers/litra-rs) is an excellent CLI for the light, but has
  no Home Assistant integration.
- The Home Assistant projects for Litra lights talk to the Beam or Beam LX over Bluetooth, or
  need the light plugged into the HA host itself.
- Desk-monitoring agents that happen to support the Litra bring a hundred other sensors along.

litra-mqtt is the missing small piece: USB on one side, MQTT discovery on the other.

## Features

- `light` entity with on/off, brightness and colour temperature (2700–6500 K)
- State stays in sync: the bridge reads the light back after every command and polls it every
  30 seconds, so changes made with the buttons on the light show up too
- Shows as *unavailable* when the computer is off, the light is unplugged or the bridge stops
- Recovers by itself after USB replugging, sleep and broker restarts
- Runs headless; on Windows without a console window

## Supported lights

| Light | USB ID | Status |
|---|---|---|
| Litra Glow | `046d:c900` | tested |
| Litra Beam | `046d:c901` | same protocol, untested |
| Litra Beam LX | `046d:c903` | not supported (different protocol) |

USB only. One light per bridge: if several are connected, the first one found is used.

## Requirements

- Home Assistant 2025.10 or newer, with the MQTT integration and a broker such as the
  Mosquitto add-on
- On the computer with the light: Python 3.9 or newer
- Tested on Windows 11 with Python 3.11. Linux and macOS should work but are untested.

## Setup

### 1. Create an MQTT user

The bridge needs its own login on your MQTT broker. With the Mosquitto add-on, open its
configuration, switch to YAML editing and add a login:

```yaml
logins:
  - username: litra
    password: <a long random password>
```

Save, then **restart the add-on**: new logins only take effect after a restart. (The Mosquitto
add-on also accepts regular Home Assistant users, so creating an HA user works as well.)

### 2. Install the bridge

Windows (PowerShell):

```powershell
git clone https://github.com/axsb/litra-mqtt.git
cd litra-mqtt
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy config.example.json config.json
```

Linux / macOS:

```bash
git clone https://github.com/axsb/litra-mqtt.git
cd litra-mqtt
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

On Linux, allow your user to access the light without root (rule taken from litra-rs), then
log out and back in:

```bash
echo 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="046d", ATTRS{idProduct}=="c90[01]", GROUP="video", MODE="0660"' \
  | sudo tee /etc/udev/rules.d/99-litra.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG video "$USER"
```

### 3. Configure

Edit `config.json`:

| Key | Default | Meaning |
|---|---|---|
| `host` | – | MQTT broker, e.g. the IP of your Home Assistant |
| `port` | `1883` | MQTT port |
| `username`, `password` | none | the login from step 1 |
| `poll_seconds` | `30` | how often the light is read back |
| `discovery_prefix` | `homeassistant` | only change if you changed it in Home Assistant |

### 4. First run

```powershell
.venv\Scripts\python litra_mqtt.py
```

(`.venv/bin/python litra_mqtt.py` on Linux / macOS.) The log should read:

```
INFO Starting, broker 192.168.1.10:1883
INFO Found Litra Glow (serial XXXXXXXXXXXX)
INFO MQTT connected
INFO State: ON, 250 lm, 4500 K
```

In Home Assistant, go to *Settings → Devices & services → MQTT*. A device **Litra Glow** with
the entity `light.litra_glow` appears. Stop the bridge with Ctrl+C.

### 5. Start automatically

**Windows:** a scheduled task starts the bridge at logon with `pythonw.exe`, so no console
window appears. It runs in your user session and needs no admin rights. A second trigger
checks every 5 minutes and restarts the bridge if it is not running; while it runs, the
check does nothing. On a laptop that mostly sleeps instead of shutting down, logon alone is
not enough. In PowerShell, from the repository folder:

```powershell
$dir = (Resolve-Path .).Path; $user = "$env:USERDOMAIN\$env:USERNAME"
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user; $logon.Delay = "PT15S"
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "litra-mqtt" -Force -Trigger @($logon, $watchdog) `
  -Action (New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\pythonw.exe" -Argument "`"$dir\litra_mqtt.py`"" -WorkingDirectory $dir) `
  -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries) `
  -Principal (New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited)
Start-ScheduledTask -TaskName "litra-mqtt"
```

To remove it: `Unregister-ScheduledTask -TaskName litra-mqtt`.

**Linux (untested):** a systemd user service. Save as
`~/.config/systemd/user/litra-mqtt.service`, adjusting the paths:

```ini
[Unit]
Description=Logitech Litra to MQTT bridge
After=network-online.target

[Service]
ExecStart=%h/litra-mqtt/.venv/bin/python %h/litra-mqtt/litra_mqtt.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Then `systemctl --user enable --now litra-mqtt`.

## Using it

The light behaves like any other light in Home Assistant: dashboards, scenes, automations and
voice assistants all work. For example:

```yaml
action: light.turn_on
target:
  entity_id: light.litra_glow
data:
  brightness_pct: 60
  color_temp_kelvin: 3500
```

Home Assistant's brightness range maps linearly onto the light's range (20–250 lm on the
Glow). Colour temperature is rounded to steps of 100 K, which is what the light accepts.

## How it works

The light exposes a vendor-defined HID collection (usage page `0xFF43`). Commands and replies
are 20-byte reports of the form `11 FF 04 <function> <value hi> <value lo>`:

| Function | Get | Set | Change report |
|---|---|---|---|
| Power (`01` = on) | `01` | `1C` | `00` |
| Brightness (lumen) | `31` | `4C` | `10` |
| Colour temperature (kelvin) | `81` | `9C` | `20` |

The light acknowledges set commands without echoing the value, so the bridge reads the state
back after each command. It also understands the unsolicited change reports, but does not rely
on them: the Glow did not send any in testing, so the bridge polls as well.

MQTT topics:

| Topic | Direction | Content |
|---|---|---|
| `homeassistant/light/litra_<serial>/config` | bridge → HA | discovery config (retained) |
| `litra/<serial>/state` | bridge → HA | JSON state (retained) |
| `litra/<serial>/set` | HA → bridge | JSON command |
| `litra/<serial>/availability` | bridge → HA | `online` / `offline`: light present |
| `litra/bridge/<hostname>` | bridge → HA | `online` / `offline`: bridge running (last will) |

## Troubleshooting

The log is `litra-mqtt.log` next to `config.json`.

- **`MQTT connection refused: Not authorized`**: wrong username or password, or the Mosquitto
  add-on was not restarted after adding the login.
- **The entity is unavailable**: the computer is off, the bridge is not running, or the light
  is unplugged. The log tells you which.
- **Changes made with the buttons on the light show up late**: they are picked up by polling,
  after at most `poll_seconds`.
- **No light is found**: check the USB cable; on Linux, the udev rule and group membership.
  The Beam LX is not supported.
- **`Another bridge is already running`**: only one bridge per computer. Bridges on different
  computers do not interfere with each other.
- **`Restarting the MQTT client` after sleep or a network change**: expected. On Windows,
  paho-mqtt can lose the local socket pair it uses internally and never recover by itself,
  so the bridge sends a heartbeat every minute and replaces the client when sending fails or
  the broker stops acknowledging.

## Contributing

Not tested yet, and contributions welcome: Litra Beam, Linux, macOS, and more than one light per bridge.

## Credits

- [timrogers/litra-rs](https://github.com/timrogers/litra-rs) (MIT): reference for the HID
  protocol
- [reznikov/litra2mqtt](https://github.com/reznikov/litra2mqtt): decoding of the unsolicited
  change reports

Not affiliated with Logitech. Logitech, Litra and Litra Glow are trademarks of Logitech.

## License

[MIT](LICENSE)
