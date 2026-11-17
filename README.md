# phonejack

Ring an ordinary landline handset from a computer, speak a question, listen to the
answer, and get back **yes / no / unclear / none**. Built for Home Assistant
automations ("Is the laundry done?" → the kitchen phone rings → you say "yes").

It uses a **Cell2Jack** Bluetooth-to-landline adapter. On Linux the computer plays the
*phone* side of the Bluetooth Hands-Free Profile (audio gateway) over raw RFCOMM and
SCO sockets, speaks with `espeak-ng` and transcribes with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`tiny.en`).

> Status: works on the author's hardware (Debian, PipeWire, one Cell2Jack). Expect to
> tune things for yours. Issues and PRs welcome.

## Files

| File | Purpose |
|---|---|
| `phonejack_server.py` | The main program: HTTP server on `127.0.0.1:8765` |
| `phonejack.py` | Helper library (Hands-Free handshake). Also a Windows CLI for `ring` / `speak` / `tts` |
| `ask_test.py`, `listen_test.py` | Hand-run test scripts |
| `ha/phonejack_ha.yaml` | Home Assistant `rest_command` + webhook automations |
| `ha/phonejack.service` | systemd user unit |
| `ha/install_ha.sh` | Installs the HA package into a Docker-based HA |

## Setup (Linux)

1. Pair and trust the Cell2Jack with `bluetoothctl`; note its address (`bluetoothctl devices`).
2. Install system packages: `espeak-ng`, `pipewire` + `pipewire-pulse`, Bluetooth stack (`bluez`).
3. Python environment:
   ```sh
   python3 -m venv venv && venv/bin/pip install -r requirements.txt
   ```
4. Run the server:
   ```sh
   PHONEJACK_ADDR=AA:BB:CC:DD:EE:FF venv/bin/python -u phonejack_server.py
   ```
5. Ask a question:
   ```sh
   curl -X POST http://127.0.0.1:8765/ -d '{"question":"Is the laundry done?","id":"x"}'
   ```
   The result (`RESULT: {...}`) is logged and, if you pass `"callback"`, POSTed there.

Request fields: `question`, `say`, `id`, `callback`, `ring_seconds` (40),
`listen_seconds` (6), `hangup_wait_seconds` (60).

### Run as a service

Keep your own settings in a drop-in so the repo stays clean and `git pull` never conflicts:
```sh
mkdir -p ~/.config/systemd/user/phonejack.service.d
printf '[Service]
Environment=PHONEJACK_ADDR=AA:BB:CC:DD:EE:FF
' > ~/.config/systemd/user/phonejack.service.d/local.conf
mkdir -p ~/.config/systemd/user && cp ha/phonejack.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now phonejack.service
sudo loginctl enable-linger $USER
```
The unit expects the project in `~/phonejack` with a `venv/` inside it.

### Home Assistant

`ha/phonejack_ha.yaml` adds a `phonejack_call` webhook that places a call and a
`phonejack_result` webhook that fires a `phonejack_result` event
(`status, answer, heard, hung_up, id, error`). `127.0.0.1` URLs assume HA runs on the
same machine (or Docker with host networking). For a container install:
```sh
sudo sh ha/install_ha.sh /path/to/ha/config [container_name]
```
Then trigger a call:
```sh
curl -X POST http://<ha>:8123/api/webhook/phonejack_call \
  -H 'Content-Type: application/json' -d '{"question":"Is the laundry done?","id":"laundry1"}'
```

## Configuration (environment variables)

`PHONEJACK_ADDR` (required), `PHONEJACK_SINK`, `PHONEJACK_CHANNEL`, `PHONEJACK_MODE`
(`sco` duplex, default; `media` older behaviour), `PHONEJACK_BARGE_PEAK` (4000; raise to
~6000 if the question is cut off with nobody talking), `PHONEJACK_END_SILENCE` (2.0),
`PHONEJACK_MAX_SPEECH` (10), `PHONEJACK_TTS_GAIN`, `PHONEJACK_LATE_ANSWER`. See the
top of `phonejack_server.py` for the full list.

## Hard-won notes

- Keep **one** RFCOMM control link open for the server's lifetime; closing it after each
  call made the adapter ignore new connections.
- `bluetoothctl info` can say `Connected: yes` while the adapter ignores RFCOMM. A
  `disconnect` + `connect` fixes it (the server does this itself).
- The handset sends `AT+CHUP` on hang-up. A handset lifted *after* ringing stopped sends
  nothing at all, so a "late answer" can't be detected.
- Reading the control link while media audio plays cuts off the first words; duplex (SCO)
  mode avoids that and allows barge-in.
- If connections stop working, press `#*#` on the handset and cycle Bluetooth.

## License

MIT
