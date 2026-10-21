"""OpenHamShack's controllers: what a press or a turn does, which control does it,
and what the screen keys show. Stdlib only, like the daemon.

Three layers, so that a new piece of hardware or a remapped key never
touches the transmit logic:
- controllers/ (drivers): a model's layout, finding it, its raw reports
  turned into (kind, control, step) events, and drawing on its screens.
- ACTIONS (here): everything a control can do, each carrying its own rules.
  Transmit's arming lives in the action, not in a key: whichever control it
  is mapped to, going on air takes two presses within ARM_SECONDS and on air
  one press only stops.
- The mapping (here): a model's default_map, the operator's changes on top
  (settings `controllerMaps`), up to MAX_PAGES pages, all checked against
  the action list and the model's layout every time it is read, so a stored
  map that a later version cannot honour falls back rather than misfires.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:          # controllers/ sits beside this file, on a box and in a checkout
    sys.path.insert(0, _HERE)

import controllers                 # noqa: E402
from controllers import base      # noqa: E402

# --- What the controls change -------------------------------------------------
TX_PRESETS = [146.5, 437.0, 1255.0, 2405.25]
# (frequency, LNB offset); None is the operator's own QO-100 LNB LO
# (qo100LnbMHz), set on the console's QO-100 page - not every LNB is 9750.
RX_PRESETS = [(146.5, 0), (437.0, 0), (1255.0, 0), (10491.5, None)]
MODCODS = [("qpsk", "1/2"), ("qpsk", "2/3"), ("8psk", "3/4")]
SYMBOL_RATES = [125, 250, 333, 500, 1000, 1500, 2000, 3000, 4000]
SOURCES = ["testcard", "webcam", "file", "stream"]
SOURCE_LABEL = {"testcard": "CARD", "webcam": "CAM", "file": "FILE", "stream": "OBS"}
ARM_SECONDS = 4.0
MAX_PAGES = 4
DEFAULT_MODEL = "ajazz-akp03e"
GREY, SLATE, BLACK = "0x1f2937", "0x5b6472", "0x000000"
RED, AMBER, GREEN, BLUE = "0xc4231b", "0xb26a00", "0x137a3c", "0x1554c0"
# Aiming faces: red below the decode threshold, amber within the advice's
# 2.5 dB fade margin, green above (the daemon's aim_band).
BAND_BG = {"bad": RED, "marginal": AMBER, "good": GREEN}


def next_in(seq: list, current, step: int = 1):
    try:
        i = seq.index(current)
    except ValueError:
        i = -1 if step > 0 else 0
    return seq[(i + step) % len(seq)]


# K6 shows the box's address for the first minutes after boot, when an
# operator who cannot see the box's screen needs it to open the console. A
# key's two lines hold about seven characters each, so a dotted quad is
# split after its second octet.
BOOT_ADDRESS_SECONDS = 180


def boot_address(net: dict | None) -> tuple[str, str] | None:
    if not net or net.get("uptime", 1e9) >= BOOT_ADDRESS_SECONDS:
        return None
    for kind in ("wired", "wifi"):
        for i in net.get("interfaces") or []:
            if i.get("type") == kind and i.get("ipv4"):
                parts = str(i["ipv4"][0]["address"]).split(".")
                return ".".join(parts[:2]), ".".join(parts[2:])
    return None


# --- Actions --------------------------------------------------------------------
class Event:
    """One press or turn as an action sees it. Status is fetched only when
    an action asks: a knob sends a report per detent, and status() is the
    daemon's heaviest call."""

    def __init__(self, ctl: "Controller", cfg: dict, now: float, step: int, screen: bool,
                 st: dict | None = None):
        self.ctl, self.cfg, self.now, self.step, self.screen = ctl, cfg, now, step, screen
        self._st = st

    @property
    def st(self) -> dict:
        if self._st is None:
            self._st = self.ctl.api["status"]()
        return self._st


class Action:
    """kind 'press' or 'turn'. run(event) does it; face(ctl, st, now) is
    what a screen key holding it shows, (line1, line2, bg[, big]), or None
    for the plain `short` face. needs: daemon functions it calls, so a box
    whose daemon lacks one never offers it. single: at most one control may
    hold it, the same one on every page."""

    def __init__(self, aid: str, label: str, kind: str, run, face=None, short=("", ""),
                 needs: tuple = (), single: bool = False, group: str = ""):
        self.id, self.label, self.kind, self.run, self.face = aid, label, kind, run, face
        self.short, self.needs, self.single, self.group = short, needs, single, group

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "name": NAMES.get(self.id) or self.label.split(" (")[0],
                "kind": self.kind, "group": self.group, "single": self.single}


ACTIONS: dict[str, Action] = {}


def action(aid: str, label: str, kind: str = "press", **kw):
    def register(fn):
        ACTIONS[aid] = Action(aid, label, kind, fn, **kw)
        return fn
    return register


def face_of(aid: str):
    def attach(fn):
        ACTIONS[aid].face = fn
        return fn
    return attach


@action("none", "Nothing", short=("", ""), group="Other")
def _none(e: Event) -> None:
    pass


@action("none-turn", "Nothing", kind="turn", group="Other")
def _none_turn(e: Event) -> None:
    pass


