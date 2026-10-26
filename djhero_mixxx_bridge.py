#!/usr/bin/env python3
# ^ "shebang": lets you run this file directly (./djhero_mixxx_bridge.py) with python3 yoc can past python3 djhero_mixxx_bridge.py in the terminal.
"""PS3 DJ Hero turntable (12ba:0140) -> virtual MIDI port "DJ Hero" for Mixxx.

HOW IT WORKS
  The turntable is a USB HID device. Linux exposes its raw data as /dev/hidrawN.
  This script reads every 27-byte report from that file, works out what changed
  (buttons, d-pad, platter, crossfader, knob) and sends the matching MIDI messages
  on a virtual MIDI port called "DJ Hero". Mixxx listens to that port and the
  mapping files (DJ-Hero-Bridge.midi.xml + DJ-Hero-Bridge.js) turn the MIDI into actions.

SETTINGS PAGE
  The bridge also serves a settings page on your own computer (http://127.0.0.1:8765/).
  Every adjustable value is there and changes apply instantly, but they are only kept
  after a restart if you press SAVE on the page (saved in ~/.config/djhero_settings.json).
  Bridge-side values are used right here; Mixxx-side values are sent to Mixxx over MIDI
  (channel 2), so you never have to edit the JS file.

FILES THAT MUST SIT TOGETHER IN ONE FOLDER
  djhero_mixxx_bridge.py   this file
  djhero_ui.html           the settings page
  dj-hero-logo.png         the logo used by the settings page
  dj-hero-stickers.png     the sticker photo used by the settings page
  (and in ~/.mixxx/controllers/: DJ-Hero-Bridge.js and DJ-Hero-Bridge.midi.xml)

Byte layout taken from a real probe of this device:
  bytes 0-1   button bits: bit0 square, bit1 X, bit2 circle, bit3 triangle/euphoria,
              bit8 select, bit9 start, bit12 PS.  The three platter buttons set the SAME
              bits as square/X/circle, so they are told apart by byte 23:
              0x10 platter green, 0x20 platter red, 0x40 platter blue.
  byte 2      d-pad (hat, low nibble 0..7, other = released)
  byte 5      platter velocity, 0x80 = still, above = clockwise
  bytes 19-20 effects knob, 10-bit endless;  bytes 21-22 crossfader, 10-bit (0 = left)
  byte 11     pressure byte that goes to 0xFF with euphoria

MIDI out, channel 1 (the controls):
  notes  1 square, 2 X, 3 circle, 4 triangle   (base face buttons)
         40 platter green, 41 platter red, 42 platter blue, 50 euphoria
         9 select, 10 start, 13 PS, 20 d-pad up, 21 right, 22 down, 23 left
  CC1 platter SPEED (64 = still, above = clockwise, platter_res steps per count)
  CC3 crossfader 0..127 | CC4 knob (relative, 64 = still)
MIDI out, channel 2 (the settings): CC number = setting id, see REGISTRY below.

  python3 djhero_mixxx_bridge.py                run the bridge (splash + settings page)
  python3 djhero_mixxx_bridge.py --no-splash    skip the startup animation
  python3 djhero_mixxx_bridge.py --logo-rows 30 limit the startup logo to 30 terminal rows (default: as big as fits)
  python3 djhero_mixxx_bridge.py --no-browser   do not open the settings page automatically
  python3 djhero_mixxx_bridge.py --ui-only      only the settings page, no turntable needed
  python3 djhero_mixxx_bridge.py --watch        press buttons and see which one the bridge sees
  python3 djhero_mixxx_bridge.py --learn        teach it the difference between TRIANGLE and EUPHORIA
"""
import base64, glob, json, os, random, select, shutil, socket, sys, threading, time, webbrowser, zlib
#   glob: find files | json: settings and learned rule | os: raw file access
#   random: splash animation | select: wait for data | shutil: terminal size
#   sys: arguments and exit | threading: web page and heartbeat run beside the main loop
#   base64, zlib: unpack the compressed logo for the startup animation | socket: fast replies for the web page
#   time: timing | webbrowser: open the settings page
from collections import Counter, deque                        # Counter: --learn | deque: platter smoothing, event log
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer   # the small web server for the settings page

HERE = os.path.dirname(os.path.abspath(__file__))   # folder this script lives in (the web files are here too)

# USB vendor/product id of the turntable (12ba:0140), written the way the kernel prints
# them in /sys/class/hidraw/*/device/uevent (8 hex digits each, upper case).
VID, PID = "000012BA", "00000140"
REPORT_LEN = 27        # every report from the turntable is exactly 27 bytes long
RULE_FILE = os.path.expanduser("~/.config/djhero_triangle.json")      # where --learn saves its result
SETTINGS_FILE = os.path.expanduser("~/.config/djhero_settings.json")  # where your settings are saved
RAWB = [0, 1, 2] + list(range(7, 19)) + [23]   # bytes that carry buttons
UI_PORT = 8765         # settings page port (the next free one is used if this is taken)

# Friendly names for each MIDI note number (used by --watch and the settings page).
NOTE_NAMES = {1: "SQUARE", 2: "X", 3: "CIRCLE", 4: "TRIANGLE",
              40: "platter GREEN", 41: "platter RED", 42: "platter BLUE", 50: "EUPHORIA",
              9: "SELECT", 10: "START", 13: "PS",
              20: "D-pad UP", 21: "D-pad RIGHT", 22: "D-pad DOWN", 23: "D-pad LEFT"}

