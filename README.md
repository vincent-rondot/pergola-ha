# Pergola

A Home Assistant custom integration to control a BLE-connected motorized pergola/awning.

Exposes:
- A **cover** entity (open / close / stop the retractable roof)
- A **light** entity (on / off)

Connects over Bluetooth Low Energy using [`pergola_controller`](https://github.com/vincent-rondot/pergola_controller). The integration opens a fresh BLE connection for the duration of each command and disconnects immediately after — the device only accepts one BLE connection at a time, so staying connected would lock out other apps controlling the same device.

## Installation

### Via HACS (recommended)

1. In HACS, add this repository as a custom repository (category: Integration).
2. Install "Pergola" from HACS.
3. Restart Home Assistant.

### Manual

Copy this folder into `config/custom_components/pergola/` and restart Home Assistant.

## Configuration

Configuration is done via the Home Assistant UI (Settings → Devices & Services → Add Integration → Pergola). You'll need:
- The device's BLE address (discovered automatically via a scan during setup)
- An Android ID (a persistent identifier the device uses to recognize the client)
- The device's PIN

## Requirements

- A Bluetooth adapter accessible to the Home Assistant host
- Network/BLE proximity to the device

## `pergola_controller` is private — GitHub auth for pip

`manifest.json` installs `pergola_controller` from a **private** GitHub repo (`git+https://github.com/vincent-rondot/pergola_controller.git@v0.1.0`). pip/git need a credential to fetch it, provided via a `.netrc` file (a read-only, fine-grained GitHub PAT scoped to just that one repo):

```
machine github.com
login vincent-rondot
password <fine-grained PAT, Contents: Read-only, scoped to pergola_controller>
```

### Where this lives, per environment

- **`rbpi2`** (dev/test Pi): `~/.netrc` in the shell user's home — persists normally, no special care needed.
- **WSL devcontainer**: `/home/vscode/.netrc` inside the devcontainer — persists as long as the container/volume isn't rebuilt from scratch.
- **Production HAOS** (`192.168.1.23`): this is the tricky one. The `homeassistant` Docker container (managed by Supervisor) only persists `/config`, `/media`, `/share`, `/ssl` across Home Assistant Core updates — `/root` (where git actually looks via `$HOME`) gets wiped every time Core updates, since that recreates the container from a fresh image.

  **Fix in place:** a backup copy lives at `/config/.netrc` (persistent). If this integration fails to load after a Core update with an error like `could not read Username for 'https://github.com'`, restore the working copy by running on the HAOS host:
  ```bash
  docker cp homeassistant:/config/.netrc /tmp/n && docker cp /tmp/n homeassistant:/root/.netrc && docker exec homeassistant chmod 600 /root/.netrc
  ```
  This is a known limitation, not automated — there's no supported way to persist custom env vars or files under `/root` across Supervisor-managed container recreation. `__init__.py` logs a reminder of this on every successful load.
