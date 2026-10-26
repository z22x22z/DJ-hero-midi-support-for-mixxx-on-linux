// Mixxx script for the DJ Hero MIDI bridge (djhero_mixxx_bridge.py)
//
//  FACE BUTTONS (hot cues)        PLATTER BUTTONS (active deck)
//   circle    hot cue 1            red     sync lock
//   X         hot cue 2            green   cue (hold to preview)
//   square    hot cue 3            blue    headphone cue
//   triangle  hot cue 4
//  PS              switch which deck the hot cues control (deck 1 <-> 2).
//                  This does NOT change the active deck: the platter, tempo, sync,
//                  cue and effects stay on the active deck. (Happens when you let go of PS.)
//  PS HELD (more than 0.15 s) + a face button = loop shortcuts on the ACTIVE deck:
//                  X      4 beat loop on; press it again to turn the loop off (keyboard U / Q)
//                  circle loop in point     (keyboard 7 / 2)
//                  square loop out point    (keyboard 8 / 3, activates the loop)
//                  If PS is used like this, it does not switch the hot cue deck.
//  euphoria        next effect: Filter deck 1 -> Filter deck 2 -> Echo -> Phaser -> Reverb
//                  (the knob adjusts the selected one)
//  d-pad up/down   tempo up/down (hold to repeat)     d-pad right  switch active deck 1<->2
//  d-pad left      effect on/off (a Filter: its own deck; Echo/Phaser/Reverb: the active deck)
//  select          play/pause deck 1                  start        play/pause deck 2
//  platter         scratches the active deck
//
// Hot cue buttons work like Mixxx's keyboard keys (Z X C V / M , . /): press an empty
// hot cue to set it, press a set one to jump to it. Hold to preview.
// STICKY: hold a hot cue button for STICKY_HOLD_MS and it stays "held" after you let go;
// press it again to release it.
//
// Every number in the settings block below can be changed live from the bridge's settings
// page (http://localhost:8765/). The bridge sends them over MIDI channel 2 (see SETTINGS
// near the bottom), so the values here are only the starting defaults.
// Filter is the deck's quick effect. Echo, Phaser, Reverb must be loaded ONCE into
// effect slots 1, 2, 3 of effect unit 1.

var DJHeroBridge = {};

// ---------------- settings ----------------
DJHeroBridge.DIRECT_SCRATCH = true;   // true = set the scratch speed directly (no smoothing, least delay)
// Platter feel (direct mode). Playback speed = RATE_SCALE * (platter speed ^ RATE_POWER), capped.
//  - too much scrubbing  -> lower RATE_SCALE or MAX_RATE
//  - slow / sluggish     -> raise RATE_SCALE; lower RATE_POWER boosts slow movement more
//  - negative RATE_SCALE reverses the direction
DJHeroBridge.RATE_SCALE = 0.35;
DJHeroBridge.RATE_POWER = 0.7;        // 1 = straight line, lower = more sensitive at slow speeds
DJHeroBridge.MAX_RATE = 3.0;          // never scrub faster than this many times normal speed
DJHeroBridge.RELEASE_MS = 60;         // after the platter stops, hold the record this long, then let go (0 = instantly)
DJHeroBridge.PLATTER_RES = 3;         // MIDI steps per platter count. MUST match PLATTER_RES in the Python file
DJHeroBridge.TICK_SCALE = 0.2;        // filtered mode only (DIRECT_SCRATCH = false)
DJHeroBridge.INTERVALS = 128;
DJHeroBridge.RPM = 33 + 1 / 3;
DJHeroBridge.ALPHA = 1 / 4;           // filtered mode only (BETA is ALPHA / 32)
DJHeroBridge.KNOB_STEP = 0.02;        // effect knob sensitivity
DJHeroBridge.FX_MASK = 31;            // which effects Euphoria steps through (tick boxes: bit 0 = Filter deck 1 ... bit 4 = Reverb)
DJHeroBridge.STICKY_ENABLED = true;   // hold a hot cue button and it stays held
DJHeroBridge.STICKY_HOLD_MS = 400;    // how long to hold before it turns sticky
DJHeroBridge.TEMPO_INVERT = false;    // true if d-pad up makes the tempo go DOWN
DJHeroBridge.TEMPO_REPEAT_DELAY = 400;
DJHeroBridge.TEMPO_REPEAT_MS = 100;
DJHeroBridge.EFFECTS = ["Filter deck 1", "Filter deck 2", "Echo", "Phaser", "Reverb"];
// face button note -> hot cue number (notes: 1 square, 2 X, 3 circle, 4 triangle)
DJHeroBridge.HOTCUE = {3: 1, 2: 2, 1: 3, 4: 4};
// PS held longer than this (ms) turns X / circle / square into loop buttons
DJHeroBridge.PS_HOLD_MS = 150;
// face button note -> Mixxx loop control (on the active deck)
DJHeroBridge.LOOP = {2: "beatloop_4_activate", 3: "loop_in", 1: "loop_out"};
// ------------------------------------------