# ====================================================================================
#  SETTINGS REGISTRY: every adjustable value, in one place.
#  kind: "bool", "int", "float" or "mask" (a set of tick boxes stored as a number).
#  id:   None = used only in this script. A number = also sent to Mixxx as CC <id> on
#        MIDI channel 2. The SAME ids and ranges are in DJ-Hero-Bridge.js (SETTINGS table)
#        and DJ-Hero-Bridge.midi.xml, so do not change an id without changing those too.
# ====================================================================================
G_SCRATCH = "Platter: scratch feel (applied in Mixxx)"
G_SENSOR = "Platter: sensor (applied in the bridge)"
G_KNOB = "Effects knob and effect list"
G_XFADE = "Crossfader"
G_TEMPO = "Tempo (d-pad up / down)"
G_CUES = "Hot cues and loops"


def _d(key, group, label, kind, default, lo=None, hi=None, step=None, mid=None, help="", options=None):
    """Build one registry entry (just a dictionary, so it can be sent to the web page as JSON)."""
    return dict(key=key, group=group, label=label, kind=kind, default=default,
                min=lo, max=hi, step=step, id=mid, help=help, options=options)


REGISTRY = [
    _d("direct_scratch", G_SCRATCH, "Direct scratch mode", "bool", True, mid=1,
       help="On = the platter sets the scratch speed directly (least delay). Off = Mixxx's smoothed scratch filter."),
    _d("rate_scale", G_SCRATCH, "Scratch speed", "float", 0.35, 0.05, 1.5, 0.01, 2,
       "How fast the track scratches for a slow platter move. Raise if sluggish, lower if it scrubs too much."),
    _d("rate_power", G_SCRATCH, "Speed curve", "float", 0.7, 0.3, 1.2, 0.05, 3,
       "Below 1 = slow moves respond more and fast moves are softened. 1 = straight line."),
    _d("max_rate", G_SCRATCH, "Fastest scratch", "float", 3.0, 1.0, 10.0, 0.5, 4,
       "The track never scrubs faster than this many times normal speed."),
    _d("release_ms", G_SCRATCH, "Hold after stopping (ms)", "int", 60, 0, 300, 5, 5,
       "After the platter stops, the record is held this long before it lets go. 0 = let go instantly."),
    _d("tick_scale", G_SCRATCH, "Filtered mode: speed", "float", 0.2, 0.05, 1.0, 0.05, 6,
       "Only used when Direct scratch mode is off."),
    _d("alpha", G_SCRATCH, "Filtered mode: follow speed", "float", 0.25, 0.05, 1.0, 0.05, 7,
       "Only used when Direct scratch mode is off. Higher = follows the platter faster but is jumpier."),

    _d("platter_gain", G_SENSOR, "Platter sensitivity", "float", 1.0, 0.2, 3.0, 0.1, None,
       "Multiplies the platter movement before it is sent to Mixxx."),
    _d("platter_res", G_SENSOR, "Platter detail (MIDI steps per count)", "int", 3, 1, 8, 1, 8,
       "Higher = finer slow speeds. Sent to Mixxx automatically so both sides always match."),
    _d("platter_deadzone", G_SENSOR, "Platter dead zone", "int", 0, 0, 4, 1, None,
       "Movements this small are ignored. 0 is fine: the platter reads exactly still when untouched."),
    _d("platter_deadzone_buttons", G_SENSOR, "Dead zone while a platter button is held", "int", 3, 0, 6, 1, None,
       "Pressing a platter button wobbles the platter a little; this ignores that wobble."),
    _d("platter_smooth_ms", G_SENSOR, "Smoothing (ms)", "int", 24, 0, 80, 2, None,
       "Averages the platter over this time so slow movement is steady. 0 = off."),
    _d("platter_send_hz", G_SENSOR, "Max platter messages per second", "int", 500, 100, 1000, 50, None,
       "Limits how often platter speed is sent. Starting and stopping are always sent instantly."),
    _d("invert_platter", G_SENSOR, "Reverse platter direction", "bool", False, mid=None,
       help="Turn on if clockwise scratches backwards."),

    _d("knob_step", G_KNOB, "Effect knob sensitivity", "float", 0.02, 0.005, 0.1, 0.005, 9,
       "How much the selected effect or filter changes per knob step."),
    _d("knob_div", G_KNOB, "Knob counts per step", "int", 16, 4, 64, 2, None,
       "Higher = you must turn the knob further for each step (slower)."),
    _d("knob_noise", G_KNOB, "Knob jitter filter", "int", 6, 1, 20, 1, None,
       "Ignores knob wobble smaller than this."),
    _d("invert_knob", G_KNOB, "Reverse knob direction", "bool", False, mid=None,
       help="Turn on if turning clockwise lowers the effect."),
    _d("fx_mask", G_KNOB, "Effects in the Euphoria list", "mask", 31, 0, 31, 1, 10,
       "Tick the effects that Euphoria steps through.",
       ["Filter deck 1", "Filter deck 2", "Echo", "Phaser", "Reverb"]),

    _d("invert_crossfader", G_XFADE, "Reverse crossfader", "bool", False, mid=None,
       help="Turn on if the crossfader is the wrong way round."),

    _d("tempo_invert", G_TEMPO, "Reverse tempo buttons", "bool", False, mid=11,
       help="Turn on if d-pad up slows the track down."),
    _d("tempo_repeat_delay", G_TEMPO, "Hold before repeating (ms)", "int", 400, 100, 1000, 50, 12,
       "How long you hold d-pad up / down before the tempo keeps changing by itself."),
    _d("tempo_repeat_ms", G_TEMPO, "Repeat speed (ms per step)", "int", 100, 30, 400, 10, 13,
       "Smaller = the tempo changes faster while you hold the button."),

    _d("ps_hold_ms", G_CUES, "PS hold time for loops (ms)", "int", 150, 50, 600, 10, 14,
       "Hold PS longer than this, then press X / Circle / Square for the loop shortcuts."),
    _d("sticky_enabled", G_CUES, "Sticky hot cues", "bool", True, mid=15,
       help="Hold a hot cue button (Circle, X, Square, Triangle) long enough and it stays held after you let go. Press it again to release."),
    _d("sticky_hold_ms", G_CUES, "Sticky hold time (ms)", "int", 400, 150, 1500, 50, 16,
       "How long you must hold a hot cue button before it turns sticky."),
]