@action("transmit", "Transmit: press, then again within 4 s; press to stop", single=True,
        group="Transmit")
def _transmit(e: Event) -> None:
    c = e.ctl
    if e.st.get("onAir"):
        c.api["ptt"](False)
        c.armed_until = 0.0
    elif e.now < c.armed_until:
        c.armed_until = 0.0
        c.api["ptt"](True)              # set_ptt applies the licence guard
    else:
        c.armed_until = e.now + ARM_SECONDS
        if not e.screen:
            # A key with no picture cannot say ARM itself; the status key can.
            c.note("ARM", "again", e.now)


@face_of("transmit")
def _transmit_face(c: "Controller", st: dict, now: float) -> tuple:
    on_air = st.get("onAir")
    timeout = st.get("txTimeout") or {}
    if on_air and timeout.get("warning"):
        # The time-out's last minute, counted down here too. Keeping on
        # air is the console's button: this key still only stops.
        return "ON AIR", f"off in {timeout.get('secondsLeft', 0)} s", RED
    if on_air:
        # With a PTT output, whether the PA/relay is keyed too: a relay
        # that did not close is worth seeing at the key that stops it.
        ptt = st.get("pttOutput") or {}
        return "ON AIR", "" if ptt.get("mode", "off") == "off" else "PA on" if ptt.get("on") else "PA off", RED
    if now < c.armed_until:
        return "ARM", "again", AMBER
    if st.get("callsignNeeded"):
        # Said rather than "blocked": a new box's first press of TX
        # should send its operator to the callsign, not the licence.
        return "SET", "CALL", SLATE
    if st.get("txBlocked"):
        return "TX", "blocked", SLATE
    return "TX", "", GREY


@action("preview", "Preview on/off", group="Transmit")
def _preview(e: Event) -> None:
    e.ctl.api["preview"](not e.st.get("previewing"))


@face_of("preview")
def _preview_face(c, st, now):
    return "PREV", "on" if st.get("previewing") else "off", BLUE if st.get("previewing") else GREY


@action("record", "Record the received signal on/off", group="Receive")
def _record(e: Event) -> None:
    e.ctl.api["save_patch"]({"rxRecord": not e.cfg.get("rxRecord")})


@face_of("record")
def _record_face(c, st, now):
    cfg = st.get("config", {})
    rec = (st.get("rx") or {}).get("recording") or {}
    return ("REC", "on" if rec.get("file") else ("wait" if cfg.get("rxRecord") else "off"),
            RED if rec.get("file") else (AMBER if cfg.get("rxRecord") else GREY))


@action("source", "Picture source: test card, camera, video file, stream", group="Transmit")
def _source(e: Event) -> None:
    e.ctl.api["save_patch"]({"source": next_in(SOURCES, e.cfg.get("source", "testcard"))})


@face_of("source")
def _source_face(c, st, now):
    return SOURCE_LABEL.get(st.get("config", {}).get("source"), "SRC"), "source", GREY


@action("screens", "Picture / Split (one screen)", group="Screens")
def _screens(e: Event) -> None:
    e.ctl.api["toggle_screens"]()


@face_of("screens")
def _screens_face(c, st, now):
    chosen = (st.get("screens") or {}).get("chosen") or "split"
    return {"picture": "PICT", "console": "CONS"}.get(chosen, "SPLIT"), "screen", GREY


@action("aim", "Receive status (the address for 3 minutes after boot); press for aiming, again to stop",
        group="Receive")
def _aim(e: Event) -> None:
    # K6 was display-only; aiming is what a receive-status key is for at
    # the mast, and the transmit keys stay where they are. Without aiming
    # in the daemon it is the status display it always was.
    if "aim_set" in e.ctl.api:
        e.ctl.api["aim_set"](not e.st.get("aiming"))


@face_of("aim")
def _aim_face(c, st, now):
    if st.get("aiming") and "aim" in c.api:
        a = c.aim_reading()
        if a.get("locked"):
            # SDT names are often "CALL-Name"; the callsign is what fits.
            return "LOCK", (a.get("name") or "aiming").split("-")[0][:7], GREEN
        return "AIM", "search", SLATE
    rx = st.get("rx") or {}
    if c.flash and now < c.flash[2]:
        return c.flash[0], c.flash[1], BLUE
    if rx.get("locked"):
        return "LOCK", f"{rx.get('mer') or 0:.0f} dB", GREEN
    addr = boot_address(st.get("network"))
    if addr:
        return (*addr, BLUE)
    return "RX", "search", GREY


def _num(v: float) -> str:
    # A margin of 10 dB or more drops its decimal so it still fits a key at
    # a size readable at arm's length.
    return f"{v:+.0f}" if abs(v) >= 9.95 else f"{v:+.1f}"


@action("aim-margin", "Aiming: the margin (shows only)", needs=("aim",), group="Receive")
def _aim_margin(e: Event) -> None:
    pass                            # showing the margin: a press must not change anything


