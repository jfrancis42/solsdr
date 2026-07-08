# Running solsdr as a service (systemd)

solsdr's design goal is a "front-panel-less" appliance — a box that comes up,
connects to the SunSDR2 PRO, and exposes CAT + IQ (and optionally the
digital-mode audio bridge) with no interactive shell. These unit files make that
an always-on service.

Two units, because they have different needs:

| Unit | Type | Why |
|------|------|-----|
| `solsdr-receiver.service` | **system** | Headless RX engine: CAT (rigctld) + raw IQ TCP server. No audio session needed. |
| `solsdr-bridge.service`   | **user**   | Digital-mode bridge for JS8Call/WSJT-X. Needs the per-user PulseAudio/PipeWire session, so it runs on the user bus. |

Pick the one that matches what you're running. You rarely need both on the same
host — the bridge already stands up its own rigctld.

## Prerequisites

- `pip install .` so the `solsdr` command and the `solsdr` package are on the
  system path (or adjust `ExecStart=` to point at a source checkout — see the
  comment in `solsdr-receiver.service`).
- `rigctld` available (`hamlib` / `libhamlib-utils`) for any CAT path.
- A station config so the units stay generic. Put defaults in
  `~/.config/solsdr/config.*` (user service) or `/etc/solsdr/config.conf`
  (system service, referenced via `--config`). Example:

  ```
  # /etc/solsdr/config.conf
  radio_ip = 10.1.2.3
  local_ip = 10.1.2.185
  device   = 5
  variant  = PRO
  freq_khz = 14074
  mode     = USB
  ```

  Keys mirror the `solsdr` CLI flags; CLI flags in `ExecStart=` still override
  the file. See `solsdr/config.py` for the full key list and format.

## Headless RX engine (system service)

```bash
sudo mkdir -p /etc/solsdr
sudoedit /etc/solsdr/config.conf            # radio_ip, local_ip, device, freq_khz…
sudo cp systemd/solsdr-receiver.service /etc/systemd/system/
sudoedit /etc/systemd/system/solsdr-receiver.service   # set User= and ExecStart=
sudo systemctl daemon-reload
sudo systemctl enable --now solsdr-receiver
journalctl -u solsdr-receiver -f
```

Notes:
- `--seconds` is deliberately **omitted** so it runs forever; `Restart=on-failure`
  brings it back if the radio drops (solsdr also auto-reconnects internally).
- `AmbientCapabilities=CAP_SYS_NICE` lets the TX pacer request `SCHED_FIFO`
  without full root. RX-only? It's harmless to leave in.
- Use `--log-level` (default `info`) to tune journal verbosity; solsdr's logging
  goes to stdout/stderr, which the journal captures.

## Digital-mode bridge (user service)

```bash
mkdir -p ~/.config/systemd/user ~/.config/solsdr
$EDITOR ~/.config/solsdr/config.conf
cp systemd/solsdr-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now solsdr-bridge
systemctl --user status solsdr-bridge
```

To keep the user service running while logged out (true appliance):

```bash
sudo loginctl enable-linger $USER
```

The `ExecStopPost` line unloads any `solsdr-*` PulseAudio null sinks the bridge
created, so restarts don't leak virtual devices.

## Uninstall

```bash
# system
sudo systemctl disable --now solsdr-receiver
sudo rm /etc/systemd/system/solsdr-receiver.service && sudo systemctl daemon-reload
# user
systemctl --user disable --now solsdr-bridge
rm ~/.config/systemd/user/solsdr-bridge.service && systemctl --user daemon-reload
```