def _coerce(d, val):
    """Turn whatever the web page sent into a valid value for setting `d` (clamped to its range)."""
    kind = d["kind"]
    if kind == "bool":
        return bool(val)
    if kind == "mask":
        return max(0, min((1 << len(d["options"])) - 1, int(val)))
    val = max(d["min"], min(d["max"], float(val)))          # keep inside min..max
    return int(round(val)) if kind == "int" else float(val)


class Settings:
    """All current setting values. Read with S["key"].

    Changes take effect right away (live) but are NOT written to disk until you press SAVE on the
    settings page. `saved` remembers what is on disk, so the page can tell you when there are unsaved
    changes and can undo them.
    """

    def __init__(self):
        self.defs = {d["key"]: d for d in REGISTRY}              # key -> description
        self.v = {d["key"]: d["default"] for d in REGISTRY}      # key -> current value
        self.lock = threading.Lock()                             # the web page and the main loop both touch this
        self.load()
        self.saved = dict(self.v)                                # what is on disk now (the defaults if no file)

    def __getitem__(self, key):
        return self.v[key]

    def snapshot(self):
        with self.lock:
            return dict(self.v)

    def dirty(self):
        """True if the live values differ from what is saved on disk."""
        with self.lock:
            return self.v != self.saved

    def load(self):
        """Read the saved settings (if any) on top of the defaults."""
        try:
            saved = json.load(open(SETTINGS_FILE))
        except Exception:
            return                                                # no file yet: keep the defaults
        for k, val in saved.items():
            if k in self.defs:
                try:
                    self.v[k] = _coerce(self.defs[k], val)
                except (TypeError, ValueError):
                    pass                                          # ignore a damaged value

    def save(self):
        """Write all current values to disk (through a temp file so a crash cannot damage the real one).
        Raises OSError if the file cannot be written."""
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        snap = self.snapshot()
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        with self.lock:
            self.saved = snap                                     # now what is live is what is on disk

    def update(self, changes):
        """Apply {key: value} live (not saved). Returns the list of keys whose value really changed."""
        changed = []
        with self.lock:
            for k, val in changes.items():
                d = self.defs.get(k)
                if d is None:
                    continue                                      # unknown key: ignore
                try:
                    nv = _coerce(d, val)
                except (TypeError, ValueError):
                    continue
                if self.v[k] != nv:
                    self.v[k] = nv
                    changed.append(k)
        return changed

    def reset(self):
        """Put every value back to its default (live, not saved)."""
        with self.lock:
            self.v = {d["key"]: d["default"] for d in REGISTRY}

    def revert(self):
        """Throw away unsaved changes: go back to what is saved on disk."""
        with self.lock:
            self.v = dict(self.saved)


S = Settings()   # the one shared settings object


class Live:
    """What the turntable is doing right now. The Bridge writes it, the settings page reads it."""

    def __init__(self):
        self.platter = 0              # last platter value sent (-63..63)
        self.xfader = 63              # last crossfader value sent (0..127)
        self.knob_turn = 0            # total knob steps so far (turns the on-screen dial)
        self.pressed = set()          # names of the buttons held right now
        self.events = deque(maxlen=12)   # recent button events as (time, text)
        self.node = None              # e.g. /dev/hidraw5
        self.reports = 0              # how many reports have arrived
        self.last_report = 0.0        # when the last one arrived
        self.last_push = 0.0          # when settings were last sent to Mixxx
        self.midi_open = False        # True once the virtual MIDI port exists

    def snapshot(self, settings):
        """A JSON-friendly copy for the web page."""
        for _ in range(3):            # a set/deque can change while we copy it; just try again
            try:
                pressed, events = sorted(self.pressed), list(self.events)
                break
            except RuntimeError:
                pressed, events = [], []
        now = time.time()
        return {"platter": self.platter, "xfader": self.xfader, "knob_turn": self.knob_turn,
                "pressed": pressed, "events": events[::-1],
                "buttons": list(NOTE_NAMES.values()),
                "node": self.node, "connected": (now - self.last_report) < 2.0,
                "reports": self.reports, "midi_open": self.midi_open,
                "push_age": (now - self.last_push) if self.last_push else None,
                "knob_div": settings["knob_div"], "dirty": settings.dirty()}


LIVE = Live()











# ------------------- sending settings to Mixxx (MIDI channel 2) -------------------
MIDI_SEND = None   # set in main() once the MIDI port is open: a function send(type, number, value)


def encode(d, val):
    """Setting value -> MIDI value 0..127 (Mixxx turns it back into the real number)."""
    if d["kind"] == "bool":
        return 127 if val else 0
    if d["kind"] == "mask":
        return int(val) & 127                                     # a small number, sent as it is
    return max(0, min(127, int(round((val - d["min"]) / (d["max"] - d["min"]) * 127))))


