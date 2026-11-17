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