DJHeroBridge.deck = 1;
DJHeroBridge.hotDeck = 1;             // deck the hot cue buttons control (set with PS)
DJHeroBridge.padDeck = {};            // deck each held pad was pressed on
DJHeroBridge.ps = {down: false, used: false, t: 0};   // PS button state
DJHeroBridge.loopHeld = {};           // loop control each held face button pressed {g, k}
DJHeroBridge.fx = 0;                  // index into EFFECTS (0/1 = filter deck 1/2, 2/3/4 = effect slots 1/2/3)
DJHeroBridge.cueDeck = 1;
DJHeroBridge.sticky = {};             // hot cue buttons currently latched: note -> deck group
DJHeroBridge.skipRelease = {};        // buttons whose next release must be ignored (it ended a sticky)
DJHeroBridge.padT = {};               // when each hot cue button was pressed
DJHeroBridge.scratching = {1: false, 2: false};
DJHeroBridge.timers = {1: 0, 2: 0};
DJHeroBridge.tempoTimers = {up: 0, down: 0};

DJHeroBridge.init = function (id, debugging) {
    DJHeroBridge.applyEffectSelection();
    print("DJ Hero loaded. Active deck 1, hot cues on deck 1, effect Filter deck 1");
};

DJHeroBridge.shutdown = function () {
    DJHeroBridge.stopScratch(1);
    DJHeroBridge.stopScratch(2);
    DJHeroBridge.stopTempo("up");
    DJHeroBridge.stopTempo("down");
    for (var c in DJHeroBridge.sticky) {      // let go of any sticky hot cues
        engine.setValue(DJHeroBridge.sticky[c], "hotcue_" + DJHeroBridge.HOTCUE[c] + "_activate", 0);
    }
};

DJHeroBridge.chan = function () { return "[Channel" + DJHeroBridge.deck + "]"; };

DJHeroBridge.toggle = function (g, k) {
    engine.setValue(g, k, engine.getValue(g, k) ? 0 : 1);
};

// ---------------- platter ----------------
DJHeroBridge.stopScratch = function (deck) {
    var B = DJHeroBridge;
    if (B.timers[deck]) {
        engine.stopTimer(B.timers[deck]);
        B.timers[deck] = 0;
    }
    if (B.scratching[deck]) {
        if (B.DIRECT_SCRATCH) {
            engine.setValue("[Channel" + deck + "]", "scratch2", 0);
            engine.setValue("[Channel" + deck + "]", "scratch2_enable", 0);
        } else {
            engine.scratchDisable(deck);
        }
        B.scratching[deck] = false;
    }
};

// platter speed (in platter counts, signed) -> playback speed. Slow movement is boosted,
// fast movement is compressed and capped, so it responds early but never scrubs wildly.
DJHeroBridge.platterRate = function (v) {
    var B = DJHeroBridge;
    var r = B.RATE_SCALE * Math.pow(Math.abs(v), B.RATE_POWER);
    if (Math.abs(r) > B.MAX_RATE) { r = B.MAX_RATE; }
    return v < 0 ? -r : r;
};

