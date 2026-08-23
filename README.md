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