def push_settings(keys=None):
    """Send settings to Mixxx. keys=None sends all of them, otherwise only those keys."""
    if MIDI_SEND is None:
        return                                                    # no MIDI port (e.g. --ui-only)
    for d in REGISTRY:
        if d["id"] is None or (keys is not None and d["key"] not in keys):
            continue                                              # bridge-only setting, or not asked for
        MIDI_SEND("cc2", d["id"], encode(d, S[d["key"]]))
    LIVE.last_push = time.time()


def heartbeat():
    """Every 3 seconds send all settings again, so Mixxx gets them even if it starts later."""
    while True:
        time.sleep(3)
        push_settings()


# ------------------------------- turntable helpers -------------------------------
def load_rule():
    """Learned difference between base TRIANGLE and EUPHORIA: {byte: (triangle_val, euphoria_val)}"""
    try:
        # The file stores {"byte number": [triangle value, euphoria value]}; turn it back into
        # {int: tuple}.
        return {int(k): tuple(v) for k, v in json.load(open(RULE_FILE)).items()}
    except Exception:
        # No file yet (you never ran --learn) or it is unreadable: no rule, use the default guess.
        return None


def find_node():
    """Return the /dev/hidrawN path that belongs to the DJ Hero turntable."""
    # Every hidraw device has a folder in /sys/class/hidraw/. Check each one in order.
    for p in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            # "uevent" is a small text file that names the device, including its vendor/product id.
            ue = open(p + "/device/uevent").read().upper()
        except OSError:
            continue  # unreadable entry, try the next one
        if VID in ue and PID in ue:
            # Found it: turn ".../hidraw5" into "/dev/hidraw5".
            return "/dev/" + os.path.basename(p)
    # Nothing matched: stop with a helpful message.
    sys.exit("Turntable not found. Replug it and close RPCS3.")