// CC1: platter SPEED, 64 = still, above = clockwise (PLATTER_RES steps per platter count)
DJHeroBridge.platter = function (channel, control, value, status, group) {
    var B = DJHeroBridge, deck = B.deck, v = (value - 64) / B.PLATTER_RES;

    if (v === 0) {
        // platter stopped: hold the record for a moment, then let go (no stop-start stutter)
        if (!B.scratching[deck]) { return; }
        if (B.DIRECT_SCRATCH) { engine.setValue("[Channel" + deck + "]", "scratch2", 0); }
        else { engine.scratchTick(deck, 0); }
        if (B.RELEASE_MS <= 0) { B.stopScratch(deck); return; }
        if (!B.timers[deck]) {
            B.timers[deck] = engine.beginTimer(B.RELEASE_MS, function () {
                B.timers[deck] = 0;
                B.stopScratch(deck);
            }, true);
        }
        return;
    }

    // moving: cancel any pending let-go and (re)grab the record
    if (B.timers[deck]) { engine.stopTimer(B.timers[deck]); B.timers[deck] = 0; }
    if (!B.scratching[deck]) {
        if (B.DIRECT_SCRATCH) {
            engine.setValue("[Channel" + deck + "]", "scratch2_enable", 1);
        } else {
            engine.scratchEnable(deck, B.INTERVALS, B.RPM, B.ALPHA, B.ALPHA / 32);
        }
        B.scratching[deck] = true;
    }
    if (B.DIRECT_SCRATCH) {
        engine.setValue("[Channel" + deck + "]", "scratch2", B.platterRate(v));
    } else {
        engine.scratchTick(deck, v * B.TICK_SCALE);
    }
};

// ---------------- effects ----------------
// fx index: 0 = filter on deck 1, 1 = filter on deck 2,
//           2 = effect slot 1 (Echo), 3 = slot 2 (Phaser), 4 = slot 3 (Reverb) of effect unit 1
DJHeroBridge.applyEffectSelection = function () {
    var B = DJHeroBridge;
    for (var slot = 1; slot <= 3; slot++) {
        engine.setValue("[EffectRack1_EffectUnit1_Effect" + slot + "]", "enabled", (B.fx === slot + 1) ? 1 : 0);
    }
    print("DJ Hero effect: " + B.EFFECTS[B.fx]);
};

// Euphoria: go to the next effect that is ticked in FX_MASK (skips the others)
DJHeroBridge.nextEffect = function () {
    var B = DJHeroBridge, n = B.EFFECTS.length;
    for (var i = 1; i <= n; i++) {
        var c = (B.fx + i) % n;
        if (B.FX_MASK & (1 << c)) { B.fx = c; break; }
    }
    B.applyEffectSelection();
};

// d-pad left: filters switch on/off on their own deck; the other effects go on / off the active deck
DJHeroBridge.fxApply = function () {
    var B = DJHeroBridge, g = B.chan();
    if (B.fx <= 1) {
        B.toggle("[QuickEffectRack1_[Channel" + (B.fx + 1) + "]]", "enabled");
        print("DJ Hero " + B.EFFECTS[B.fx] + " toggled");
    } else {
        var u = "[EffectRack1_EffectUnit1]";
        engine.setValue(u, "enabled", 1);
        if (engine.getValue(u, "mix") < 0.05) { engine.setValue(u, "mix", 1); }
        B.toggle(u, "group_" + g + "_enable");
        print("DJ Hero " + B.EFFECTS[B.fx] + " toggled on deck " + B.deck);
    }
};

DJHeroBridge.knob = function (channel, control, value, status, group) {
    var B = DJHeroBridge, g, k;
    if (B.fx <= 1) { g = "[QuickEffectRack1_[Channel" + (B.fx + 1) + "]]"; k = "super1"; }
    else { g = "[EffectRack1_EffectUnit1_Effect" + (B.fx - 1) + "]"; k = "meta"; }
    var v = engine.getParameter(g, k) + (value - 64) * B.KNOB_STEP;
    engine.setParameter(g, k, Math.max(0, Math.min(1, v)));
};

// ---------------- tempo ----------------
DJHeroBridge.tempoStep = function (up) {
    var B = DJHeroBridge, g = B.chan();
    var k = ((up !== B.TEMPO_INVERT) ? "rate_perm_up_small" : "rate_perm_down_small");
    engine.setValue(g, k, 1);
    engine.setValue(g, k, 0);
};

DJHeroBridge.stopTempo = function (which) {
    var B = DJHeroBridge;
    if (B.tempoTimers[which]) {
        engine.stopTimer(B.tempoTimers[which]);
        B.tempoTimers[which] = 0;
    }
};

