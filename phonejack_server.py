import array
import collections
import concurrent.futures
import json
import os
import re
import select
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import phonejack as pj
import soundfile as sf
from faster_whisper import WhisperModel

ADDR = pj.find_address()
CHANNEL = int(os.environ.get("PHONEJACK_CHANNEL", "1"))
CALLBACK_PREFIX = "http://127.0.0.1:8123/"  # only Home Assistant on this machine
SILENCE_PEAK = 1500  # below this the handset mic is considered silent
REPLIES = {
    "yes": "Great, thank you.",
    "no": "Okay, thank you.",
    "unclear": "Sorry, I did not catch that.",
    "none": "Sorry, I did not hear anything.",
}

# ---- timing guards
# Pause after pickup before the first spoken word. 0 = the original, working timing:
# a pause here left the link idle and the first utterance was lost.
SETTLE_S = float(os.environ.get("PHONEJACK_SETTLE", "0"))
# Silence played before the FIRST utterance of a call (later ones use pj.LEAD_SECONDS).
FIRST_LEAD_S = float(os.environ.get("PHONEJACK_FIRST_LEAD", "2"))
# Reading the Bluetooth control link while audio plays cut the first words off. Keep off
# unless you want instant hang-up detection mid-sentence and the audio proves fine.
PUMP_WHILE_SPEAKING = os.environ.get("PHONEJACK_PUMP_WHILE_SPEAKING", "0") == "1"

# ---- full-duplex mode: speak AND listen over the call's own audio link (SCO).
# The call stays open from pickup (no dial tone, no flip when listening starts) and the
# caller can answer before the question has finished. PHONEJACK_MODE=media restores the
# older "play over media audio, then switch to listening" behaviour.
SCO_MODE = os.environ.get("PHONEJACK_MODE", "sco") == "sco"
SCO_LEAD_S = float(os.environ.get("PHONEJACK_SCO_LEAD", "1.0"))        # silence sent at pickup
BARGE_PEAK = int(os.environ.get("PHONEJACK_BARGE_PEAK", "4000"))      # mic level = caller talking over the question
END_SILENCE_S = float(os.environ.get("PHONEJACK_END_SILENCE", "2.0"))  # quiet this long after speech = finished
MAX_SPEECH_S = float(os.environ.get("PHONEJACK_MAX_SPEECH", "10"))    # cap on one answer
TTS_GAIN = float(os.environ.get("PHONEJACK_TTS_GAIN", "1.0"))
PRE_ROLL_BYTES = 8000   # 0.5 s (8 kHz, 16-bit) kept before the first loud packet
VOICED_NEEDED = 5       # loud packets needed among the last 20 to call it speech
SCO_GUARD_S = 90.0      # a duplex phase that long without finishing is a fault
# Keep listening for a late ATA after the ring window. OFF by default: tested on the real
# Cell2Jack, a handset lifted after the ringing has stopped just gets a dial tone and sends
# no command at all, so there is nothing to hear and the wait only delayed every no-answer.
LATE_ANSWER_S = float(os.environ.get("PHONEJACK_LATE_ANSWER", "0"))
EXPIRED_HANGUP_S = 30.0 # how long to wait for a hang-up after "call expired"
HANGUP_WAIT_S = 60.0    # how long to wait for a hang-up after the reply
COOLDOWN_S = 10.0       # quiet time between two calls
CONNECT_TRIES = 3       # Hands-Free connection attempts
CONNECT_PAUSE_S = 3.0
BT_CONNECT_TRIES = 5    # `bluetoothctl connect` attempts while the controller is busy
HANGUP_PROMPT = "Please hang up now."
EXPIRED_TEXT = "This call has expired. Goodbye."

LOCK = threading.Lock()
LAST_END = 0.0  # monotonic time the previous call finished
# The one Hands-Free connection, kept open between calls (a real phone never drops it:
# closing it after every call left the Cell2Jack refusing new connections).
LINK = None
IO_LOCK = threading.Lock()  # held by whoever is reading the link: a call, or the idle reader
print("loading speech model...", flush=True)
MODEL = WhisperModel("tiny.en", device="cpu", compute_type="int8")
print("ready", flush=True)


class HungUp(Exception):
    """The handset was put down (AT+CHUP) while we were mid-call."""