@face_of("aim-margin")
def _aim_margin_face(c, st, now):
    a = c.aim_reading() if st.get("aiming") else {}
    if not st.get("aiming"):
        return "MARGIN", "aim off", GREY
    if a.get("locked") and a.get("marginDb") is not None:
        return _num(a["marginDb"]), "margin", BAND_BG[a["band"]], True
    if a.get("locked") and a.get("merDb") is not None:
        return f"{a['merDb']:.1f}", "MER dB", GREY, True
    if a.get("level") is not None:
        # Grey, not red: red is a margin below zero, and this number is
        # the rough AGC level (agc_level), something to turn for, not dB.
        return f"{a['level']:.0f}", "no lock", SLATE, True
    return "NO", "lock", RED


@action("aim-peak", "Aiming: the peak; press to reset it", needs=("aim",), group="Receive")
def _aim_peak(e: Event) -> None:
    if e.st.get("aiming") and "aim_reset" in e.ctl.api:
        e.ctl.api["aim_reset"]()
        e.ctl.note("PEAK", "reset", e.now)


@face_of("aim-peak")
def _aim_peak_face(c, st, now):
    if not st.get("aiming"):
        return "PEAK", "aim off", GREY
    a = c.aim_reading()
    peak = a.get("peakMarginDb")
    if c.flash and now < c.flash[2] and c.flash[0] == "PEAK":
        return "PEAK", "reset", BLUE
    if peak is not None:
        return _num(peak), "peak", BAND_BG[a.get("peakBand") or "bad"], True
    return "PEAK", "none", GREY


@action("tx-preset", "Transmit band preset (next)", group="Transmit")
def _tx_preset(e: Event) -> None:
    f = next_in(TX_PRESETS, float(e.cfg.get("frequencyMHz", 0)))
    e.ctl.api["save_patch"]({"frequencyMHz": f})
    e.ctl.note("TX", f"{f:g}", e.now)


@face_of("tx-preset")
def _tx_preset_face(c, st, now):
    return "TX", f"{float(st.get('config', {}).get('frequencyMHz') or 0):g}", GREY


def _rx_presets(cfg: dict) -> list[tuple[float, float]]:
    lo = float(cfg.get("qo100LnbMHz") or 9750)
    return [(f, lo if lnb is None else lnb) for f, lnb in RX_PRESETS]


def _rx_set(e: Event, f: float, lnb: float) -> None:
    other = e.ctl.heard_receiver(e.st)
    if other:
        e.ctl.api["rx_tune"](other["id"], {"frequencyMHz": f, "offsetMHz": lnb})
        e.ctl.note(f"RX{other['id']}", f"{f:g}", e.now)
        return
    e.ctl.api["save_patch"]({"rxFrequencyMHz": f, "rxOffsetMHz": lnb})
    e.ctl.note("RX", f"{f:g}", e.now)


@action("rx-preset", "Receive band preset (next; the receiver being heard)", group="Receive")
def _rx_preset(e: Event) -> None:
    other = e.ctl.heard_receiver(e.st)
    if other:
        cur = (float(other.get("frequencyMHz") or 0), float(other.get("offsetMHz") or 0))
    else:
        cur = (float(e.cfg.get("rxFrequencyMHz", 0)), float(e.cfg.get("rxOffsetMHz", 0)))
    _rx_set(e, *next_in(_rx_presets(e.cfg), cur))


@face_of("rx-preset")
def _rx_preset_face(c, st, now):
    return "RX", f"{float(st.get('config', {}).get('rxFrequencyMHz') or 0):g}", GREY


def _preset_actions() -> None:
    """One action per preset, for a key that goes straight to a band."""
    for n, f in enumerate(TX_PRESETS, 1):
        def run(e: Event, f=f) -> None:
            e.ctl.api["save_patch"]({"frequencyMHz": f})
            e.ctl.note("TX", f"{f:g}", e.now)

        def face(c, st, now, f=f):
            on = abs(float(st.get("config", {}).get("frequencyMHz") or 0) - f) < 0.001
            return "TX", f"{f:g}", BLUE if on else GREY
        ACTIONS[f"tx-preset-{n}"] = Action(f"tx-preset-{n}", f"Transmit on {f:g} MHz", "press", run, face,
                                           group="Transmit")
    for n, (f, lnb) in enumerate(RX_PRESETS, 1):
        def run(e: Event, n=n) -> None:
            _rx_set(e, *_rx_presets(e.cfg)[n - 1])

        def face(c, st, now, f=f):
            on = abs(float(st.get("config", {}).get("rxFrequencyMHz") or 0) - f) < 0.001
            return "RX", f"{f:g}", BLUE if on else GREY
        ACTIONS[f"rx-preset-{n}"] = Action(f"rx-preset-{n}", f"Receive on {f:g} MHz" + (" (QO-100)" if lnb is None else ""),
                                           "press", run, face, group="Receive")


_preset_actions()


@action("modcod", "Transmit modulation (next)", group="Transmit")
def _modcod(e: Event) -> None:
    const, fec = next_in(MODCODS, (e.cfg.get("constellation"), e.cfg.get("fec")))
    e.ctl.api["save_patch"]({"mode": "dvbs2", "constellation": const, "fec": fec})
    e.ctl.note(const.upper(), fec, e.now)


@face_of("modcod")
def _modcod_face(c, st, now):
    cfg = st.get("config", {})
    return str(cfg.get("constellation") or "").upper() or "MOD", str(cfg.get("fec") or ""), GREY


