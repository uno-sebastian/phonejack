"""phonejack - ring a home telephone through a Cell2Jack and speak to whoever answers.

REQUIREMENTS
    Windows, Python 3.9 or newer, Cell2Jack paired with this PC over Bluetooth.

    Libraries to install:
        python -m pip install sounddevice soundfile pyttsx3 numpy

        sounddevice  plays audio to a chosen output device
        soundfile    reads .wav files
        pyttsx3      text-to-speech using the voices built into Windows
        numpy        audio buffers (adds the silence that wakes the Bluetooth link)

    Everything else (socket, argparse, subprocess, ...) ships with Python.
    Ringing uses Python's built-in Bluetooth sockets, so no Bluetooth library is needed.

ONE-TIME WINDOWS SETTING (needed for the `ring` command)
    Windows normally takes over the Cell2Jack's call ("Hands-Free") service.
    This script has to talk to that service itself, so Windows must release it:

    1. Press Win+R, paste this and press Enter (opens Devices and Printers):
           shell:::{A8A91A66-3A7D-4424-8D24-04E180695C7A}
    2. Right-click the Cell2Jack > Properties > Services tab.
    3. UNTICK "Hands-free Telephony". Leave "Audio Sink" TICKED
       (that is the media audio used by speak / tts).
    4. Click OK.

    To undo, tick "Hands-free Telephony" again. If `ring` ever reports
    "No Hands-Free channel answered", check this setting first: Windows can
    re-tick it when the Cell2Jack is removed and paired again.

USING THE PHONE
    To bluetooth connect the Cell2Jack, plug it in and press #*# to start the connection.
    speak / tts play straight away, so the handset must already be off-hook.
    If you hear nothing, dial 61# on the handset once to turn on media audio.
    ring starts with the handset on-hook and plays after someone picks up.

Usage:
    python phonejack.py devices
    python phonejack.py speak good_morning.wav       play now (handset must be off-hook)
    python phonejack.py tts "Laundry is finished."   speak now (handset must be off-hook)
    python phonejack.py ring                         ring, report answer / no answer
    python phonejack.py ring --say "Good morning!"   ring, then speak when answered
    python phonejack.py ring --play alarm.wav        ring, then play a file when answered

Options (before the command): --device TEXT   output device name (default "cell2jack")
Ring options: --seconds N (default 20), --addr AA:BB:.., --channel N, --quiet
Exit code for ring: 0 = answered, 1 = no answer / rejected.
"""
import argparse
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

LEAD_SECONDS = 2.0  # silence played first so the Bluetooth link is awake


# ---------------------------------------------------------------- audio

def output_devices():
    return [(i, d) for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]


def find_device(name):
    # First match is normally the MME entry, which resamples for us.
    for i, d in output_devices():
        if name.lower() in d["name"].lower():
            return i
    sys.exit(f'No output device matching "{name}". Run: python phonejack.py devices')


def play(path, device_name):
    data, rate = sf.read(path, dtype="float32", always_2d=True)
    lead = np.zeros((int(rate * LEAD_SECONDS), data.shape[1]), dtype="float32")
    tail = np.zeros((int(rate * 0.5), data.shape[1]), dtype="float32")
    sd.play(np.concatenate([lead, data, tail]), rate, device=find_device(device_name))
    sd.wait()


def tts(text, device_name):
    import pyttsx3

    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        engine = pyttsx3.init()
        engine.save_to_file(text, path)
        engine.runAndWait()
        play(path, device_name)
    finally:
        os.remove(path)


# ---------------------------------------------------------------- ringing
# We act as the "cellphone" side of Bluetooth Hands-Free: handshake, RING,
# and the Cell2Jack sends ATA when the handset is picked up.

CIND_TEST = ('+CIND: ("service",(0,1)),("call",(0,1)),("callsetup",(0-3)),'
             '("callheld",(0-2)),("signal",(0-5)),("roam",(0,1)),("battchg",(0-5))')
CIND_NOW = "+CIND: 1,0,0,0,5,0,5"
CALL, CALLSETUP = 2, 3  # indicator positions in the list above


def find_address():
    ps = "Get-PnpDevice -Class Bluetooth | ? FriendlyName -like '*Cell2Jack*' | % InstanceId"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=30).stdout
    m = re.search(r"DEV_([0-9A-F]{12})", out, re.I) or re.search(r"&([0-9A-F]{12})_C0", out, re.I)
    if not m:
        sys.exit("Could not find the Cell2Jack's Bluetooth address; pass --addr AA:BB:CC:DD:EE:FF")
    h = m.group(1).upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def find_handsfree(addr, channel, log):
    """Return (socket, first_bytes). The hands-free side speaks first with AT+BRSF."""
    for ch in ([channel] if channel else range(1, 31)):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        s.settimeout(5)
        try:
            s.connect((addr, ch))
            s.settimeout(3)
            data = s.recv(1024)
        except OSError:
            s.close()
            continue
        if b"AT" in data:
            log(f"Hands-Free service on channel {ch}")
            return s, data
        s.close()
    sys.exit("No Hands-Free channel answered. Is 'Hands-free Telephony' unticked in "
             "Windows for the Cell2Jack, and is the Cell2Jack powered and in range?")