# ---------------------------------------------------------------- bluetooth link

def _bt(*args, timeout=20):
    return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout)


def _is_connected():
    return "Connected: yes" in _bt("info", ADDR).stdout


def ensure_connected():
    """Make sure the Cell2Jack is connected at the Bluetooth level; raise RuntimeError if not."""
    try:
        if _is_connected():
            return
        for attempt in range(BT_CONNECT_TRIES):
            r = _bt("connect", ADDR, timeout=30)
            out = r.stdout + r.stderr
            if _is_connected():
                time.sleep(2)  # let the services come up
                return
            if "br-connection-busy" not in out:
                break
            print(f"bluetooth busy, retry {attempt + 1}/{BT_CONNECT_TRIES}", flush=True)
            time.sleep(3)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Cell2Jack not connected ({e!r})")
    raise RuntimeError("Cell2Jack not connected")


def open_call():
    """Open the Hands-Free link, retrying a few times (a rapid second request can fail once)."""
    err = ""
    for rnd in range(2):
        for i in range(CONNECT_TRIES):
            try:
                return Call()
            except SystemExit as e:
                err = str(e)
                print(f"hands-free connect {i + 1}/{CONNECT_TRIES} failed: {err}", flush=True)
                if i < CONNECT_TRIES - 1:
                    time.sleep(CONNECT_PAUSE_S)
        if rnd == 0:
            # `Connected: yes` can be stale: the adapter then ignores RFCOMM until the
            # Bluetooth link is cycled (verified by hand: disconnect + connect fixed it).
            print("forcing a Bluetooth disconnect/reconnect", flush=True)
            try:
                _bt("disconnect", ADDR, timeout=15)
                time.sleep(3)
                _bt("connect", ADDR, timeout=30)
                time.sleep(3)
            except (OSError, subprocess.SubprocessError) as e:
                print("bluetooth cycle failed:", repr(e), flush=True)
    raise RuntimeError(err or "Hands-Free connection failed")


def get_call():
    """Return (call, fresh). Reuses the open link; opens a new one only if there is none."""
    global LINK
    if LINK is not None:
        LINK.answered = False
        LINK.hung_up = False
        LINK.spoke = False
        return LINK, False
    LINK = open_call()
    return LINK, True


def discard_link():
    global LINK
    if LINK is not None:
        try:
            LINK.s.close()
        except OSError:
            pass
        LINK = None
        print("control link discarded", flush=True)


def idle_reader():
    """Between calls keep answering the handset's commands and notice if the link drops."""
    while True:
        if IO_LOCK.acquire(timeout=0.2):
            try:
                if LINK is not None:
                    try:
                        LINK.pump()
                    except OSError as e:
                        print("control link lost while idle:", repr(e), flush=True)
                        discard_link()
                else:
                    time.sleep(0.3)
            finally:
                IO_LOCK.release()
            time.sleep(0.05)  # let a waiting call take the lock


# ---------------------------------------------------------------- the call

