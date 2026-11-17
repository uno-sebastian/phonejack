import os
import re
import select
import socket
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np
import phonejack as pj
from faster_whisper import WhisperModel

QUESTION = (sys.argv[1] if len(sys.argv) > 1 else
            "Is the laundry finished? Please say yes or no after I stop talking.")
LISTEN = 6.0
ADDR = pj.find_address()

s, buf = pj.find_handsfree(ADDR, 1, print)
s.settimeout(0.3)
st = {"ready_at": None, "answered": False, "hung_up": False}


def send(line):
    s.sendall(b"\r\n" + line.encode() + b"\r\n")


def handle(cmd):
    print("  <<", cmd, flush=True)
    up = cmd.upper()
    if up.startswith("AT+BRSF="):
        send("+BRSF: 0")
    elif up == "AT+CIND=?":
        send(pj.CIND_TEST)
    elif up == "AT+CIND?":
        send(pj.CIND_NOW)
    elif up.startswith("AT+CMER"):
        st["ready_at"] = time.monotonic() + 1.5
    elif up == "AT+CHLD=?":
        send("+CHLD: (0,1,2,3)")
    elif up == "AT+COPS?":
        send('+COPS: 0,0,"PhoneJack"')
    elif up == "AT+CLCC":
        pass
    elif up == "ATA":
        st["answered"] = True
    elif up == "AT+CHUP":
        st["hung_up"] = True
    elif up.startswith(("AT+XAPL", "AT+IPHONEACCEV", "AT+XEVENT", "AT+ANDROID", "AT+BIND")):
        send("ERROR")
        return
    send("OK")


def pump():
    global buf
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


def speak(text):
    print("-- speaking:", text, flush=True)
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(["espeak-ng", "-w", tmp, text], check=True)
        pj.play(tmp, None)
    finally:
        os.remove(tmp)


def listen(seconds):
    sco = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO)
    sco.settimeout(5)
    sco.connect(ADDR)
    print("-- listening...", flush=True)
    chunks = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([sco, s], [], [], 0.2)
        if sco in ready:
            d = sco.recv(1024)
            if not d:
                break
            chunks.append(d)
        if s in ready:
            pump()
    sco.close()
    return b"".join(chunks)


def transcribe(raw):
    with wave.open("ask_capture.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(raw)
    data = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype("float32") / 32768.0
    if len(data) < 800:
        return ""
    n = int(len(data) * 2)
    audio = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data).astype("float32")
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio, language="en")
    return " ".join(seg.text.strip() for seg in segments)


# ---- handshake
while not (st["ready_at"] and time.monotonic() >= st["ready_at"]):
    pump()

# ---- ring
send(f"+CIEV: {pj.CALLSETUP},1")
ring_until = time.monotonic() + 40
next_ring = 0
while not st["answered"] and time.monotonic() < ring_until:
    if time.monotonic() >= next_ring:
        send("RING")
        send('+CLIP: "5550100",129')
        next_ring = time.monotonic() + 3
    pump()
if not st["answered"]:
    send(f"+CIEV: {pj.CALLSETUP},0")
    sys.exit("NO ANSWER")

# ---- answered: drop the fake call so media audio can play, ask the question
send(f"+CIEV: {pj.CALL},1")
send(f"+CIEV: {pj.CALLSETUP},0")
time.sleep(0.5)
send(f"+CIEV: {pj.CALL},0")
speak(QUESTION)

# ---- re-open the fake call and listen over SCO
send(f"+CIEV: {pj.CALL},1")
time.sleep(0.5)
try:
    raw = listen(LISTEN)
except OSError as e:
    send(f"+CIEV: {pj.CALL},0")
    sys.exit(f"SCO failed: {e}")
send(f"+CIEV: {pj.CALL},0")
print(f"-- captured {len(raw)} bytes", flush=True)

text = transcribe(raw)
print("HEARD:", text or "(nothing)", flush=True)
words = set(re.findall(r"[a-z']+", text.lower()))
if "yes" in words and "no" not in words:
    reply = "Great, thank you. I have noted yes."
elif "no" in words and "yes" not in words:
    reply = "Okay, thank you. I have noted no."
else:
    reply = "Sorry, I did not catch that."
speak(reply)
time.sleep(0.3)
s.close()