class Bridge:
    """Turns raw turntable reports into MIDI messages.

    It is kept separate from the file reading so it can be tested without hardware:
    `send` is any function that accepts (type, number, value).
    """

    def __init__(self, send, rule=None, settings=None, live=None):
        self.send = send          # function that actually sends a MIDI message
        self.rule = rule          # learned Triangle-vs-Euphoria rule (or None)
        self.S = settings if settings is not None else S    # live settings
        self.live = live if live is not None else LIVE      # shared with the settings page
        self.state = None         # last known pressed/released state of every button
        self.hat = set()          # d-pad directions currently held (as MIDI note numbers)
        self.plat_win = deque()   # recent platter readings as (time, speed), for smoothing
        self.plat_t = 0.0         # time of the last platter message (for rate limiting)
        self.plat_sent = 0        # last platter value we sent (0 = still)
        self.last_xf = None       # last crossfader value we sent (to send only changes)
        self.anchor = None        # last knob position we accepted (to ignore jitter)
        self.acc = 0.0            # knob movement collected but not yet turned into a MIDI step

    def _note(self, n, on):
        """Send a button press/release as a MIDI note and record it for the settings page."""
        self.send("note_on" if on else "note_off", n, 127 if on else 0)
        name = NOTE_NAMES.get(n, str(n))
        if on:
            self.live.pressed.add(name)
        else:
            self.live.pressed.discard(name)
        self.live.events.append((time.strftime("%H:%M:%S"), name + (" pressed" if on else " released")))

    def is_euphoria(self, d):
        """With bit 3 set, decide whether the EUPHORIA button (not TRIANGLE) caused it."""
        if self.rule:
            # A rule was learned: count how many of the bytes it lists look like the
            # triangle press, and how many look like the euphoria press.
            tri = sum(1 for i, (t, e) in self.rule.items() if d[i] == t)
            eup = sum(1 for i, (t, e) in self.rule.items() if d[i] == e)
            return eup >= tri      # on a tie, assume euphoria
        # No rule: fall back to the guess that only euphoria raises pressure byte 11.
        return d[11] > 0

    def handle(self, d):
        """Process ONE report `d` (27 bytes) and send whatever MIDI it implies."""
        if len(d) < REPORT_LEN:
            return  # incomplete report, ignore it
        s = self.S                                  # shorthand for the live settings
        self.live.reports += 1
        self.live.last_report = time.time()

        # ---- buttons -------------------------------------------------------------
        bits = d[0] | (d[1] << 8)   # join bytes 0 and 1 into one 16-bit number of button bits
        # Platter buttons are flagged in byte 23: 0x10 green, 0x20 red, 0x40 blue.
        pg, pr, pb = bool(d[23] & 0x10), bool(d[23] & 0x20), bool(d[23] & 0x40)
        # Bit 3 is shared by TRIANGLE and EUPHORIA; work out which one it really is.
        euph = bool(bits & 8) and self.is_euphoria(d)
        # Current state (True = pressed) of every button we care about, keyed by MIDI note number.
        state = {
            1: bool(bits & 1) and not pb,        # square (blue platter button shares this bit)
            2: bool(bits & 2) and not pg,        # X      (green)
            3: bool(bits & 4) and not pr,        # circle (red)
            4: bool(bits & 8) and not euph,      # triangle
            40: pg, 41: pr, 42: pb, 50: euph,    # platter green / red / blue, euphoria
            9: bool(bits >> 8 & 1), 10: bool(bits >> 9 & 1), 13: bool(bits >> 12 & 1),  # select, start, PS
        }
        if self.state is not None:   # skip the very first report: there is nothing to compare with yet
            for n, on in state.items():
                if on != self.state[n]:
                    self._note(n, on)       # the button changed: send note_on / note_off
        self.state = state           # remember for next time

        # ---- d-pad (hat) ---------------------------------------------------------
        # d-pad (hat): low nibble of byte 2, 0..7 clockwise from up, anything else = released
        h = d[2] & 0x0F              # keep only the low 4 bits
        dirs = set()                 # directions pressed right now
        if h <= 7:                   # 8 and above (idle is 0x0F) means nothing is pressed
            # Diagonals (1, 3, 5, 7) count as two directions at once.
            if h in (7, 0, 1): dirs.add(20)   # up
            if h in (1, 2, 3): dirs.add(21)   # right
            if h in (3, 4, 5): dirs.add(22)   # down
            if h in (5, 6, 7): dirs.add(23)   # left
        for n in sorted(dirs - self.hat): self._note(n, True)    # newly pressed directions
        for n in sorted(self.hat - dirs): self._note(n, False)   # directions just released
        self.hat = dirs              # remember for next time

        # ---- platter -------------------------------------------------------------
        # The platter reports its SPEED. Average the last few milliseconds so slow movement
        # (which arrives as 0,0,1,0,0,1...) becomes a steady speed instead of stuttering, then
        # send it with platter_res steps per count so slow speeds keep their detail.
        v = d[5] - 128               # speed: 0 = still, positive = clockwise, negative = counter-clockwise
        # Tiny values are noise. Use a bigger dead zone while a platter button is held,
        # because pressing it wobbles the platter slightly.
        if abs(v) <= (s["platter_deadzone_buttons"] if (pg or pr or pb) else s["platter_deadzone"]):
            v = 0
        sv = -v if s["invert_platter"] else v      # apply the "scratches backwards" setting
        now = time.monotonic()                     # current time in seconds (never jumps backwards)
        self.plat_win.append((now, sv))            # remember this reading...
        cutoff = now - s["platter_smooth_ms"] / 1000.0
        while self.plat_win and self.plat_win[0][0] < cutoff:
            self.plat_win.popleft()                # ...and forget readings older than the smoothing window
        avg = sum(x for _, x in self.plat_win) / len(self.plat_win)   # smoothed speed in counts
        # Convert to a MIDI step count in the range -63..63 (64 = still is added when sending).
        val = max(-63, min(63, int(round(avg * s["platter_res"] * s["platter_gain"]))))
        due = now - self.plat_t >= 1.0 / s["platter_send_hz"]     # rate limit allows another message
        if val != self.plat_sent:
            # Speed changed. Starting and stopping are sent right away; other changes obey the rate limit.
            if due or val == 0 or self.plat_sent == 0:
                self.send("cc", 1, 64 + val)
                self.plat_sent, self.plat_t = val, now
        elif val != 0 and due:
            # Still moving at the same speed: repeat it so Mixxx knows the platter is still going.
            self.send("cc", 1, 64 + val)
            self.plat_t = now
        self.live.platter = self.plat_sent

        # ---- crossfader ----------------------------------------------------------
        # 10-bit value (0..1023) in bytes 21-22; scale it down to MIDI's 0..127.
        xf = round((d[21] | (d[22] << 8)) * 127 / 1023)
        xf = max(0, min(127, xf))
        if s["invert_crossfader"]:
            xf = 127 - xf                          # crossfader fitted the other way round
        if xf != self.last_xf:         # only send when it actually changed
            self.send("cc", 3, xf)
            self.last_xf = xf
        self.live.xfader = xf

        # ---- effects knob (endless, relative) ------------------------------------
        # The knob turns forever, so its value wraps around 0..1023. We send how far it moved,
        # not where it is.
        kv = d[19] | (d[20] << 8)      # 10-bit knob position from bytes 19-20
        if self.anchor is None:
            self.anchor = kv           # first report: just remember the starting position
        # Shortest signed distance from the last accepted position, handling the wrap-around.
        delta = (kv - self.anchor + 512) % 1024 - 512
        if abs(delta) >= s["knob_noise"]:   # ignore small jitter (the knob wobbles by a few counts)
            self.anchor = kv           # accept this as the new reference position
            self.acc += -delta if s["invert_knob"] else delta   # collect the movement
            steps = int(self.acc / s["knob_div"])               # whole MIDI steps collected so far
            self.acc -= steps * s["knob_div"]                   # keep the leftover for next time
            if steps:
                # 64 = no movement; above = clockwise, below = counter-clockwise.
                self.send("cc", 4, 64 + max(-63, min(63, steps)))
                self.live.knob_turn += steps


