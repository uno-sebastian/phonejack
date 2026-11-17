import sys
import time

import phonejack as pj

HOLD = float(sys.argv[1]) if len(sys.argv) > 1 else 60

s, buf = pj.find_handsfree(pj.find_address(), 1, print)
s.settimeout(0.3)
st = {"ready_at": None, "answered": False, "hung_up": False}


def send(line):
    print("  >>", line, flush=True)
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
        if st["answered"]:
            send('+CLCC: 1,1,0,0,0,"5550100",129')
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
            print("-- Cell2Jack closed the connection")
            sys.exit()
        buf += data
    except TimeoutError:
        pass
    while b"\r" in buf:
        line, buf = buf.split(b"\r", 1)
        line = line.strip().decode(errors="replace")
        if line:
            handle(line)


while not (st["ready_at"] and time.monotonic() >= st["ready_at"]):
    pump()
send(f"+CIEV: {pj.CALLSETUP},1")
nxt = 0
end = time.monotonic() + 40
while not st["answered"] and time.monotonic() < end:
    if time.monotonic() >= nxt:
        send("RING")
        send('+CLIP: "5550100",129')
        nxt = time.monotonic() + 3
    pump()
if not st["answered"]:
    send(f"+CIEV: {pj.CALLSETUP},0")
    sys.exit("not answered")

send(f"+CIEV: {pj.CALL},1")
send(f"+CIEV: {pj.CALLSETUP},0")
print(f"-- call is OPEN for {HOLD:.0f}s: press keys, flash the hook, hang up, speak", flush=True)
import array
import select
import socket
import wave

try:
    sco = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO)
    sco.settimeout(5)
    sco.connect(pj.find_address())
    print("-- SCO connected: SPEAK NOW into the handset (count one to ten)", flush=True)
    sco.settimeout(0.2)
    chunks = []
    times = []
    t0 = time.monotonic()
    stop = time.monotonic() + 10
    while time.monotonic() < stop:
        try:
            ready, _, _ = select.select([sco, s], [], [], 0.2)
            if sco in ready:
                d = sco.recv(1024)
                if not d:
                    print(f"-- SCO closed by peer at +{time.monotonic() - t0:.2f}s", flush=True)
                    break
                chunks.append(d)
                times.append(time.monotonic() - t0)
            if s in ready:
                pump()
        except OSError as e:
            print("-- SCO recv error:", e, flush=True)
            break
    sco.close()
    raw = b"".join(chunks)
    with wave.open("sco_capture.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(raw)
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    peak = max((abs(x) for x in a), default=0)
    print(f"-- captured {len(raw)} bytes, peak amplitude {peak} of 32767", flush=True)
    if times:
        print(f"-- packets: {len(chunks)}, sizes: {sorted(set(len(c) for c in chunks))}, "
              f"first at {times[0]:.2f}s, last at {times[-1]:.2f}s", flush=True)
    else:
        print("-- no packets received at all", flush=True)
except OSError as e:
    print("-- SCO failed:", e, flush=True)

end = time.monotonic() + HOLD
while time.monotonic() < end and not st["hung_up"]:
    pump()
send(f"+CIEV: {pj.CALL},0")
print("-- done", flush=True)
s.close()