@action("rx-cycle", "Several receivers: grid → 1 → 2 … full screen and sound", needs=("rx_cycle",), group="Receive")
def _rx_cycle(e: Event) -> None:
    if len(e.st.get("receivers") or []) > 1:
        view = e.ctl.api["rx_cycle"]()
        e.ctl.note("RX", f"{view['selected']} full" if view.get("enlarged") else "grid", e.now)


@face_of("rx-cycle")
def _rx_cycle_face(c, st, now):
    view = st.get("rxView") or {}
    return "RX", f"{view.get('selected', 1)} full" if view.get("enlarged") else "grid", GREY


@action("page", "Next page of keys", single=True, group="Other")
def _page(e: Event) -> None:
    pass                            # Controller.handle turns the page: it knows which model was pressed


@action("testcard-next", "Next test card (sent from the next key-up)", needs=("next_testcard",),
        short=("CARD", "next"), group="Transmit")
def _testcard(e: Event) -> None:
    e.ctl.note("CARD", str(e.ctl.api["next_testcard"]() or "next")[:7], e.now)


# Turn actions. Each also gives two press actions, "<id>:up" and
# "<id>:down", so a keypad with no knobs can do the same job with a pair of
# keys; a press is one detent.
@action("power", "Transmit power ±1 dB", kind="turn", group="Transmit")
def _power(e: Event) -> None:
    g = max(-70, min(0, int(e.cfg.get("gainDb", -10)) + e.step))
    e.ctl.api["save_patch"]({"gainDb": g})
    e.ctl.note("POWER", f"{g} dB", e.now)


@action("rx-tune", "Receive frequency ±0.125 MHz (the receiver being heard)", kind="turn", group="Receive")
def _rx_tune(e: Event) -> None:
    other = e.ctl.heard_receiver()
    if other:
        f = round(float(other.get("frequencyMHz") or 0) + 0.125 * e.step, 3)
        e.ctl.api["rx_tune"](other["id"], {"frequencyMHz": f})
        e.ctl.note(f"RX{other['id']}", f"{f:.3f}", e.now)
        return
    f = round(float(e.cfg.get("rxFrequencyMHz", 0)) + 0.125 * e.step, 3)
    e.ctl.api["save_patch"]({"rxFrequencyMHz": f})
    e.ctl.note("RX", f"{f:.3f}", e.now)


@action("symbol-rate", "Transmit symbol rate (standard rates)", kind="turn", group="Transmit")
def _symbol_rate(e: Event) -> None:
    sr = next_in(SYMBOL_RATES, int(e.cfg.get("symbolRateKs", 333)), e.step)
    e.ctl.api["save_patch"]({"symbolRateKs": sr})
    e.ctl.note("TX SR", f"{sr} kS", e.now)


@action("qo100-channel", "QO-100 uplink channel (band plan, at the symbol rate)", kind="turn",
        needs=("qo100_channels",), group="Transmit")
def _qo100_channel(e: Event) -> None:
    chans = e.ctl.api["qo100_channels"](float(e.cfg.get("symbolRateKs", 333)))
    if not chans:
        e.ctl.note("QO-100", "no chan", e.now)   # the rate has no channels in the plan
        return
    ups = [c["uplinkMHz"] for c in chans]
    cur = float(e.cfg.get("frequencyMHz") or 0)
    near = next((u for u in ups if abs(u - cur) < 0.001), None)
    up = next_in(ups, near, 1 if e.step > 0 else -1) if near is not None else ups[0 if e.step > 0 else -1]
    e.ctl.api["save_patch"]({"frequencyMHz": up})
    down = next(c["downlinkMHz"] for c in chans if c["uplinkMHz"] == up)
    e.ctl.note("QO CH", f"{down:.2f}", e.now)


TURN_SHORT = {"power": ("PWR", "dB"), "rx-tune": ("RX", "MHz"), "symbol-rate": ("SR", "kS"),
              "qo100-channel": ("QO CH", "")}


def _press_pairs() -> None:
    for t in [a for a in ACTIONS.values() if a.kind == "turn" and a.id != "none-turn"]:
        for word, step, sign in (("up", +1, "+"), ("down", -1, "−")):
            def run(e: Event, t=t, step=step) -> None:
                t.run(Event(e.ctl, e.cfg, e.now, step, e.screen, e._st))
            a, b = TURN_SHORT.get(t.id, (t.id.upper()[:6], ""))
            ACTIONS[f"{t.id}:{word}"] = Action(f"{t.id}:{word}", f"{t.label.split(' ±')[0].split(' (')[0]}: {word}",
                                               "press", run, short=(a, f"{sign} {b}".strip()),
                                               needs=t.needs, group=t.group)


_press_pairs()
# What the console's drawing writes on a control: a word or two that fits.
NAMES = {"transmit": "Transmit", "preview": "Preview", "record": "Record", "source": "Source",
         "screens": "Screens", "aim": "Status / aim", "aim-margin": "Margin", "aim-peak": "Peak",
         "tx-preset": "TX band", "rx-preset": "RX band", "modcod": "TX mode", "rx-cycle": "Receivers",
         "page": "Next page", "testcard-next": "Test card", "power": "Power", "rx-tune": "RX tune",
         "symbol-rate": "Symbol rate", "qo100-channel": "QO-100 ch", "none": "—", "none-turn": "—",
         **{f"tx-preset-{n}": f"TX {f:g}" for n, f in enumerate(TX_PRESETS, 1)},
         **{f"rx-preset-{n}": f"RX {f:g}" for n, (f, _) in enumerate(RX_PRESETS, 1)}}
