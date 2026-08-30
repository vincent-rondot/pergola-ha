# Pergola

A Home Assistant custom integration to control a BLE-connected motorized pergola/awning.

Exposes:
- A **cover** entity (open / close / stop the retractable roof)
- A **light** entity (on / off)
- A **Wind protection** switch (always available, on by default)
- A **Winter sun mode** switch (opt-in, off by default)

Connects over Bluetooth Low Energy using [`pergola_controller`](https://github.com/vincent-rondot/pergola_controller). The integration opens a fresh BLE connection for the duration of each command and disconnects immediately after — the device only accepts one BLE connection at a time, so staying connected would lock out other apps controlling the same device.

## Important hardware quirk: there is no real Stop

Confirmed empirically on the real device: the dedicated stop command (`cmd_active`/`'B'`, what `pergola_controller`'s `stop()` sends) does **not** interrupt motion at all - pressing the cover's Stop button, or letting a timed pulse fall through to `stop()`, does nothing, and the pergola keeps running to its physical end-stop regardless. This bit winter sun mode hard early on: pulses meant to nudge it a few percent instead ran the cover fully closed the first time, since the "stop" they ended with never actually stopped anything.

What actually halts motion - confirmed by the same real-world testing, and matching how the vendor app's own buttons behave - is **re-sending whichever direction is already moving** (Open interrupts an Open, Close interrupts a Close; a toggle, not press-to-move/press-to-stop). Both `PergolaCover.async_stop_cover()` and winter sun mode's internal pulse mechanism now do exactly that instead of calling `stop()`. The cover entity remembers the last direction it sent for this purpose; if Stop is pressed with no known prior direction (e.g. right after a HA restart, before any Open/Close has been sent), it falls back to the documented `stop()` command with a logged warning - which is not confirmed to do anything, so don't rely on Stop working correctly in that specific situation.

## Wind protection and winter sun mode

The pergola only exposes `Open`/`Close`/`Stop` — there is no real position feedback from the hardware. Both features below work around that, and both need a **weather entity** configured first (Settings → Devices & Services → Pergola → Configure). If your Home Assistant location is set, you likely already have one for free: HA auto-configures a Met.no entity (e.g. `weather.forecast_maison`) during onboarding — check Developer Tools → States for anything starting with `weather.`. Met.no is a regional forecast, not a local ground-truth sensor, so it's a reasonable proxy for rain but a coarser one for wind (local gusts on your specific terrace won't necessarily show up in it). A physical ESPHome anemometer, e.g. sharing the ESP32 already planned for the `bluetooth_proxy` role, would be the more accurate long-term fix if wind protection ends up triggering on the wrong days.

Both features refuse to turn on until a weather entity is configured (`HomeAssistantError`, shown in the UI).

### Wind protection (`switch.pergola_wind_protection`)

The device shipped with its own wind sensor, but its stock firmware behavior on wind is to **open** — wrong for this pergola's slat geometry, where an open pergola vibrates in the wind. That sensor has been physically disconnected. This switch replaces it: every 15 minutes, and immediately on any change to the configured weather entity, it checks the entity's `wind_speed` attribute against the **Wind speed threshold** number entity (in km/h — whatever unit your weather entity actually reports in is converted automatically) and closes the cover if it's at or above the threshold.

It only ever closes, never opens, so it's safe to leave on indefinitely (the default) — if wind is still high after a manual reopen, it just closes again on the next check. Independent of winter sun mode; it protects the pergola year-round.

**Diagnostics**: three entities -
- `sensor.pergola_wind_speed` - last reading, converted to km/h regardless of what unit the weather entity reports - the same unit as **Wind speed threshold**, on purpose, so the two are always directly comparable.
- `sensor.pergola_wind_protection_last_triggered` - timestamp of the last wind-triggered close. Updates on *every* 15-minute check for as long as it stays windy (each of those re-closes is a harmless no-op on an already-closed cover, but the timestamp still bumps) - good for "exactly when did this last happen", not for triggering a notification without spamming it.
- `binary_sensor.pergola_wind_protection_active` - on for the whole duration of a windy episode, but its *state* only changes once at the start and once at the end of that episode (writing the same value repeatedly doesn't re-fire a state change in HA) - this is the one to automate on for a single notification per episode, plus you get a free "it calmed down" notification on the off transition too.

**Notifications**: the integration itself never sends a push notification or creates a persistent notification — that's a decision for your own automations, not something to hardcode into the integration. Trigger on `binary_sensor.pergola_wind_protection_active` turning on. Example automation to get a phone notification:

```yaml
automation:
  - alias: "Pergola closed for wind"
    trigger:
      - platform: state
        entity_id: binary_sensor.pergola_wind_protection_active
        to: "on"
    action:
      - service: notify.mobile_app_your_phone  # replace with your actual notify target
        data:
          message: >
            Pergola closed for wind: {{ states('sensor.pergola_wind_speed') }} km/h
            (threshold {{ states('number.pergola_wind_speed_threshold') }} km/h)
```

**Simulating high wind for testing**, two options:
- Easiest: temporarily lower the **Wind speed threshold** number entity (e.g. to 0) so whatever the real current wind reading is counts as "high" — no need to touch the weather entity at all, works with real live data, and the effect is immediate on the next check (or toggle the switch off/on to force an immediate check).
- Or fake the weather entity itself: Developer Tools → States (enable Advanced Mode in your user profile first if it's not visible) → set the weather entity's `wind_speed` attribute directly. This gets overwritten the next time the real integration polls (Met.no updates roughly hourly), so it's only a temporary override.

### Winter sun mode (`switch.pergola_winter_sun_mode`)

For winter days: keeps the slats open for maximum sunlight, nudging them closed a little through the day to track the sun as it sweeps west (this terrace faces close to due south, slats perpendicular to the wall, closing towards the west).

**It never opens the cover itself, under any circumstance.** You open the pergola by hand — HA, the vendor app, or the physical remote, it doesn't matter which, and winter sun mode doesn't strictly need to see it happen to still be useful. If HA *does* see an Open through its own cover buttons, that's a precise, trusted "we know it's 100% open right now" reset of the day's tracking baseline, and the very next check catches up immediately to wherever the sun already is. If it doesn't (e.g. the physical remote, which this integration has no way to observe at all), the first check of each day - whenever that happens to be - does **not** guess or pulse at all: it silently records today's current target as the starting reference point and waits. From the *next* check onward, it only ever pulses by how much the sun has moved relative to that reference - so even though the absolute position was never actually confirmed, the pergola still gets nudged along correctly for the rest of the day, without ever risking a wrong-sized correction based on an unverified guess.

From there, at every mark on the clock implied by the **Winter sun check interval** select entity (5/10/15/30 min or 1 h - e.g. 15 min lands on :00/:15/:30/:45, not just every 15 minutes from whenever you flipped the switch) it estimates the sun's position (via HA's built-in `sun.sun` entity) and, if the ideal position has moved meaningfully since the *last* check, sends a timed `Close` pulse to catch up — sized using the **Full travel time** number entity, defaulted to 67s (1 min 7 s, measured with a stopwatch on the real hardware) - re-time and adjust it if your unit differs. Sub-threshold moves are skipped without losing track of them - a few small moves in a row still add up into one correctly-sized catch-up pulse against the original baseline, rather than each being forgotten individually. It pauses (does nothing) while the weather entity reports rain or wind at/above the configured threshold, and resumes automatically once conditions clear, without needing to be turned off and back on.

**Crucially, pulse size is always computed from how much the *target* has moved since the last check, never from any absolute "how open is it now" estimate.** This matters a lot for a device with zero position feedback: if winter sun mode had been off for hours or days, or the physical remote was used in the meantime, any absolute estimate of "how open it currently is" would be pure guesswork - and a pulse sized from a wrong absolute guess could be wrong by an arbitrary, unbounded amount, in a way that isn't actually harmless (over-closing a pergola that was already correctly positioned is a real malfunction, not a no-op). That's exactly why the very first check of a day never pulses at all when there's no confirmed baseline - only relative movement from a known point ever gets acted on, so the worst case is always bounded by how far the sun genuinely moved, never by how wrong some guess might have been.

Why it never closes-and-self-disables on rain the way an earlier design of this feature did: the pergola already has its own rain sensor built in, which closes it automatically at the *onset* of rain but deliberately does not reclose it if you manually reopen while it's still raining — which might be exactly what you wanted (e.g. rinsing the terrace). This integration doesn't try to guess whether a reopen during rain was deliberate; it just never fights it, by never actively closing for rain at all. Its only rain-related behavior is refusing to open — which it was never going to do anyway.

Because it only ever issues `Close` commands, and pulse sizing never depends on any stored position estimate (see above), there's no scenario where stale state anywhere in this integration can cause an unwanted reopen - or an oversized close, for that matter.

`TRACK_START_AZIMUTH`/`TRACK_END_AZIMUTH`/`MIN_POSITION` (the sun-tracking curve itself) are constants at the top of `sun_tracker.py`, not UI-exposed entities — hand-tune them there if tracking starts/stops earlier or later than the sun actually clears/leaves the terrace, or if it's closing further than you'd like in the afternoon.

**Diagnostics**: `number.pergola_estimated_open_percentage` (0-100%) is a best-guess of the physical position - editable, not just a readout. It is emphatically **not** a real position reading — there is no feedback from the hardware at all. It animates towards its target over roughly the real travel time (per **Full travel time**) rather than snapping instantly, so it visually tracks what the motor is actually doing - heads towards 100% on any Open and 0% on any Close issued through HA (the cover's own buttons, or wind protection), and freezes wherever it's gotten to on Stop, rather than continuing on towards the Open/Close's original target. That means you can do a hand-timed manual pulse - click Close, wait a few seconds, click Stop - the same way winter sun mode does it internally, and the estimate will land roughly where you actually stopped. It still drifts silently out of sync with reality if the cover is moved through something HA doesn't see at all, i.e. the physical remote or the vendor app directly - **when that happens, just set this number to whatever you see for real** (e.g. "it's roughly 60% open") to fix the display. No BLE command is sent when you do this; it only corrects what's *shown*, nothing physical happens.

Setting it deliberately does **not** change what winter sun mode does next. Only a real Open or Close sent through HA - where the motor genuinely ran to its physical end-stop, a hard fact - gets to influence pulse sizing; a hand estimate typed in after eyeballing the pergola is not that kind of fact, even when it's your best guess, so it's kept completely out of the decision-making math on purpose. That separation is exactly what makes the pulse math itself immune to estimates being wrong - letting a rough correction feed back into it would undo that. Don't build automations against it as a trigger; it exists for visibility and manual correction, not as a source of truth to react to.

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