def ring(seconds, addr=None, channel=None, on_answer=None, verbose=True):
    """Ring the phone. Returns True if answered. on_answer() runs after pickup."""
    log = print if verbose else (lambda *_: None)
    s, buf = find_handsfree(addr or find_address(), channel, log)
    s.settimeout(0.3)
    state = {"ready_at": None, "answered": False, "hung_up": False}

    def send(line):
        log("  >>", line)
        s.sendall(b"\r\n" + line.encode() + b"\r\n")

    def handle(cmd):
        log("  <<", cmd)
        up = cmd.upper()
        if up.startswith("AT+BRSF="):
            send("+BRSF: 0")
        elif up == "AT+CIND=?":
            send(CIND_TEST)
        elif up == "AT+CIND?":
            send(CIND_NOW)
        elif up.startswith("AT+CMER"):
            state["ready_at"] = time.monotonic() + 1.5
        elif up == "AT+CHLD=?":
            send("+CHLD: (0,1,2,3)")
        elif up == "AT+COPS?":
            send('+COPS: 0,0,"PhoneJack"')
        elif up == "ATA":
            state["answered"] = True
        elif up == "AT+CHUP":
            state["hung_up"] = True
        elif up.startswith(("AT+XAPL", "AT+IPHONEACCEV", "AT+XEVENT", "AT+ANDROID", "AT+BIND")):
            send("ERROR")
            return
        send("OK")

    def pump():
        nonlocal buf
        try:
            data = s.recv(1024)
            if not data:
                sys.exit("Cell2Jack closed the connection")
            buf += data
        except TimeoutError:
            pass
        while b"\r" in buf:
            line, buf = buf.split(b"\r", 1)
            line = line.strip().decode(errors="replace")
            if line:
                handle(line)

    try:
        deadline = time.monotonic() + 15
        while not (state["ready_at"] and time.monotonic() >= state["ready_at"]):
            if time.monotonic() > deadline:
                sys.exit("Handshake with the Cell2Jack did not complete")
            pump()

        send(f"+CIEV: {CALLSETUP},1")
        ring_until = time.monotonic() + seconds
        next_ring = 0
        while not state["answered"] and not state["hung_up"] and time.monotonic() < ring_until:
            if time.monotonic() >= next_ring:
                send("RING")
                send('+CLIP: "5550100",129')
                next_ring = time.monotonic() + 3
            pump()

        if not state["answered"]:
            send(f"+CIEV: {CALLSETUP},0")
            return False

        send(f"+CIEV: {CALL},1")
        send(f"+CIEV: {CALLSETUP},0")
        time.sleep(0.5)
        send(f"+CIEV: {CALL},0")  # drop the fake call so media audio can play
        if on_answer:
            on_answer()
        return True
    finally:
        time.sleep(0.3)
        s.close()


# ---- Linux overrides
# Bluetooth address of your Cell2Jack (see `bluetoothctl devices`). Required.
ADDR = os.environ.get("PHONEJACK_ADDR", "")
# PipeWire sink name of the adapter (see `pw-cli ls Node` / `wpctl status`);
# derived from the address when not set.
SINK = os.environ.get("PHONEJACK_SINK") or (
    "bluez_output.%s.1" % ADDR.upper().replace(":", "_"))


def find_address():
    if not ADDR:
        sys.exit("Set PHONEJACK_ADDR to your Cell2Jack's Bluetooth address "
                 "(e.g. PHONEJACK_ADDR=AA:BB:CC:DD:EE:FF)")
    return ADDR.upper()


def play(path, device_name=None):
    data, rate = sf.read(path, dtype="float32", always_2d=True)
    lead = np.zeros((int(rate * LEAD_SECONDS), data.shape[1]), dtype="float32")
    tail = np.zeros((int(rate * 0.5), data.shape[1]), dtype="float32")
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        sf.write(tmp, np.concatenate([lead, data, tail]), rate)
        subprocess.run(["pw-play", "--target", SINK, tmp], check=True)
    finally:
        os.remove(tmp)


# ---------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(prog="phonejack")
    p.add_argument("--device", default="cell2jack")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices")
    sub.add_parser("speak").add_argument("wav")
    sub.add_parser("tts").add_argument("text")
    r = sub.add_parser("ring")
    r.add_argument("--seconds", type=float, default=20)
    r.add_argument("--say", help="text to speak once answered")
    r.add_argument("--play", help="wav file to play once answered (before --say)")
    r.add_argument("--addr")
    r.add_argument("--channel", type=int)
    r.add_argument("--quiet", action="store_true", help="hide the protocol log")
    a = p.parse_args()

    if a.cmd == "devices":
        for i, d in output_devices():
            print(i, d["name"])
    elif a.cmd == "speak":
        play(a.wav, a.device)
    elif a.cmd == "tts":
        tts(a.text, a.device)
    elif a.cmd == "ring":
        def on_answer():
            if a.play:
                play(a.play, a.device)
            if a.say:
                tts(a.say, a.device)

        answered = ring(a.seconds, a.addr, a.channel, on_answer, verbose=not a.quiet)
        print("ANSWERED" if answered else "NO ANSWER")
        sys.exit(0 if answered else 1)


if __name__ == "__main__":
    main()