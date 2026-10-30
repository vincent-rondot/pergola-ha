# Pergola

A Home Assistant custom integration to control a BLE-connected motorized pergola/awning.

Exposes:
- A **cover** entity (open / close / stop the retractable roof)
- A **light** entity (on / off, with brightness - confirmed working over Bluetooth as of pergola_controller 0.2.1, see `light.py`)
- A **Wind protection** switch (always available, on by default)
- A **Winter sun mode** switch (opt-in, off by default)

Connects over Bluetooth Low Energy using [`pergola_controller`](https://github.com/vincent-rondot/pergola_controller) (0.2.1+). The device only accepts one BLE connection at a time, so staying connected would lock out other apps controlling the same device (the vendor app, its web app) - two connection modes, controlled by the **Stay connected** switch (off by default):
- **Off (default)**: connects briefly every **Status poll interval** (default 5 min) just to pick up a fresh status report, then disconnects - plus on-demand for actual commands, kept open for **Idle timeout** seconds afterwards in case more commands follow right away.
- **On**: stays connected indefinitely, with automatic reconnect if the link drops - continuous live status, but locks out the vendor app/web app for as long as it's on. Turn it back off to let them connect.

`binary_sensor.pergola_connected` shows whether HA currently holds the link.

## Important hardware quirk: there is no real Stop

Confirmed empirically on the real device: the dedicated stop command (`cmd_active`/`'B'`, what `pergola_controller`'s `stop()` sends) does **not** interrupt motion at all - pressing the cover's Stop button, or letting a timed pulse fall through to `stop()`, does nothing, and the pergola keeps running to its physical end-stop regardless. This bit winter sun mode hard early on: pulses meant to nudge it a few percent instead ran the cover fully closed the first time, since the "stop" they ended with never actually stopped anything.

What actually halts motion - confirmed by the same real-world testing, and matching how the vendor app's own buttons behave - is **re-sending whichever direction is already moving** (Open interrupts an Open, Close interrupts a Close; a toggle, not press-to-move/press-to-stop). Both `PergolaCover.async_stop_cover()` and winter sun mode's internal pulse mechanism now do exactly that instead of calling `stop()`. The cover entity remembers the last direction it sent for this purpose; if Stop is pressed with no known prior direction (e.g. right after a HA restart, before any Open/Close has been sent), it falls back to the documented `stop()` command with a logged warning - which is not confirmed to do anything, so don't rely on Stop working correctly in that specific situation.

## Wind protection and winter sun mode

The pergola only exposes `Open`/`Close`/`Stop` — and while it does report real-time state (`opening`/`closing`/`stopped`/`open`/`closed`, see `sensor.pergola_blades_state`) as of pergola_controller 0.2.1, it never reports a percentage or angle. Both features below work around that, and both need a **weather entity** configured first (Settings → Devices & Services → Pergola → Configure). If your Home Assistant location is set, you likely already have one for free: HA auto-configures a Met.no entity (e.g. `weather.forecast_maison`) during onboarding — check Developer Tools → States for anything starting with `weather.`. Met.no is a regional forecast, not a local ground-truth sensor, so it's a reasonable proxy for rain but a coarser one for wind (local gusts on your specific terrace won't necessarily show up in it). A physical ESPHome anemometer, e.g. sharing the ESP32 already planned for the `bluetooth_proxy` role, would be the more accurate long-term fix if wind protection ends up triggering on the wrong days.

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

For winter days: keeps the slats aimed at the sun for maximum light, nudging their angle through the day to follow it, using only timed `Close` pulses - the device's only fine-grained control.

**The sun-tracking curve (since 2026-10-16) follows the slats' real geometry, not a simple clock/azimuth ramp.** This pergola's slats run perpendicular to the wall (each slat's long axis points away from the house, the terrace faces 165°) and pivot together from 0° (closed, flat, overlapping - watertight) up to 140° (fully open) - ordered deliberately to open towards the west, to maximize evening sun. The target angle at any moment is the sun's real **profile angle** (`profile_angle()` in `sun_tracker.py` - the angle the sun appears at within the vertical plane perpendicular to the slats' axis, computed from HA's `sun.sun` azimuth and elevation), which lines the slats up edge-on to the sun for maximum light through the gaps - a flat slat has no front/back, so whenever the raw formula gives a negative (mechanically unreachable) angle, its +180° equivalent is used instead where that still fits in [0°, 140°], since it's the *same* physical plane, not an approximation (2026-10-17 fix - see `profile_angle()`'s docstring for the bug this replaced, where the whole 1-2h stretch before the sun actually crosses the slats' axis incorrectly stayed pinned fully open instead of already closing smoothly). Deep mornings, where even that equivalent falls outside [0°, 140°], genuinely have no useful angle for this one-directional mechanism and stay fully open; tracking starts a couple hours before solar noon, passes through exactly 90°/vertical right as the sun crosses the slats' axis, and continues closing through the afternoon, floored at **`MIN_POSITION`** (default 0.25, i.e. never past 35°/75% closed - real slat thickness means a near-flat slat doesn't pass meaningfully more light than fully closed, however good the idealized angle math looks). `WALL_ORIENTATION`, `MAX_SLAT_ANGLE`, `MIN_POSITION` and `TRACK_END_AZIMUTH` (a safety cap near sunset) are constants at the top of `sun_tracker.py`, not UI-exposed entities - hand-tune them there if your installation's geometry differs.

**Opens itself once a day, at dawn, if the weather looks clear** (since pergola_controller 0.2.1 gave this integration real status feedback, 2026-10-16) - checks rain (from **two** independent sources, see below), wind (`is_windy`), one attempt only: if blocked, it does **not** retry later that day, open by hand instead.

**Two rain sources gate the daily auto-open** (2026-10-21, see `weather_guard.is_rain_forecast()`): the configured weather entity's current condition (`is_raining`, usually Met.no - "is it raining right now") AND, if a **Rain forecast sensor** entity is configured in the integration's options, the `meteofrance` integration's "next rain" nowcast sensor (`is_rain_forecast` - "is rain forecast in roughly the next 30 minutes", minute-level, not the coarser hourly Met.no forecast). Either source alone is enough to skip today's auto-open. Added after an incident (2026-10-18/21) made clear the pergola's own onboard rain sensor only reacts *after* rain has actually started - it protects the terrace once it's raining, but gives zero advance warning, so a dawn check landing moments before rain begins could still open straight onto it. The meteofrance entity isn't auto-discovered like the Met.no one - add the "Météo-France" integration (Settings → Devices & Services → Add Integration, search your commune) and pick its `sensor.*_next_rain` entity in Pergola's own options to enable this second check. Before 0.2.1, and if that daily attempt is ever skipped or fails, you open the pergola by hand instead - HA, the vendor app, or the physical remote, it doesn't matter which. Either way, an Open seen through HA's own cover buttons (whether you clicked it or the daily auto-open did) is a precise, trusted "we know it's 100% open right now" reset of the day's tracking baseline, and the very next check catches up immediately to wherever the sun already is. If HA never sees an Open at all that day (e.g. only the physical remote was used, which this integration has no way to observe), the first check of the day does **not** guess or pulse at all: it silently records today's current target as the starting reference point and waits. From the *next* check onward, it only ever pulses by how much the sun has moved relative to that reference - so even though the absolute position was never actually confirmed, the pergola still gets nudged along correctly for the rest of the day, without ever risking a wrong-sized correction based on an unverified guess.

**Closes itself once a day, at dusk, unconditionally** (2026-10-17) - symmetric to the dawn auto-open above, but never gated on weather (closing is always the safe state). Two bugs here were fixed 2026-10-26: `profile_angle()`'s raw-angle correction flipped sign on an infinitesimal elevation change right around 0° (HA's `sun.sun` can read `above_horizon` for a sliver before/after true geometric sunrise, due to atmospheric refraction), which could snap a dawn pulse straight back down to `MIN_POSITION` moments after the daily auto-open - elevation ≤ 0° is now treated the same as deep morning (no useful angle, stay open) before `profile_angle()` ever runs. Separately, the one-shot auto-close guard used to compare against the calendar date *at the moment of the check* - since night straddles midnight, the very first below-horizon check after midnight (still closing for *yesterday's* dusk) consumed that guard immediately, silently skipping the *real* dusk closing later the same day until the next midnight rollover (~6h late, every night). It's now keyed off the date of that day's auto-open instead, so it fires at the actual dusk.

**Pulses aim for the sun's position at the *middle* of the next check interval, not "now"** (2026-10-30) - using `astral` directly (already a transitive dependency of HA's own `sun` integration, nothing extra to install) to project azimuth/elevation forward by half of `check_interval_minutes`. Targeting "now" always lags for the whole interval between checks (correct the instant it's computed, increasingly stale until the next one); the midpoint balances that into a small lead/lag either side instead of a one-sided drift. The **Winter sun deadband** number entity's default was lowered from 5% to 2.5% to match the tighter tracking - sub-threshold checks still accumulate against the original baseline rather than being lost (see above).

From there, at every mark on the clock implied by the **Winter sun check interval** select entity (5/10/15/30 min or 1 h - e.g. 15 min lands on :00/:15/:30/:45, not just every 15 minutes from whenever you flipped the switch) it re-evaluates the target angle and, if it's moved meaningfully since the *last* check, sends a timed `Close` pulse to catch up — sized using the **Full travel time** number entity, defaulted to 75s (remeasured 2026-10-16 from real `A`→`K` board events over the whole 0°-140° range; re-time and adjust it if your unit differs - note a *partial* run, e.g. near-closed to fully closed, is proportionally much faster, not 75s). Sub-threshold moves are skipped without losing track of them - a few small moves in a row still add up into one correctly-sized catch-up pulse against the original baseline, rather than each being forgotten individually. The regular pulses pause (do nothing) only while the weather entity reports wind at/above the configured threshold, resuming automatically once it clears - **not** on rain any more (2026-10-16): the pergola's own rain sensor already closes it at the *onset* of rain regardless, so a paused pulse only ever mattered for the one narrow case of a deliberate manual reopen mid-rain (e.g. to rinse the terrace) - and even then, the consequence was just a small, harmless catch-up pulse once conditions clear, not worth the complexity of a separate rain-aware code path. Rain still fully gates the once-a-day auto-open above, where opening onto active rain is a real, obvious problem.

**Crucially, regular pulse size is always computed from how much the *target* angle has moved since the last check, never from any absolute "how open is it now" estimate.** This matters a lot for a device with no fine-grained position feedback (it does now report `opening`/`closing`/`stopped`/`open`/`closed` as of 0.2.1 - see below - but never a percentage or angle): if winter sun mode had been off for hours or days, or the physical remote was used in the meantime, any absolute estimate of "how open it currently is" would be pure guesswork - and a pulse sized from a wrong absolute guess could be wrong by an arbitrary, unbounded amount, in a way that isn't actually harmless (over-closing a pergola that was already correctly positioned is a real malfunction, not a no-op). That's exactly why the very first check of a day never pulses at all when there's no confirmed baseline - only relative movement from a known point ever gets acted on, so the worst case is always bounded by how far the sun genuinely moved, never by how wrong some guess might have been.

**Diagnostics**: `sensor.pergola_blades_state` (pergola_controller 0.2.1+) is the card's own real-time report - `opening`/`closing`/`stopped`/`open`/`closed` - a fact, not an estimate, whenever the connection is live (periodic poll by default, see "Status poll interval"; stale/unavailable the rest of the time by design, see the top-level section on connection modes). `number.pergola_estimated_open_percentage` (0-100%) remains a cosmetic best-guess *angle*, not a real reading, used for display and hand-correction only - see its own long docstring in `number.py` for exactly what it does and doesn't feed into. It snaps to 100%/0% automatically whenever `sensor.pergola_blades_state` reports a confirmed `open`/`closed` transition, regardless of who/what caused it (HA, remote, vendor app) - see `WinterSunController._on_connection_status()`.

To validate/calibrate the model against the real slats (2026-10-17): `sensor.pergola_sun_azimuth` and `sensor.pergola_sun_elevation` mirror `sun.sun`'s own attributes for convenience, and `sensor.pergola_target_slat_angle` shows what `target_position()`/`profile_angle()` currently say the slats *should* be angled at (0°-140°), recomputed live on every `sun.sun` update - not just at the last check/pulse. Hold a protractor up to the real slats and compare against this sensor's current value at the same moment.

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