def learn(fd):
    """Find what is different between the base TRIANGLE button and the EUPHORIA button."""
    def rec(secs):
        """Record every report for `secs` seconds and return them as a list."""
        while select.select([fd], [], [], 0)[0]:   # throw away reports that piled up while we waited
            os.read(fd, 64)
        fr, end = [], time.time() + secs
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:   # wait up to 50 ms for the next report
                x = os.read(fd, 64)
                if len(x) >= REPORT_LEN:
                    fr.append(x)
        return fr

    def countdown(msg):
        """Print an instruction and a 3-2-1 countdown so you can get ready."""
        print("\n" + msg)
        for n in (3, 2, 1):
            print(f"   ready in {n}...", end="\r", flush=True)
            time.sleep(1)
        print("   GO! keep it held       ")

    print("Close Mixxx first. Keep your hands off the turntable.")
    time.sleep(2)
    countdown("Press and HOLD the base TRIANGLE button (not euphoria)")
    tri = [f for f in rec(2.5) if f[0] & 8]    # keep only reports where bit 3 (triangle/euphoria) is set
    countdown("Now press and HOLD the EUPHORIA button")
    eup = [f for f in rec(2.5) if f[0] & 8]
    if not tri or not eup:
        sys.exit("Did not see a button press, run --learn again.")
    # For each byte, the most common value seen while the button was held.
    mode = lambda fr, i: Counter(f[i] for f in fr).most_common(1)[0][0]
    # Keep only the bytes where TRIANGLE and EUPHORIA behave differently.
    diff = {i: (mode(tri, i), mode(eup, i)) for i in RAWB if mode(tri, i) != mode(eup, i)}
    os.makedirs(os.path.dirname(RULE_FILE), exist_ok=True)   # make ~/.config if it does not exist
    json.dump({str(i): list(v) for i, v in diff.items()}, open(RULE_FILE, "w"))   # save the rule
    if not diff:
        print("\nTRIANGLE and EUPHORIA send the IDENTICAL signal, so they cannot be told apart.")
        print("Tell me and I will give you a workaround.")
    else:
        print("\nLearned. They differ in:", ", ".join(
            f"byte {i}: triangle={t:02x} euphoria={e:02x}" for i, (t, e) in diff.items()))
        print("Now run the bridge (or --watch to check).")