for _t in ("power", "rx-tune", "symbol-rate", "qo100-channel"):
    NAMES[f"{_t}:up"], NAMES[f"{_t}:down"] = f"{NAMES[_t]} +", f"{NAMES[_t]} −"
# Shown in the console's picker in this order.
GROUPS = ["Transmit", "Receive", "Screens", "Other"]
# Never hidden under aiming's margin and peak, nor moved by it.
PROTECTED = {"transmit", "aim", "page"}


def available(api: dict) -> dict[str, Action]:
    return {k: a for k, a in ACTIONS.items() if all(n in api for n in a.needs)}


# --- The mapping ---------------------------------------------------------------
def stored_pages(cfg: dict, model: str) -> list[dict]:
    m = (cfg.get("controllerMaps") or {}) if isinstance(cfg.get("controllerMaps"), dict) else {}
    pages = (m.get(model) or {}).get("pages") if isinstance(m.get(model), dict) else None
    if not isinstance(pages, list) or not pages:
        return [{}]
    return [p if isinstance(p, dict) else {} for p in pages[:MAX_PAGES]]


def resolve(drv, cfg: dict, api: dict) -> tuple[list[dict], list[str]]:
    """The mapping in force: one {control: {slot: action}} per page, and
    notes on anything stored that could not be used. Page 1 is the model's
    default with the operator's changes; a later page starts as page 1 and
    has its own changes. Every check is made here, on read, so a stored map
    from another version, a restore or a hand edit can only fall back."""
    acts = available(api)
    notes: list[str] = []

    def blank(kind: str) -> dict:
        return {s: ("none" if s == "press" else "none-turn") for s in base.slots(kind)}
    first = {c["id"]: {**blank(c["kind"]), **drv.default_map.get(c["id"], {})} for c in drv.controls}
    for cid, b in first.items():            # a default this daemon cannot do is nothing
        for slot, aid in list(b.items()):
            if aid not in acts:
                b[slot] = "none" if slot == "press" else "none-turn"
    raw = stored_pages(cfg, drv.model)
    pages: list[dict] = []
    explicit: set[str] = set()
    for i, over in enumerate(raw):
        page = {cid: dict(b) for cid, b in (first if i == 0 else pages[0]).items()}
        for cid, binding in over.items():
            where = f"{cid}" + (f" on page {i + 1}" if i else "")
            kind = drv.control_kind(cid) if isinstance(cid, str) else None
            if not kind:
                notes.append(f"{cid}: not a control on the {drv.name}; left out")
                continue
            if not isinstance(binding, dict):
                continue
            for slot, aid in binding.items():
                if slot not in base.slots(kind):
                    notes.append(f"{where}: a {kind} cannot {slot}; left out")
                elif aid not in ACTIONS:
                    notes.append(f"{where}: {aid!r} is not in this version; using the default")
                elif aid not in acts:
                    notes.append(f"{where}: {ACTIONS[aid].label} is not available here; using the default")
                elif ACTIONS[aid].kind != slot:
                    notes.append(f"{where}: {ACTIONS[aid].label} is a {ACTIONS[aid].kind} action; using the default")
                elif ACTIONS[aid].single and i:
                    notes.append(f"{where}: {ACTIONS[aid].label} can only be set on page 1; left out")
                else:
                    page.setdefault(cid, blank(kind))[slot] = aid
                    if i == 0:
                        explicit.add(cid)
        if i == 0:
            # One control each for transmit and the page key. A default
            # holder gives way to the operator's choice (moving TX to B1
            # clears K1); two explicit holders keep the first.
            for aid in [a for a in acts if acts[a].single]:
                holders = [cid for cid, b in page.items() if b.get("press") == aid]
                if len(holders) > 1:
                    chosen = [c for c in holders if c in explicit] or holders
                    keep = chosen[0]
                    if len([c for c in holders if c in explicit]) > 1:
                        notes.append(f"{ACTIONS[aid].label.split(':')[0]} was on {', '.join(holders)}; kept on {keep}")
                    for c in holders:
                        if c != keep:
                            page[c]["press"] = "none"
        else:
            # Transmit and the page key are on the same control on every
            # page, whatever the page says: the stop key never moves.
            for cid, b in pages[0].items():
                if acts.get(b.get("press", "")) and acts[b["press"]].single:
                    if page[cid].get("press") != b["press"]:
                        notes.append(f"{cid} on page {i + 1}: keeps {ACTIONS[b['press']].label.split(':')[0]} on every page")
                    page[cid]["press"] = b["press"]
                    for c2, b2 in page.items():
                        if c2 != cid and b2.get("press") == b["press"]:
                            b2["press"] = "none"
        pages.append(page)
    if len(pages) > 1 and not any(b.get("press") == "page" for b in pages[0].values()):
        notes.append(f"Pages 2-{len(pages)} cannot be reached: no control is set to Next page")
    return pages, notes