class Call:
    def __init__(self):
        self.s, self.buf = pj.find_handsfree(ADDR, CHANNEL, lambda *_: None)
        self.s.settimeout(0.3)
        self.ready_at = None
        self.answered = False
        self.hung_up = False
        self.spoke = False
        self.sco = None      # duplex audio link, open only in SCO mode
        self.out = b""       # audio waiting to be sent to the handset (8 kHz s16 mono)
        self.out_pos = 0

    def send(self, line):
        self.s.sendall(b"\r\n" + line.encode() + b"\r\n")

    def handle(self, cmd):
        print("  <<", cmd, flush=True)
        up = cmd.upper()
        if up.startswith("AT+BRSF="):
            self.send("+BRSF: 0")
        elif up == "AT+CIND=?":
            self.send(pj.CIND_TEST)
        elif up == "AT+CIND?":
            self.send(pj.CIND_NOW)
        elif up.startswith("AT+CMER"):
            self.ready_at = time.monotonic() + 1.5
        elif up == "AT+CHLD=?":
            self.send("+CHLD: (0,1,2,3)")
        elif up == "AT+COPS?":
            self.send('+COPS: 0,0,"PhoneJack"')
        elif up == "AT+CLCC":
            pass
        elif up == "ATA":
            self.answered = True
        elif up == "AT+CHUP":
            self.hung_up = True
        elif up.startswith(("AT+XAPL", "AT+IPHONEACCEV", "AT+XEVENT", "AT+ANDROID", "AT+BIND")):
            self.send("ERROR")
            return
        self.send("OK")

    def pump(self):
        try:
            data = self.s.recv(1024)
            if not data:
                raise ConnectionError("Cell2Jack closed the connection")
            self.buf += data
        except TimeoutError:
            pass
        while b"\r" in self.buf:
            line, self.buf = self.buf.split(b"\r", 1)
            line = line.strip().decode(errors="replace")
            if line:
                self.handle(line)

    def check(self):
        """Raise HungUp if the handset has been put down."""
        if self.hung_up:
            raise HungUp()

    def handshake(self):
        deadline = time.monotonic() + 15
        while not (self.ready_at and time.monotonic() >= self.ready_at):
            if time.monotonic() > deadline:
                raise TimeoutError("handshake with the Cell2Jack did not complete")
            self.pump()

    def ring(self, seconds):
        """Return 'answered', 'answered_late' or 'no_answer'."""
        self.send(f"+CIEV: {pj.CALLSETUP},1")
        until = time.monotonic() + seconds
        nxt = 0
        while not self.answered and not self.hung_up and time.monotonic() < until:
            if time.monotonic() >= nxt:
                self.send("RING")
                self.send('+CLIP: "5550100",129')
                nxt = time.monotonic() + 3
            self.pump()
        if self.answered:
            return "answered"
        self.send(f"+CIEV: {pj.CALLSETUP},0")
        if self.hung_up:
            return "no_answer"
        # Someone may still lift the handset just after the ring window; if we closed
        # the setup and ignored it, the handset would be left off-hook with no call.
        late_end = time.monotonic() + LATE_ANSWER_S
        while not self.answered and time.monotonic() < late_end:
            self.pump()
        return "answered_late" if self.answered else "no_answer"

    def pickup(self):
        self.hung_up = False  # ignore any stale hang-up from before the pickup
        self.send(f"+CIEV: {pj.CALL},1")
        self.send(f"+CIEV: {pj.CALLSETUP},0")
        time.sleep(0.5)
        self.send(f"+CIEV: {pj.CALL},0")  # drop the fake call so media audio can play
        time.sleep(SETTLE_S)

    def speak(self, text):
        """Speak text; stop at once and raise HungUp if the handset is put down."""
        self.check()
        print("speaking:", text, flush=True)
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            subprocess.run(["espeak-ng", "-w", tmp, text], check=True)
            data, rate = sf.read(tmp, dtype="float32", always_2d=True)
            lead_s = pj.LEAD_SECONDS if self.spoke else FIRST_LEAD_S
            self.spoke = True
            lead = np.zeros((int(rate * lead_s), data.shape[1]), dtype="float32")
            tail = np.zeros((int(rate * 0.5), data.shape[1]), dtype="float32")
            sf.write(tmp, np.concatenate([lead, data, tail]), rate)
            proc = subprocess.Popen(["pw-play", "--target", pj.SINK, tmp])
            try:
                if PUMP_WHILE_SPEAKING:
                    while proc.poll() is None:
                        self.pump()  # paced by the 0.3 s socket timeout
                        if self.hung_up:
                            proc.terminate()
                            proc.wait()
                            raise HungUp()
                else:
                    # Like the original working scripts: leave the control link alone
                    # while audio plays. A hang-up is read right afterwards.
                    proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
            if proc.returncode:
                raise RuntimeError(f"pw-play exited with {proc.returncode}")
            self.pump()
            self.pump()
            self.check()
        finally:
            os.remove(tmp)

    def listen(self, seconds):
        self.check()
        self.send(f"+CIEV: {pj.CALL},1")
        time.sleep(0.5)
        sco = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO)
        try:
            sco.settimeout(5)
            sco.connect(ADDR)
            chunks = []
            end = time.monotonic() + seconds
            while time.monotonic() < end and not self.hung_up:
                ready, _, _ = select.select([sco, self.s], [], [], 0.2)
                if sco in ready:
                    try:
                        d = sco.recv(1024)
                    except OSError:
                        break
                    if not d:
                        break
                    chunks.append(d)
                if self.s in ready:
                    self.pump()
        finally:
            sco.close()
            try:
                self.send(f"+CIEV: {pj.CALL},0")
            except OSError:
                pass
        return b"".join(chunks)

    # ---- full duplex over SCO ------------------------------------------------
    # Every mic packet we receive is answered with one packet of outgoing audio (or
    # silence), so the handset's own clock paces playback and no timers are needed.

    def open_sco(self):
        self.hung_up = False  # ignore any stale hang-up from before the pickup
        self.send(f"+CIEV: {pj.CALL},1")  # the call stays open for the whole conversation
        self.send(f"+CIEV: {pj.CALLSETUP},0")
        time.sleep(0.3)
        err = None
        for _ in range(3):
            sco = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO)
            try:
                sco.settimeout(5)
                sco.connect(ADDR)
                sco.settimeout(0.2)
                self.sco = sco
                return
            except OSError as e:
                err = e
                sco.close()
                time.sleep(0.5)
        raise RuntimeError(f"SCO connect failed: {err!r}")

    def close_sco(self):
        if self.sco:
            self.sco.close()
            self.sco = None
        try:
            self.send(f"+CIEV: {pj.CALL},0")
        except OSError:
            pass

    def say(self, pcm):
        self.out = pcm
        self.out_pos = 0

    @property
    def playing(self):
        return self.out_pos < len(self.out)

    def sco_step(self):
        """Service the control link; return one mic packet (after sending one out), or None."""
        ready, _, _ = select.select([self.sco, self.s], [], [], 0.2)
        if self.s in ready:
            self.pump()
        if self.sco not in ready:
            return None
        try:
            d = self.sco.recv(1024)
        except OSError:
            d = b""
        if not d:
            print("SCO closed from the handset side: treating as hang-up", flush=True)
            self.hung_up = True
            return None
        n = len(d)
        chunk = self.out[self.out_pos:self.out_pos + n]
        self.out_pos += n
        if len(chunk) < n:
            chunk += b"\x00" * (n - len(chunk))
        try:
            self.sco.send(chunk)
        except (BlockingIOError, TimeoutError):
            pass
        return d

    def converse(self, question_pcm, listen_s):
        """Play the question while listening. Return the caller's speech (b'' if there was none).

        Speech starts when enough loud packets arrive (a higher bar while the question is
        playing, so the earpiece leaking into the mic is not mistaken for the caller) and
        ends END_SILENCE_S after the last loud packet. If the caller starts talking over the
        question, the question is cut off.
        """
        self.say(b"\x00" * (int(8000 * SCO_LEAD_S) * 2) + question_pcm)
        voiced = collections.deque(maxlen=20)
        captured = bytearray()
        start_off = None
        last_voice = speech_start = play_end = None
        play_peak = 0
        guard = time.monotonic() + SCO_GUARD_S
        while True:
            self.check()
            d = self.sco_step()
            now = time.monotonic()
            if now > guard:
                raise TimeoutError("no audio flowing over the call link")
            if d is not None:
                captured += d
                a = array.array("h")
                a.frombytes(d[: len(d) // 2 * 2])
                peak = max(map(abs, a), default=0)
                playing = self.playing
                if playing:
                    play_peak = max(play_peak, peak)
                loud = peak >= (BARGE_PEAK if playing else SILENCE_PEAK)
                voiced.append(loud)
                if loud:
                    last_voice = now
                if start_off is None and sum(voiced) >= VOICED_NEEDED:
                    start_off = max(0, len(captured) - 20 * len(d) - PRE_ROLL_BYTES)
                    speech_start = now
                    if playing:
                        print("caller talked over the question: cutting it off", flush=True)
                        self.out_pos = len(self.out)
                if play_end is None and not self.playing:
                    play_end = now
            if start_off is not None:
                if now - last_voice >= END_SILENCE_S or now - speech_start >= MAX_SPEECH_S:
                    break
            elif play_end is not None and now - play_end >= listen_s:
                break
        print(f"mic peak while the question played: {play_peak} (barge-in bar {BARGE_PEAK})", flush=True)
        return bytes(captured[start_off:]) if start_off is not None else b""

    def speak_sco(self, pcm, tail_s=0.5):
        """Play pcm over the call link, then a short quiet tail. Raises HungUp if they hang up."""
        self.say(pcm + b"\x00" * (int(8000 * tail_s) * 2))
        guard = time.monotonic() + SCO_GUARD_S
        while self.playing:
            self.check()
            self.sco_step()
            if time.monotonic() > guard:
                raise TimeoutError("no audio flowing over the call link")
        self.check()

    def understand_connected(self, raw):
        """Transcribe in a worker thread while the call link keeps being serviced."""
        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            fut = ex.submit(understand, raw)
            while not fut.done():
                self.sco_step()
            return fut.result()

    def wait_hangup_sco(self, seconds):
        end = time.monotonic() + seconds
        while not self.hung_up and time.monotonic() < end:
            try:
                self.sco_step()
            except OSError:
                break
        return self.hung_up

    def wait_hangup(self, seconds):
        """Re-open the call state and wait for AT+CHUP. Returns True if the handset was put down."""
        if self.hung_up:
            return True
        try:
            self.send(f"+CIEV: {pj.CALL},1")
            end = time.monotonic() + seconds
            while not self.hung_up and time.monotonic() < end:
                self.pump()
            self.send(f"+CIEV: {pj.CALL},0")
        except OSError:
            pass
        return self.hung_up

    def close(self):
        time.sleep(0.3)
        self.s.close()


def understand(raw):
    """Return (heard_text, 'yes' | 'no' | 'unclear' | 'none')."""
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    peak = max((abs(x) for x in a), default=0)
    print(f"captured {len(raw)} bytes, peak {peak}", flush=True)
    if peak < SILENCE_PEAK:
        return "", "none"
    data = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype("float32") / 32768.0
    n = int(len(data) * 2)  # 8 kHz -> 16 kHz
    audio = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data).astype("float32")
    segments, _ = MODEL.transcribe(audio, language="en")
    text = " ".join(seg.text.strip() for seg in segments)
    words = set(re.findall(r"[a-z']+", text.lower()))
    if "yes" in words and "no" not in words:
        return text, "yes"
    if "no" in words and "yes" not in words:
        return text, "no"
    return text, "unclear"


def tts_pcm(text):
    """Speak text with espeak-ng and return it as 8 kHz 16-bit mono PCM for the call link."""
    print("speaking:", text, flush=True)
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(["espeak-ng", "-w", tmp, text], check=True)
        data, rate = sf.read(tmp, dtype="float32", always_2d=True)
    finally:
        os.remove(tmp)
    mono = data.mean(axis=1)
    k = int(round(rate / 8000))
    if k > 1:  # crude low-pass before dropping to 8 kHz
        mono = np.convolve(mono, np.ones(k, dtype="float32") / k, mode="same")
    n = int(len(mono) * 8000 / rate)
    out = np.interp(np.linspace(0, len(mono) - 1, n), np.arange(len(mono)), mono)
    return (np.clip(out * TTS_GAIN, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def run_sco_call(c, outcome, question, say, listen_s, hangup_wait=HANGUP_WAIT_S):
    """The conversation after pickup, entirely over the call's audio link."""
    lead = b"\x00" * (int(8000 * SCO_LEAD_S) * 2)
    try:
        c.open_sco()
        if outcome == "answered_late":
            try:
                c.speak_sco(lead + tts_pcm(EXPIRED_TEXT))
            except HungUp:
                return {"status": "answered_late", "hung_up": True}
            return {"status": "answered_late", "hung_up": c.wait_hangup_sco(EXPIRED_HANGUP_S)}

        try:
            if question:
                raw = c.converse(tts_pcm(question), listen_s)
                c.check()  # hung up while listening: skip transcription and reply
                heard, answer = c.understand_connected(raw)
                print("heard:", heard or "(nothing)", "->", answer, flush=True)
                result = {"status": "answered", "heard": heard, "answer": answer}
                if c.hung_up:  # hung up while we were transcribing: nothing to say to them
                    result["hung_up"] = True
                    return result
                closing = REPLIES[answer]
                lead_in = b""
            else:
                result = {"status": "answered"}
                closing = say
                lead_in = lead
        except HungUp:
            return {"status": "hung_up_early"}

        try:
            c.speak_sco(lead_in + tts_pcm((closing + " " + HANGUP_PROMPT).strip()))
        except HungUp:
            pass  # hung up during the reply: fine, that is what we asked for
        result["hung_up"] = c.wait_hangup_sco(hangup_wait)
        return result
    finally:
        c.close_sco()


def run_call(question, say, ring_s, listen_s, hangup_wait=HANGUP_WAIT_S):
    with IO_LOCK:
        result = _run_call(question, say, ring_s, listen_s, hangup_wait)
        if result.get("status") == "failed":
            discard_link()  # next request opens a fresh link
        return result


def _run_call(question, say, ring_s, listen_s, hangup_wait=HANGUP_WAIT_S):
    try:
        if LINK is None:
            ensure_connected()
        c, fresh = get_call()
    except (RuntimeError, SystemExit, OSError) as e:
        return {"status": "failed", "error": str(e) if isinstance(e, (RuntimeError, SystemExit)) else repr(e)}
    try:
        if fresh:
            c.handshake()
        outcome = c.ring(ring_s)
        if outcome == "no_answer":
            return {"status": "no_answer"}
        if SCO_MODE:
            return run_sco_call(c, outcome, question, say, listen_s, hangup_wait)
        c.pickup()

        if outcome == "answered_late":
            try:
                c.speak(EXPIRED_TEXT)
            except HungUp:
                return {"status": "answered_late", "hung_up": True}
            return {"status": "answered_late", "hung_up": c.wait_hangup(EXPIRED_HANGUP_S)}

        try:
            if question:
                c.speak(question)
                raw = c.listen(listen_s)
                c.check()  # hung up while listening: skip transcription and reply
                heard, answer = understand(raw)
                print("heard:", heard or "(nothing)", "->", answer, flush=True)
                result = {"status": "answered", "heard": heard, "answer": answer}
                closing = REPLIES[answer]
            else:
                result = {"status": "answered"}
                closing = say
        except HungUp:
            return {"status": "hung_up_early"}

        try:
            c.speak((closing + " " + HANGUP_PROMPT).strip())
        except HungUp:
            pass  # hung up during the reply: fine, that is what we asked for
        result["hung_up"] = c.wait_hangup(hangup_wait)
        return result
    except (Exception, SystemExit) as e:
        return {"status": "failed", "error": repr(e)}
    # the control link stays open for the next call (see LINK)


def post_callback(url, result):
    try:
        req = urllib.request.Request(
            url, data=json.dumps(result).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        print("callback sent", flush=True)
    except Exception as e:
        print("callback failed:", repr(e), flush=True)


def job(body):
    global LAST_END
    try:
        # cooldown: a request that arrives right after a call waits for the link to settle
        wait = LAST_END + COOLDOWN_S - time.monotonic()
        if wait > 0:
            print(f"cooldown {wait:.1f}s", flush=True)
            time.sleep(wait)
        result = run_call(body["question"], body["say"], body["ring_seconds"], body["listen_seconds"],
                          body["hangup_wait"])
        result["id"] = body["id"]
        print("RESULT:", json.dumps(result), flush=True)
        if body["callback"]:
            post_callback(body["callback"], result)
    finally:
        LAST_END = time.monotonic()
        LOCK.release()


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = json.loads(self.rfile.read(n) or b"{}")
            body = {
                "question": str(raw.get("question", "")),
                "say": str(raw.get("say", "")),
                "id": str(raw.get("id", "")),
                "callback": str(raw.get("callback", "")),
                "ring_seconds": float(raw.get("ring_seconds", 40)),
                "listen_seconds": float(raw.get("listen_seconds", 6)),
                "hangup_wait": float(raw.get("hangup_wait_seconds", HANGUP_WAIT_S)),
            }
            if body["callback"] and not body["callback"].startswith(CALLBACK_PREFIX):
                raise ValueError("callback must start with " + CALLBACK_PREFIX)
        except Exception as e:
            print("bad request:", repr(e), flush=True)
            self.send_response(400)
            self.end_headers()
            return
        if not LOCK.acquire(blocking=False):
            self.send_response(409)  # a call is already in progress
            self.end_headers()
            return
        threading.Thread(target=job, args=(body,), daemon=True).start()
        self.send_response(202)
        self.end_headers()

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


if __name__ == "__main__":
    threading.Thread(target=idle_reader, daemon=True).start()
    HTTPServer(("127.0.0.1", 8765), H).serve_forever()