DJHeroBridge.tempo = function (up, pressed) {
    var B = DJHeroBridge, which = up ? "up" : "down";
    B.stopTempo(which);
    if (!pressed) { return; }
    B.tempoStep(up);
    B.tempoTimers[which] = engine.beginTimer(B.TEMPO_REPEAT_DELAY, function () {
        B.tempoTimers[which] = engine.beginTimer(B.TEMPO_REPEAT_MS, function () {
            B.tempoStep(up);
        });
    }, true);
};

// ---------------- settings sent from the bridge's settings page ----------------
// MIDI channel 2: CC number -> [name, min, max, kind]. A CC value 0..127 is turned back into the
// real number (bool: 64 or more = on; mask: the number itself). This table is generated from
// REGISTRY in djhero_mixxx_bridge.py; the ids and ranges must stay the same in both.
DJHeroBridge.SETTINGS = {
    1: ["DIRECT_SCRATCH", 0, 1, "bool"],   // Direct scratch mode
    2: ["RATE_SCALE", 0.05, 1.5, "float"],   // Scratch speed
    3: ["RATE_POWER", 0.3, 1.2, "float"],   // Speed curve
    4: ["MAX_RATE", 1.0, 10.0, "float"],   // Fastest scratch
    5: ["RELEASE_MS", 0, 300, "int"],   // Hold after stopping (ms)
    6: ["TICK_SCALE", 0.05, 1.0, "float"],   // Filtered mode: speed
    7: ["ALPHA", 0.05, 1.0, "float"],   // Filtered mode: follow speed
    8: ["PLATTER_RES", 1, 8, "int"],   // Platter detail (MIDI steps per count)
    9: ["KNOB_STEP", 0.005, 0.1, "float"],   // Effect knob sensitivity
    10: ["FX_MASK", 0, 31, "mask"],   // Effects in the Euphoria list
    11: ["TEMPO_INVERT", 0, 1, "bool"],   // Reverse tempo buttons
    12: ["TEMPO_REPEAT_DELAY", 100, 1000, "int"],   // Hold before repeating (ms)
    13: ["TEMPO_REPEAT_MS", 30, 400, "int"],   // Repeat speed (ms per step)
    14: ["PS_HOLD_MS", 50, 600, "int"],   // PS hold time for loops (ms)
    15: ["STICKY_ENABLED", 0, 1, "bool"],   // Sticky hot cues
    16: ["STICKY_HOLD_MS", 150, 1500, "int"],   // Sticky hold time (ms)
};

DJHeroBridge.setting = function (channel, control, value, status, group) {
    var B = DJHeroBridge, s = B.SETTINGS[control], v;
    if (!s) { return; }                                   // not a setting we know
    if (s[3] === "bool") { v = value >= 64; }
    else if (s[3] === "mask") { v = value; }
    else {
        v = s[1] + (value / 127) * (s[2] - s[1]);
        if (s[3] === "int") { v = Math.round(v); }
    }
    // switching scratch mode while scratching: let go of the record first
    if (s[0] === "DIRECT_SCRATCH" && B.DIRECT_SCRATCH !== v) { B.stopScratch(1); B.stopScratch(2); }
    B[s[0]] = v;
    // if the current effect was un-ticked, move on to the next ticked one
    if (s[0] === "FX_MASK" && B.FX_MASK && !(B.FX_MASK & (1 << B.fx))) { B.nextEffect(); }
    if (!B.settingsSeen) { B.settingsSeen = true; print("DJ Hero: settings received from the settings page"); }
};

// ---------------- buttons ----------------
DJHeroBridge.switchDeck = function () {
    var B = DJHeroBridge;
    B.stopScratch(1);
    B.stopScratch(2);
    B.deck = 3 - B.deck;
    print("DJ Hero active deck: " + B.deck);
};

// PS: choose which deck the hot cue buttons control (the active deck is not touched)
DJHeroBridge.switchHotDeck = function () {
    var B = DJHeroBridge;
    B.hotDeck = 3 - B.hotDeck;
    print("DJ Hero hot cues now control deck " + B.hotDeck + " (active deck stays " + B.deck + ")");
};

// PS pressed / released. A quick tap switches the hot cue deck (when you let go). If PS
// was held and used for a loop shortcut (see button()), letting go does nothing.
DJHeroBridge.psButton = function (pressed) {
    var B = DJHeroBridge;
    if (pressed) {
        B.ps.down = true;
        B.ps.used = false;
        B.ps.t = Date.now();
    } else {
        if (B.ps.down && !B.ps.used) { B.switchHotDeck(); }
        B.ps.down = false;
    }
};