def find_holder(page: dict, aid: str) -> str | None:
    return next((cid for cid, b in page.items() if aid in b.values()), None)


class Controller:
    """Runs every connected controller: events to actions, faces to screens.

    `api` supplies the daemon's own functions (no import cycle): load(),
    status(), save_patch(dict), ptt(bool), preview(bool), toggle_screens(),
    for aiming aim() (the reading), aim_set(bool) and aim_reset(), for
    several receivers rx_cycle() and rx_tune(id, patch), and optionally
    qo100_channels(sr) and next_testcard(). An action whose function is
    missing is not offered."""

    def __init__(self, api: dict, brightness: int = 60, drivers: list | None = None,
                 keypad_list: Path | None = None):
        self.api = api
        self.drivers = drivers if drivers is not None else controllers.DRIVERS
        self.brightness = brightness
        self.armed_until = 0.0
        self.flash: tuple[str, str, float] | None = None   # transient status-key message
        self.message = "No controller"
        self.lock = threading.RLock()
        self.devices: dict[str, object] = {}              # path -> open driver instance
        self.page_at: dict[str, int] = {}                 # model -> page showing
        self.pressed: dict | None = None                  # the last control used, for the console
        self.seen_keys: dict[str, list[str]] = {}         # open-layout models: keys pressed
        self.keypad_list = keypad_list or Path("/etc/openhamshack/keypads")
        self.generation = 0                               # bumped when the mapping changes: redraw now
        self._aim: tuple[float, dict] | None = None

    def driver(self, model: str):
        return next((d for d in self.drivers if d.model == model), None) or controllers.by_model(model)

    # Mapping ----------------------------------------------------------------------
    def profile(self, model: str, cfg: dict | None = None) -> tuple[list[dict], list[str]]:
        return resolve(self.driver(model), self.api["load"]() if cfg is None else cfg, self.api)

    def page_of(self, model: str, count: int) -> int:
        p = self.page_at.get(model, 0)
        if p >= count:                      # pages taken away under it
            p = self.page_at[model] = 0
        return p

    def overlay(self, drv, page: dict, st: dict) -> dict[str, str]:
        """While aiming: the margin and the peak on screen keys, unless the
        page already shows them. Never over transmit, aiming or the page
        key, so the stop key and the way out of aiming stay where they are."""
        if not st.get("aiming"):
            return {}
        screens = [c["id"] for c in drv.controls if c["kind"] == "screen-key"]
        order = [c for c in drv.aim_overlay if c in screens] + [c for c in screens if c not in drv.aim_overlay]
        shown = {page.get(c, {}).get("press") for c in screens}
        free = [c for c in order if page.get(c, {}).get("press") not in PROTECTED
                and page.get(c, {}).get("press") not in ("aim-margin", "aim-peak")]
        out = {}
        for aid in ("aim-margin", "aim-peak"):
            if aid not in shown and free:
                out[free.pop(0)] = aid
        return out

    # Events ------------------------------------------------------------------------
    def handle(self, event: tuple[str, str, int], now: float, model: str = DEFAULT_MODEL) -> None:
        kind, cid, step = event
        drv = self.driver(model)
        with self.lock:
            self.pressed = {"model": model, "control": cid, "at": time.time()}
            if drv.open_layout and drv.has_control(cid):
                keys = self.seen_keys.setdefault(model, [])
                if cid not in keys:
                    keys.append(cid)
                    del keys[:-24]
            if kind not in ("press", "turn"):
                return
            cfg = self.api["load"]()
            pages, _ = resolve(drv, cfg, self.api)
            p = self.page_of(model, len(pages))
            kind_of = drv.control_kind(cid)
            binding = pages[p].get(cid) or ({"press": "none"} if kind_of else {})
            screen = kind_of == "screen-key"
            if kind == "turn":
                aid = binding.get("turn", "none-turn")
                ACTIONS[aid].run(Event(self, cfg, now, step, screen))
                return
            e = Event(self, cfg, now, 0, screen)
            aid = self.overlay(drv, pages[p], e.st).get(cid) or binding.get("press", "none")
            if aid == "page":
                p = self.page_at[model] = (p + 1) % len(pages)
                self.note("PAGE", f"{p + 1} of {len(pages)}", now)
                return
            ACTIONS[aid].run(e)

    def heard_receiver(self, st: dict | None = None) -> dict | None:
        """The receiver being heard when it is not receiver 1, whose settings
        are the rx* keys the knob and B2 have always changed; None otherwise."""
        if "rx_tune" not in self.api:
            return None
        st = self.api["status"]() if st is None else st
        sel = (st.get("rxView") or {}).get("selected", 1)
        if sel == 1:
            return None
        return next((r for r in st.get("receivers") or [] if r.get("id") == sel), None)

    def note(self, a: str, b: str, now: float) -> None:
        self.flash = (a, b, now + 2.5)

    def aim_reading(self) -> dict:
        """The aiming reading, once per drawing of the faces."""
        if self._aim is None:
            self._aim = (0.0, self.api["aim"]() if "aim" in self.api else {})
        return self._aim[1]

    # Screens -----------------------------------------------------------------------
    def face_for(self, aid: str, st: dict, now: float, page: int, pages: int) -> tuple:
        a = ACTIONS.get(aid) or ACTIONS["none"]
        if aid == "page":
            return "PAGE", f"{page + 1} of {pages}", BLUE if page else GREY
        if aid == "none":
            return "", "", BLACK
        if a.face:
            return a.face(self, st, now)
        return a.short[0], a.short[1], GREY

    def faces(self, now: float, model: str = DEFAULT_MODEL) -> dict[str, tuple]:
        """What each screen key shows: (line1, line2, bg[, big])."""
        drv = self.driver(model)
        st = self.api["status"]()
        pages, _ = resolve(drv, self.api["load"](), self.api)
        p = self.page_of(model, len(pages))
        page = pages[p]
        over = self.overlay(drv, page, st)
        self._aim = None
        out = {}
        for c in drv.controls:
            if c["kind"] == "screen-key":
                out[c["id"]] = self.face_for(over.get(c["id"]) or page[c["id"]].get("press", "none"),
                                             st, now, p, len(pages))
        self._aim = None
        return out

    def refresh(self, dev, now: float) -> None:
        if not type(dev).screen:
            return
        for cid, face in self.faces(now, type(dev).model).items():
            dev.show(cid, face)

    # Changing the mapping (the console) --------------------------------------------
    def _maps(self, cfg: dict) -> dict:
        m = cfg.get("controllerMaps")
        return {k: v for k, v in m.items() if isinstance(v, dict)} if isinstance(m, dict) else {}

    def _save(self, maps: dict) -> None:
        self.api["save_patch"]({"controllerMaps": maps})
        self.generation += 1

    def _on_air_guard(self, model: str, cfg: dict, touches) -> None:
        """While on air the transmit key stays exactly where it is: moving or
        removing it then would leave the operator pressing a key that no
        longer stops the carrier."""
        st = self.api["status"]()
        if not st.get("onAir"):
            return
        pages, _ = self.profile(model, cfg)
        holder = find_holder(pages[0], "transmit")
        if touches(holder):
            raise ValueError("Not while on air: the transmit key stays where it is until you are off air")

    def set_binding(self, model: str, page: int, cid: str, slot: str, aid: str) -> None:
        """One control's action on one page. ValueError, before anything is
        saved, for something the mapping could not use."""
        drv = self.driver(model)
        if not drv:
            raise ValueError("Not a controller this version knows")
        with self.lock:
            cfg = self.api["load"]()
            pages, _ = self.profile(model, cfg)
            if not isinstance(page, int) or not 0 <= page < len(pages):
                raise ValueError("No such page")
            kind = drv.control_kind(cid) if isinstance(cid, str) else None
            if not kind:
                raise ValueError(f"{cid} is not a control on the {drv.name}")
            if slot not in base.slots(kind):
                raise ValueError(f"A {kind} cannot {slot}")
            acts = available(self.api)
            if aid not in acts:
                raise ValueError("Not an action this box offers")
            a = acts[aid]
            if a.kind != slot:
                raise ValueError(f"{a.label} is a {a.kind} action")
            if a.single and page:
                raise ValueError(f"{a.label.split(':')[0]} can only be set on page 1; it is the same key on every page")
            held = pages[0].get(cid, {}).get(slot)
            if page and acts.get(held or "") and acts[held].single and held != aid:
                raise ValueError(f"{cid} is {acts[held].label.split(':')[0]} on every page; change it on page 1")
            if slot == "press":
                self._on_air_guard(model, cfg, lambda h: (h == cid and aid != "transmit") or
                                   (aid == "transmit" and h != cid))
            maps = self._maps(cfg)
            mine = dict(maps.get(model) or {})
            over = [dict(p) for p in stored_pages(cfg, model)]
            while len(over) < len(pages):
                over.append({})
            if a.single:
                # Moving it: wherever it was becomes nothing, explicitly, so
                # the default holder does not come back with it.
                for c, b in pages[0].items():
                    if c != cid and b.get("press") == aid:
                        over[0].setdefault(c, {})
                        over[0][c] = {**over[0][c], "press": "none"}
            entry = {**(over[page].get(cid) or {}), slot: aid}
            over[page][cid] = entry
            mine["pages"] = over
            maps[model] = mine
            self._save(maps)

    def set_pages(self, model: str, count: int) -> None:
        if not self.driver(model):
            raise ValueError("Not a controller this version knows")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_PAGES:
            raise ValueError(f"Between 1 and {MAX_PAGES} pages")
        with self.lock:
            cfg = self.api["load"]()
            maps = self._maps(cfg)
            over = [dict(p) for p in stored_pages(cfg, model)][:count]
            while len(over) < count:
                over.append({})
            maps[model] = {**(maps.get(model) or {}), "pages": over}
            if self.page_at.get(model, 0) >= count:
                self.page_at[model] = 0
            self._save(maps)

    def reset(self, model: str) -> None:
        """Back to the model's own layout, one page."""
        with self.lock:
            cfg = self.api["load"]()
            self._on_air_guard(model, cfg, lambda h: h != find_holder(
                resolve(self.driver(model), {}, self.api)[0][0], "transmit"))
            maps = self._maps(cfg)
            maps.pop(model, None)
            self.page_at[model] = 0
            self._save(maps)

    def set_keypads(self, ids: list) -> None:
        """The keyboards/keypads the operator has chosen to use as
        controllers, saved and written where openhamshack-keypad (udev) reads
        them. Only devices plugged in now can be chosen: the console offers
        what it sees, and a stale id would grant a device nobody picked."""
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("A list of devices")
        offered = {c["id"] for c in controllers.keypad.candidates()}
        kept = set((self.api["load"]().get("controllerKeypads") or []))
        bad = [i for i in ids if i not in offered and i not in kept]
        if bad:
            raise ValueError("That keyboard is not plugged in")
        ids = sorted(set(ids))[:8]
        self.keypad_list.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.keypad_list.with_suffix(".new")
        tmp.write_text("".join(i.replace("\n", "") + "\n" for i in ids))
        tmp.replace(self.keypad_list)
        self.api["save_patch"]({"controllerKeypads": ids})

    # What the console shows ---------------------------------------------------------
    def info(self, faces: bool = False) -> dict:
        """For the console. faces: also what each screen key shows now (it
        asks the daemon's status, so diagnostics leave it out)."""
        cfg = self.api["load"]()
        with self.lock:
            connected = {type(d).model: d.path for d in self.devices.values() if d is not None}
        acts = available(self.api)
        models = []
        for drv in self.drivers:
            pages, notes = resolve(drv, cfg, self.api)
            p = self.page_of(drv.model, len(pages))
            models.append({**drv.describe(), "connected": drv.model in connected,
                           "path": connected.get(drv.model), "page": p, "pages": pages, "notes": notes,
                           "default": resolve(drv, {}, self.api)[0][0],
                           "faces": self.faces(time.monotonic(), drv.model) if faces and drv.screen else None,
                           "seenKeys": [k for k in self.seen_keys.get(drv.model, [])
                                        if not any(c["id"] == k for c in drv.controls)]})
        current = next(iter(connected), None) or cfg.get("padLastModel") or DEFAULT_MODEL
        first = next((m for m in models if m["model"] == current), models[0] if models else None)
        keys = []
        if first:   # the old flat list, for a console from before remapping
            for c in first["controls"]:
                for slot, aid in first["pages"][first["page"]].get(c["id"], {}).items():
                    if aid not in ("none", "none-turn"):
                        keys.append({"id": c["id"] + (" turn" if slot == "turn" else ""), "action": aid,
                                     "label": ACTIONS[aid].label})
        try:
            kp = controllers.keypad.candidates()
        except Exception:
            kp = []
        chosen = set(cfg.get("controllerKeypads") or [])
        return {"connected": bool(connected), "model": first["name"] if connected and first else None,
                "current": current, "keys": keys, "brightness": self.brightness, "message": self.message,
                "controllers": models, "maxPages": MAX_PAGES,
                "actions": [acts[k].public() for k in acts if k not in ("none", "none-turn")],
                "groups": GROUPS, "pressed": self.pressed,
                "keypads": [{**k, "chosen": k["id"] in chosen, "ready": os.access(k["node"], os.R_OK)}
                            for k in kp] + [{"id": i, "name": i.split(":", 2)[-1], "chosen": True, "ready": False,
                                             "unplugged": True} for i in sorted(chosen - {k["id"] for k in kp})]}

    def set_brightness(self, level: int) -> None:
        self.brightness = max(0, min(100, int(level)))
        with self.lock:
            devs = list(self.devices.values())
        for d in devs:
            try:
                d.set_brightness(self.brightness)
            except OSError:
                pass

    # Loop ----------------------------------------------------------------------------
    def run(self) -> None:
        """Look for controllers every few seconds; serve each on its own
        thread until it is unplugged, then look for it again."""
        while True:
            try:
                cfg = self.api["load"]()
            except Exception:
                cfg = {}
            for drv in self.drivers:
                try:
                    paths = drv.find(cfg)
                except Exception:
                    paths = []
                for path in paths:
                    with self.lock:
                        if path in self.devices:
                            continue
                        self.devices[path] = None      # claimed: the next round must not open it again
                    threading.Thread(target=self.serve, args=(drv, path), daemon=True).start()
            with self.lock:
                if not self.devices:
                    self.message = "No controller connected"
            time.sleep(3)

    def serve(self, drv, path: str) -> None:
        dev = None
        try:
            dev = drv(path)
            dev.start(self.brightness)
            with self.lock:
                self.devices[path] = dev
                self.message = f"Connected ({path})"
            if self.api["load"]().get("padLastModel") != drv.model:
                self.api["save_patch"]({"padLastModel": drv.model})
            last, gen = 0.0, self.generation
            while True:
                events = dev.read(0.25)
                now = time.monotonic()
                for event in events:
                    try:
                        self.handle(event, now, drv.model)
                    except Exception as exc:  # an action failing must not drop the controller
                        self.message = f"Last action failed: {exc}"[:120]
                    last = 0.0                # redraw at once after a press
                if gen != self.generation:    # remapped from the console: faces now, not in half a second
                    gen, last = self.generation, 0.0
                if now - last >= 0.5:
                    self.refresh(dev, now)
                    last = now
        except OSError as exc:
            self.message = f"Controller disconnected ({exc.strerror or exc})"
        finally:
            if dev:
                dev.close()
            with self.lock:
                self.devices.pop(path, None)
        time.sleep(2)
