# Troubleshooting

Problems hit while bringing phonejack up on real hardware (a Cell2Jack-style Bluetooth
handset adapter, with a Linux box playing the *phone* side of Bluetooth Hands-Free), and what
fixed them. Everything here was observed on the real device, not guessed.

Throughout, `AA:BB:CC:DD:EE:FF` is the adapter's Bluetooth address.

## Quick symptom table

| Symptom | Most likely cause | Fix |
|---|---|---|
| `No Hands-Free channel answered`, connects time out | Adapter wedged while `bluetoothctl` still says `Connected: yes` | [Cycle Bluetooth](#1-connects-time-out-but-bluetoothctl-says-connected) |
| First call works, the next one fails | Control link was closed after the first call | [Keep one link open](#2-the-second-call-fails) |
| First words of the question are missing | Control link was read while audio played | [Audio](#3-the-first-words-of-the-question-are-lost) |
| Dial tone while it speaks, or when listening starts | Media audio played with no call open | [Use the call link](#4-dial-tone-while-speaking-or-a-flip-when-listening-starts) |
| `hung_up` is false although you hung up | Hang-up never read, or window too short | [Hang-up](#5-hang-up-is-not-detected) |
| A late pickup is never reported | Adapter sends nothing without a ringing call | [Late pickup](#6-picking-up-after-the-ringing-stopped) |
| Question cut off with nobody talking | Earpiece sound leaking into the mic | [Barge-in](#7-the-question-is-cut-off-by-itself) |
| Server restart over SSH fails or kills itself | `pkill -f` matches its own shell | [Deploying](#8-restarting-the-server-over-ssh) |
| `bluetoothctl connect` fails with `br-connection-profile-unavailable` | PipeWire registered no audio profiles (headless, no login seat) | [Headless PipeWire](#9-br-connection-profile-unavailable-on-a-headless-machine) |
| Pipewire/WirePlumber `inactive (dead)` after install | Socket-activated; nothing has used audio yet | [Start the audio services](#10-pipewire-is-inactive-on-a-server) |
| No Hands-Free channel, or the audio stack grabs the call | The system's audio stack owns the Hands-Free role | [Release Hands-Free](#11-letting-the-script-own-the-hands-free-service-linux) |
| `connect(): wrong format` opening SCO | SCO addresses are a string, not a tuple | [SCO address](#12-sco-connect-wrong-format) |
| Only about 1% of the microphone audio arrives | The capture loop blocks on the other socket | [Draining SCO](#13-only-a-few-microphone-packets-arrive) |
| `bluetoothctl connect` says `br-connection-page-timeout` | The adapter is off or asleep | [Page timeout](#14-bluetoothctl-connect-errors-and-what-they-mean) |
| Speech works in the CLI but is silent from the web service | Text-to-speech library run in a background thread | [TTS in a thread](#15-text-to-speech-is-silent-inside-the-web-service) |
| Keypad digits never show up | The adapter sends none | [Keypad](#16-keypad-digits-are-not-sent) |
| Speech-to-text crashes in `av.open()` / hallucinates | Library version mismatch / garbage audio | [Transcribing](#17-transcribing-the-captured-audio) |
| Home Assistant cannot talk to Bluetooth | Docker does not give the container BlueZ or audio | [Home Assistant](#18-running-with-home-assistant-in-docker) |

## 1. Connects time out, but `bluetoothctl` says Connected

`bluetoothctl info AA:BB:CC:DD:EE:FF` shows `Connected: yes`, yet an RFCOMM connect to the
Hands-Free channel just times out (on every channel). Waiting does not help: it was still
dead after more than four minutes.

Fix: drop and re-make the Bluetooth link, then connect again.

```bash
bluetoothctl disconnect AA:BB:CC:DD:EE:FF; sleep 3
bluetoothctl connect AA:BB:CC:DD:EE:FF;    sleep 3
```

The adapter then answers immediately (the first thing it sends is `AT+BRSF=...`).
The server now does this by itself when its first round of connection attempts fails.
Pressing `#*#` on the handset also restarts the adapter's Bluetooth connection.

To see what is really happening, probe the channel directly and look at the error:

```python
import socket
s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
s.settimeout(5)
s.connect(("AA:BB:CC:DD:EE:FF", 1))   # TimeoutError = adapter ignoring us
print(s.recv(100))                      # a healthy adapter sends b'AT+BRSF=...'
```

On Windows, also check that "Hands-free Telephony" is unticked for the device, otherwise
Windows owns the Hands-Free channel (see the header of `phonejack.py`).

## 2. The second call fails

After a call ended, every new connection attempt failed, even with a 10 s cooldown and
retries. Cause: the server closed its control (RFCOMM) connection after each call. A real
phone never does; reference gateway code (e.g. Bumble's HFP gateway) keeps the service-level
connection open between calls.

Fix: open the control link once, keep it open for the life of the server, ring on it for
every request, and keep reading it between calls (the adapter sends commands such as volume
changes at any time and expects `OK`). Reconnect only if it drops. With this, several calls
in a row worked with no reconnects.

## 3. The first words of the question are lost

Playing text-to-speech over the normal audio (media) channel while also reading the control
link every few hundred ms made the first words disappear (you hear "two words", then silence).
Things that did **not** help: a longer lead-in of silence (5 s), and adding a pause after
pickup (it made things worse: the link idled and dropped the audio path).

Things that did help:
- Do not touch the control link while the audio plays (play with a plain blocking player and
  read the link right afterwards), or
- better, avoid the media channel entirely: see the next section.

## 4. Dial tone while speaking, or a flip when listening starts

With the handset off-hook and no call open, the adapter plays its own dial tone. Media audio
then plays over it, and starting to listen (which needs a call) flips the line to the call
channel and back.

Fix: keep a (fake) call open from pickup (`+CIEV: call,1`, `+CIEV: callsetup,0`) and do both
directions over the **SCO** call link:
- For every mic packet received, send one packet of outgoing audio (or silence). The adapter's
  own clock then paces playback; no timers are needed.
- Audio is 8 kHz, 16-bit mono PCM. Resample text-to-speech down to that.
- Send silence right at pickup so there is no gap.
- Because the mic is live while it speaks, the caller can answer early, and you can end
  listening a couple of seconds after the speech stops instead of using a fixed window.

## 5. Hang-up is not detected

The adapter sends `AT+CHUP` when the handset is put down. Log every command you receive
(`<< AT+...`) so you can see this. The server must answer `OK` and then send
`+CIEV: call,0` itself. It only sees the command if it is reading the control link at that
moment, so read it during the whole conversation and while waiting for the hang-up, and make
the wait long enough (a result of `hung_up: false` can simply mean the handset was put down
after the wait ended).

## 6. Picking up after the ringing stopped

Once the ringing has stopped, lifting the handset gives only a dial tone, and the adapter
sends **no command at all**. There is nothing to detect, so an "answered late" feature
cannot work with this hardware. Keep the ring window long enough (40 s) instead.
A handset left off-hook with no call stays on the dial tone; on some units that state may
trigger a "power on" shutdown message (not confirmed).

## 7. The question is cut off by itself

With a live mic during the question, the earpiece can leak into the mic. Measured here:
silence about 500 to 900, earpiece leakage up to roughly 3000 to 4500, speech 5000 to 27000
(raw 16-bit peaks). Use a higher bar while the question plays than after it (the defaults
are 4000 while speaking and 1500 afterwards), require several loud packets in a row rather
than one spike, and tune the bar from the logged "mic peak while the question played" value.

## 8. Restarting the server over SSH

`pkill -f phonejack_server.py` run through `ssh host '...'` matches the remote shell's own
command line and kills the session (exit code 255). Look up the PID in a separate command
(`pgrep -f "^venv/bin/python -u phonejack_server"`) and `kill <pid>`, then start the server
with `nohup ... &` in another command.

## Handy protocol facts (observed)

- The adapter initiates the Hands-Free handshake with: `AT+BRSF=63`, `AT+CIND=?`, `AT+CIND?`,
  `AT+CMER=3, 0, 0, 1`, then often `AT+VGS=..`, `AT+BTRH?`, `AT+CSRSF=...`. Answering `OK`
  to unknown commands worked. Replying `+BRSF: 0` was accepted.
- To ring: `+CIEV: callsetup,1`, then repeat `RING` and `+CLIP` every 3 s. Pickup arrives as `ATA`.
- Hang-up arrives as `AT+CHUP`.
- Open SCO only while the fake call is active; the first packets tell you the packet size,
  which you can reuse when sending.
- Speech recognition: the small English model returns `"."` or a one-word guess for unclear
  audio; treat anything that is not clearly "yes" or "no" as `unclear`.

## 9. `br-connection-profile-unavailable` on a headless machine

`bluetoothctl pair` works, but `bluetoothctl connect AA:BB:CC:DD:EE:FF` fails with
`org.bluez.Error.Failed br-connection-profile-unavailable`.

Check what the computer itself offers:

```bash
bluetoothctl show | grep -i -E "Audio|UUID: A"
```

If there is no local `Audio Source (0000110a...)` and `Audio Sink (0000110b...)`, nothing has
registered audio profiles with BlueZ, so there is nothing to connect with. On a machine
with no desktop session (SSH only, `loginctl list-sessions` shows no seat), WirePlumber 0.5
does not start its Bluetooth part at all. The journal then shows no Bluetooth lines.

Fix: turn off the seat requirement in
`~/.config/wireplumber/wireplumber.conf.d/52-bluez-headless.conf`:

```
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
```

Restart with `systemctl --user restart wireplumber`. The two audio UUIDs then appear, the
connect succeeds, and the device shows up as a `bluez5` sink in `wpctl status`.

## 10. PipeWire is `inactive (dead)` on a server

On a fresh install `systemctl --user status pipewire wireplumber` shows both inactive,
because PipeWire starts on demand. Start it and keep the user session alive:

```bash
loginctl enable-linger $USER
systemctl --user enable --now pipewire.socket pipewire-pulse.socket wireplumber.service
```

The `libcamera` warnings WirePlumber prints on a machine without that plugin are harmless.
`pactl` is not installed by `pipewire-audio`; use `wpctl status` and `wpctl inspect <id>` (the
sink's `node.name`, such as `bluez_output.AA_BB_CC_DD_EE_FF.1`, is what `pw-play --target`
needs).

## 11. Letting the script own the Hands-Free service (Linux)

The Linux version of "untick Hands-free Telephony" on Windows is to stop WirePlumber from
claiming the Hands-Free roles, so it only handles audio. In
`~/.config/wireplumber/wireplumber.conf.d/51-no-hfp.conf`:

```
monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink a2dp_source ]
}
```

With this in place, the Hands-Free channel (channel 1 on the unit tested) was reachable by
the script and ringing worked. (This was applied before the first attempt, so it was not
checked what happens without it.)

## 12. SCO `connect(): wrong format`

In Python on Linux, RFCOMM takes a tuple but SCO takes just the address string:

```python
sco = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO)
sco.connect("AA:BB:CC:DD:EE:FF")      # not ("AA:BB:CC:DD:EE:FF",)
```

It must be done while a (fake) call is active. Packets were 48 bytes: 8 kHz, 16-bit mono PCM.

## 13. Only a few microphone packets arrive

Recording 10 s should give about 3,300 packets (about 160,000 bytes). A capture loop that
read one SCO packet and then blocked on the control link for 0.3 s got only about 34
packets (about 3 per second) while the rest sat unread in the kernel buffer. The audio that did
arrive was real, just almost all missing.

Fix: wait on both sockets together and read whichever is ready:

```python
ready, _, _ = select.select([sco, ctrl], [], [], 0.2)
if sco in ready:
    chunks.append(sco.recv(1024))
if ctrl in ready:
    handle_control()
```

## 14. `bluetoothctl connect` errors and what they mean

- `br-connection-page-timeout`: the adapter did not answer at all (switched off, asleep, or
  not powered). It needed a power cycle (unplug, wait a second, plug in) or `#*#` on the
  handset; it then connected normally.
- `br-connection-busy`: try again a moment later; the second attempt succeeded.
- `br-connection-profile-unavailable`: see section 9.

Leaving the handset lifted for a long time after a call was followed by the adapter's
"power on" voice message and `Connected: no`. This matches section 6 and is still
unconfirmed.

## 15. Text-to-speech is silent inside the web service

A text-to-speech library (`pyttsx3`) worked from the command line but produced no sound when
the same call ran in the web server's worker thread, and the log never reached its "answered"
line. Generating the speech with a command-line tool instead worked:

```python
subprocess.run(["espeak-ng", "-w", tmp_wav, text], check=True)
```

then play `tmp_wav` as usual. (The cause was not proven; running that library off the main
thread is the suspect.)

## 16. Keypad digits are not sent

While a fake call was open, pressing keypad digits on the handset produced no Hands-Free
commands (no `AT+VTS`). Only `AT+CHUP` appeared, when the handset was hung up. Do not design
the interaction around keypad digits; use spoken answers over SCO instead (sections 4 and 13).

## 17. Transcribing the captured audio

- `faster-whisper` crashed inside `av.open()` (`TypeError: open() got ...`) when given a WAV
  file path, a version mismatch with its audio library. Pass a float32 NumPy array instead:
  convert the 16-bit samples to `-1..1` and resample 8 kHz to 16 kHz (`np.interp` was enough).
- The small English model hallucinates on audio it cannot understand: random speech gave
  "a bird, a bird, a bird, ..." and clear counting gave "1, 2, 3, ... 10, 11". If the transcript
  looks repetitive and silly, listen to the saved WAV first.
- Check the microphone level before transcribing. Observed raw peaks: a silent line stayed
  around 1,400 or lower, speaking reached 9,000 to 23,000, and noise that produced no words
  reached about 8,000. A peak below about 1,500 can be treated as "no answer".

## 18. Running with Home Assistant in Docker

A Home Assistant container does not get BlueZ, D-Bus or audio, even with host networking.
Run phonejack on the host and let Home Assistant call it:

- Bind the service to `127.0.0.1` so it is not exposed to the network; with host networking
  the container reaches it at `http://127.0.0.1:<port>`.
- A call (ring, ask, listen, transcribe) can take over a minute, so return `202` at once
  and have the service post the result to a Home Assistant webhook when finished.
- Only accept callback URLs that start with Home Assistant's own local address, so the
  service cannot be used to send requests elsewhere.
- Run one call at a time (reject extra requests with `409`).