# =================================== settings page ===================================
class UIHandler(BaseHTTPRequestHandler):
    """The tiny web server behind http://127.0.0.1:8765/ . Only this computer can reach it."""

    protocol_version = "HTTP/1.1"     # keep the connection open between requests (much faster than reconnecting)
    wbufsize = 65536                  # collect the reply's header and body and send them together...

    def setup(self):
        super().setup()
        # ...and never hold a small reply back. Without this, a kept-open connection can stall about
        # 40 ms on every request (the classic "Nagle + delayed ACK" delay), which feels like lag.
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def log_message(self, *args):
        pass                                      # keep the terminal quiet

    def _reply(self, code, body, ctype="application/json"):
        """Send an HTTP answer. dict/list bodies become JSON."""
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype == "application/json" else "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        """Refuse requests that were not addressed to this computer (blocks DNS-rebinding tricks)."""
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _file(self, name, ctype):
        """Send one of the web files that sit next to this script."""
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                self._reply(200, f.read(), ctype)
        except OSError:
            self._reply(404, f"<h1>Missing file: {name}</h1><p>Keep it in the same folder as "
                             f"djhero_mixxx_bridge.py</p>", "text/html; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._reply(403, {"error": "bad host"})
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._file("djhero_ui.html", "text/html; charset=utf-8")
        elif path == "/logo.png":
            self._file("dj-hero-logo.png", "image/png")
        elif path == "/stickers.png":
            self._file("dj-hero-stickers.png", "image/png")
        elif path == "/api/settings":
            self._reply(200, {"registry": REGISTRY, "values": S.snapshot(), "dirty": S.dirty()})
        elif path == "/api/state":
            self._reply(200, LIVE.snapshot(S))
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        # Read the request body FIRST, even if we are about to refuse it. The connection is kept open, and
        # unread bytes would be mistaken for the start of the next request.
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > 65536:
            self.close_connection = True
            return self._reply(413, {"error": "bad length"})
        raw = self.rfile.read(n) if n else b""
        # A custom header that a web page on another site cannot add: blocks cross-site tricks.
        if not self._host_ok() or self.headers.get("X-DJHero") != "1":
            return self._reply(403, {"error": "forbidden"})
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return self._reply(400, {"error": "bad json"})
        path = self.path.split("?")[0]
        if path == "/api/settings":                   # a change made on the page: applied live, NOT saved
            changed = S.update(data if isinstance(data, dict) else {})
            push_settings(set(changed))               # tell Mixxx about the ones that changed
        elif path == "/api/save":                     # the SAVE button: write to disk
            try:
                S.save()
            except OSError as e:
                return self._reply(500, {"error": f"could not save: {e}"})
        elif path == "/api/reset":                    # defaults, live (press SAVE to keep them)
            S.reset()
            push_settings()
        elif path == "/api/revert":                   # undo everything since the last save
            S.revert()
            push_settings()
        elif path == "/api/resend":
            push_settings()
        else:
            return self._reply(404, {"error": "not found"})
        self._reply(200, {"values": S.snapshot(), "dirty": S.dirty()})


def start_ui():
    """Start the settings page in the background. Returns its address, or None if no port was free."""
    for port in range(UI_PORT, UI_PORT + 11):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), UIHandler)   # 127.0.0.1 = this computer only
            break
        except OSError:
            continue                                                    # port in use, try the next one
    else:
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}/"                                  # numeric address: no slow "localhost" lookup


# ================================== startup animation ==================================
# Your DJ Hero logo, stored at high resolution and drawn in the terminal at whatever size fits.
# LOGO_Z is the logo as a 280 x 242 grid of 4 colour classes (0 transparent, 1 yellow outline,
# 2 black, 3 white letters), compressed with zlib and written as base64 text.
LOGO_W, LOGO_H = 280, 242
LOGO_Z = """
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
qeqtvpKGFF0aywl/ARVzhk8=
"""
MAX_LOGO_ROWS = 56                                  # never draw the logo taller than this many terminal rows
ASPECT = 2.0 * LOGO_W / LOGO_H                      # logo columns per logo row (a terminal cell is twice as tall as wide)

# The 16 "quadrant" characters: each one fills some of the 4 quarters of a cell.
# The number is a mask: 1 = top-left, 2 = top-right, 4 = bottom-left, 8 = bottom-right.
QUAD = " \u2598\u259d\u2580\u2596\u258c\u259e\u259b\u2597\u259a\u2590\u259c\u2584\u2599\u259f\u2588"
# Rough colour of each class, only used to decide which of two colours a third colour is closer to.
CLASS_RGB = {0: (24, 24, 24), 1: (245, 217, 10), 2: (20, 20, 20), 3: (255, 255, 255)}

_logo_rows = None   # the decoded source grid (filled in the first time it is needed)


def _logo_source():
    """Decode LOGO_Z once into a list of LOGO_H rows of LOGO_W class numbers."""
    global _logo_rows
    if _logo_rows is None:
        raw = zlib.decompress(base64.b64decode("".join(LOGO_Z.split())))
        _logo_rows = [raw[y * LOGO_W:(y + 1) * LOGO_W] for y in range(LOGO_H)]
    return _logo_rows


def _logo_pixels(tw, th):
    """Shrink (or stretch) the logo to tw x th pixels. Each pixel takes the class that covers most of
    its block of the source. Yellow counts extra so the thin outline survives at small sizes."""
    src = _logo_source()
    out = []
    for ty in range(th):
        y0 = ty * LOGO_H // th
        y1 = max(y0 + 1, (ty + 1) * LOGO_H // th)
        block = src[y0:y1]                       # the source rows that make up this pixel row
        row = []
        for tx in range(tw):
            x0 = tx * LOGO_W // tw
            x1 = max(x0 + 1, (tx + 1) * LOGO_W // tw)
            n = [0, 0, 0, 0]                     # how many source pixels of each class are in the block
            for r in block:
                seg = r[x0:x1]
                for k in range(4):
                    n[k] += seg.count(k)
            n[1] *= 1.6                          # give the yellow outline extra weight
            row.append(n.index(max(n)))
        out.append(row)
    return out


def _logo_cells(px):
    """Turn a pixel grid (2 pixels per cell across and down) into terminal cells (glyph, fg, bg).

    Every cell covers a 2x2 block of pixels. At most two colours can be shown in one cell, so the
    two most common classes win and a third one joins whichever of them it is closer to. Transparent
    (class 0) is always used as the background, so it shows the terminal's own background colour."""
    def dist(a, b):
        return sum((p - q) ** 2 for p, q in zip(CLASS_RGB[a], CLASS_RGB[b]))
    cells = []
    for cy in range(len(px) // 2):
        top, bot = px[2 * cy], px[2 * cy + 1]
        row = []
        for cx in range(len(top) // 2):
            q = (top[2 * cx], top[2 * cx + 1], bot[2 * cx], bot[2 * cx + 1])    # TL, TR, BL, BR
            if q[0] == q[1] == q[2] == q[3]:
                row.append((" ", 0, q[0]))                   # one colour: a plain coloured space
                continue
            cnt = Counter(q)
            a, b = sorted(cnt, key=lambda k: (-cnt[k], k))[:2]   # the two most common classes
            if a == 0:
                bg, fg = a, b                                # transparent is always the background
            elif b == 0:
                bg, fg = b, a
            else:
                bg, fg = a, b                                # otherwise the more common one is the background
            mask = 0
            for i, k in enumerate(q):
                if k != a and k != b:
                    k = a if dist(k, a) <= dist(k, b) else b  # a third colour joins the closer one
                if k == fg:
                    mask |= 1 << i
            row.append((QUAD[mask], fg, bg))
        cells.append(row)
    return cells


def _cells_to_lines(cells, upto, yellow):
    """Colour codes + characters for the first `upto` rows of cells (the rest are blank).
    A new colour code is only written when the colours change, which keeps the output small."""
    pal = {1: yellow, 2: 234, 3: 231}                # xterm-256 colour numbers: yellow, dark grey, white
    lines = []
    for r, row in enumerate(cells):
        if r >= upto:
            lines.append("")
            continue
        out, cur = [], None
        for glyph, fg, bg in row:
            key = (None if glyph == " " else fg, bg)  # a space has no visible foreground
            if key != cur:
                fgc = "39" if glyph == " " else f"38;5;{pal[fg]}"
                bgc = "49" if bg == 0 else f"48;5;{pal[bg]}"
                out.append(f"\033[{fgc};{bgc}m")
                cur = key
            out.append(glyph)
        out.append("\033[0m")
        lines.append("".join(out))
    return lines


def _arg_value(name):
    """Value of a command-line option written as `--name 40` or `--name=40` (None if absent)."""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def splash(max_rows=None):
    """Animated DJ Hero logo in the terminal: it is revealed from the top, the yellow outline pulses,
    then a short equalizer plays. The logo is drawn as big as your terminal allows (higher resolution
    in a bigger window). Takes about 2 seconds."""
    out = sys.stdout
    size = shutil.get_terminal_size((80, 24))
    # How many terminal rows can the logo use? Keep 7 free for the tagline, equalizer and status lines,
    # and make sure the matching width fits the window too.
    rows = min(size.lines - 7, int((size.columns - 2) / ASPECT), MAX_LOGO_ROWS)
    if max_rows:
        rows = min(rows, int(max_rows))
    nice = (out.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
            and "utf" in (out.encoding or "").lower() and rows >= 12)
    if not nice:
        # Terminal too small no colour or not a terminal a plain one-line banner instead.
        print("==  DJ HERO turntable -> Mixxx bridge  ==")
        return
    cols = int(round(rows * ASPECT))
    cells = _logo_cells(_logo_pixels(2 * cols, 2 * rows))    # 2x2 pixels per cell: sharper than half-blocks
    tagline = "P S 3   D J   H E R O   T U R N T A B L E   >>>   M I X X X"
    pad = max(0, (cols - len(tagline)) // 2)
    bars = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"          # equalizer bar heights

    def put(row, col, text):
        """Write text at a screen position (1-based row and column)."""
        out.write(f"\033[{row};{col}H{text}")

    try:
        out.write("\033[?25l\033[2J")                         # hide the cursor, clear the screen
        for i, line in enumerate(_cells_to_lines(cells, rows, 220)):   # 1) reveal the fuuuuuuuuu from the top
            put(i + 1, 1, line)
            out.flush()
            time.sleep(0.9 / rows)
        put(rows + 2, pad + 1, f"\033[38;5;220m{tagline}\033[0m")
        for y in (226, 178, 226, 220):                        # 2) the yellow outline pulses
            for i, line in enumerate(_cells_to_lines(cells, rows, y)):
                put(i + 1, 1, line)
            out.flush()
            time.sleep(0.1)
        for _ in range(14):                                   # 3) equalizer bars dance under the logo
            put(rows + 3, pad + 1, "\033[38;5;220m" + "".join(random.choice(bars) for _ in tagline) + "\033[0m")
            out.flush()
            time.sleep(0.07)
        put(rows + 3, pad + 1, "\033[38;5;220m" + bars[0] * len(tagline) + "\033[0m")   # settle flat
        put(rows + 5, 1, "")                                  # leave the cursor below the logo
    finally:
        out.write("\033[0m\033[?25h\n")                       # always restore colours and the cursor
        out.flush()


# ======================================= main =======================================
def main():
    """Entry point: choose what to run from the command-line flags."""
    global MIDI_SEND
    args = set(sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(__doc__)
        return

    # ---- --learn: teach it how Triangle differs from Euphoria, then exit ----
    if "--learn" in args:
        fd = _open_turntable(find_node())
        return learn(fd)

    # ---- --watch: print what the bridge sees, without sending any MIDI ----
    if "--watch" in args:
        fd = _open_turntable(find_node())

        def show(t, n, v):
            """Stands in for sending MIDI: print button presses/releases (skip the noisy CCs)."""
            if t.startswith("note"):
                print(f"{NOTE_NAMES.get(n, n):18} {'pressed' if t == 'note_on' else 'released'}")

        b = Bridge(show, load_rule())   # same logic as the real bridge, but it prints instead
        prev = None                     # previous report, to see which bytes changed
        print("WATCH MODE: press buttons one at a time (Ctrl+C to quit)")
        while True:
            select.select([fd], [], [])     # sleep until a report arrives
            d = os.read(fd, 64)
            if prev is not None and len(d) >= REPORT_LEN:
                # List the button bytes that changed, as "b<index> old->new" in hex.
                ch = [f"b{i} {prev[i]:02x}->{d[i]:02x}" for i in RAWB if prev[i] != d[i]]
                if ch:
                    print("   raw:", " ".join(ch))
            prev = d
            b.handle(d)

    # ---- normal mode ----
    if "--no-splash" not in args:
        splash(_arg_value("--logo-rows"))            # the startup animation (as big as the window allows)
    ui_only = "--ui-only" in args
    fd = None
    if not ui_only:
        node = find_node()                           # e.g. /dev/hidraw5
        fd = _open_turntable(node)
        LIVE.node = node
        print(f"[ok] Turntable found on {node}")

    url = start_ui()                                 # the settings page
    if url:
        print(f"[ok] Settings page: {url}")
        if "--no-browser" not in args:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()   # open it in your browser
    else:
        print("[!!] Could not start the settings page (no free port)")

    if ui_only:
        print("UI-only mode: no turntable, no MIDI. Ctrl+C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return

    import mido   # imported here so --watch, --learn and --ui-only work even if mido is not installed
    out = mido.open_output("DJ Hero", virtual=True)   # create the virtual MIDI port Mixxx connects to
    lock = threading.Lock()                           # the heartbeat and the main loop both send MIDI

    def send(t, n, v):
        """Send one MIDI message. "cc" = control on channel 1, "cc2" = setting on channel 2, else a note."""
        with lock:
            if t == "cc":
                out.send(mido.Message("control_change", channel=0, control=n, value=v))
            elif t == "cc2":
                out.send(mido.Message("control_change", channel=1, control=n, value=v))
            else:
                out.send(mido.Message(t, channel=0, note=n, velocity=v))

    MIDI_SEND = send                                  # lets the settings page send settings too
    LIVE.midi_open = True
    push_settings()                                   # give Mixxx the saved settings right away...
    threading.Thread(target=heartbeat, daemon=True).start()   # ...and keep repeating them

    bridge = Bridge(send, load_rule())
    print("[ok] Virtual MIDI port 'DJ Hero' is open. Leave this running and start Mixxx. (Ctrl+C to quit)")
    try:
        while True:                                  # run until you press Ctrl+C
            select.select([fd], [], [])              # sleep until the turntable sends a report
            bridge.handle(os.read(fd, 64))           # decode it and send any MIDI
    except KeyboardInterrupt:
        print("\nBye!")


def _open_turntable(node):
    """Open the hidraw file for reading, with a helpful message if the permissions are wrong."""
    try:
        return os.open(node, os.O_RDONLY)            # read-only; reports arrive as bytes
    except PermissionError:
        sys.exit(f"No permission for {node}. Check the udev rule and replug.")


if __name__ == "__main__":   # only run main() when started directly, not when imported
    main()