// Notes: 1 square, 2 X, 3 circle, 4 triangle | 40 platter green, 41 red, 42 blue | 50 euphoria
//        9 select, 10 start, 13 PS | 20 d-pad up, 21 right, 22 down, 23 left
DJHeroBridge.button = function (channel, control, value, status, group) {
    var B = DJHeroBridge;
    var pressed = ((status & 0xF0) === 0x90) && value > 0;

    // face buttons
    if (B.HOTCUE[control] !== undefined) {
        // PS held long enough + X / circle / square = loop shortcut on the ACTIVE deck
        if (B.LOOP[control] !== undefined) {
            if (pressed && B.ps.down && (Date.now() - B.ps.t) > B.PS_HOLD_MS) {
                B.ps.used = true;
                var lg = B.chan(), lk = B.LOOP[control];
                // X: if a loop is already running on this deck, this press turns it OFF
                // (Mixxx's "Toggle Loop On/Off", keyboard 4 / 9) instead of making a new one.
                if (lk === "beatloop_4_activate" && engine.getValue(lg, "loop_enabled")) {
                    lk = "reloop_toggle";
                }
                B.loopHeld[control] = {g: lg, k: lk};
                engine.setValue(lg, lk, 1);
                return;
            }
            if (!pressed && B.loopHeld[control] !== undefined) {
                engine.setValue(B.loopHeld[control].g, B.loopHeld[control].k, 0);
                delete B.loopHeld[control];
                return;
            }
        }

        // otherwise: hot cues on the hot-cue deck (hold to preview, like the keyboard keys).
        // STICKY: hold a hot cue button long enough and it stays "held" after you let go;
        // pressing it again lets go.
        var key = "hotcue_" + B.HOTCUE[control] + "_activate";
        if (pressed) {
            if (B.sticky[control]) {
                engine.setValue(B.sticky[control], key, 0);     // 2nd press: release the sticky hot cue
                delete B.sticky[control];
                B.skipRelease[control] = true;                  // ignore the release that ends THIS press
                return;
            }
            B.padDeck[control] = B.hotDeck;
            B.padT[control] = Date.now();
            engine.setValue("[Channel" + B.hotDeck + "]", key, 1);
        } else if (B.skipRelease[control]) {
            delete B.skipRelease[control];
        } else if (B.padDeck[control] !== undefined) {
            var hg = "[Channel" + B.padDeck[control] + "]";
            if (B.STICKY_ENABLED && (Date.now() - B.padT[control]) >= B.STICKY_HOLD_MS) {
                B.sticky[control] = hg;                         // held long enough: keep it held
                print("DJ Hero sticky hot cue " + B.HOTCUE[control] + " on " + hg + " (press again to release)");
            } else {
                engine.setValue(hg, key, 0);                    // normal quick press: release now
            }
            delete B.padDeck[control];
        }
        return;
    }

    var g = B.chan();
    switch (control) {
    case 41:  // platter red: sync lock
        if (pressed) { B.toggle(g, "sync_enabled"); }
        break;
    case 40:  // platter green: cue (hold)
        if (pressed) { B.cueDeck = B.deck; }
        engine.setValue("[Channel" + B.cueDeck + "]", "cue_default", pressed ? 1 : 0);
        break;
    case 42:  // platter blue: headphone cue
        if (pressed) { B.toggle(g, "pfl"); }
        break;
    case 50:  // euphoria: next effect
        if (pressed) { B.nextEffect(); }
        break;
    case 13:  // PS: tap = switch which deck the hot cues control; hold + face button = loops
        B.psButton(pressed);
        break;
    case 9:   // select: play/pause deck 1
        if (pressed) { B.toggle("[Channel1]", "play"); }
        break;
    case 10:  // start: play/pause deck 2
        if (pressed) { B.toggle("[Channel2]", "play"); }
        break;
    case 20: B.tempo(true, pressed); break;               // d-pad up
    case 22: B.tempo(false, pressed); break;              // d-pad down
    case 21: if (pressed) { B.switchDeck(); } break;      // d-pad right
    case 23: if (pressed) { B.fxApply(); } break;         // d-pad left
    }
};
