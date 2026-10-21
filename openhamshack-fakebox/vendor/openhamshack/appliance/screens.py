#!/usr/bin/env python3
"""Arrange the receive picture and the console across however many screens
are connected, and again whenever that changes.

Two screens: mpv full screen on $rx, the console full screen on $ui.

One screen (docs/ui-review.md, "Proposed one-screen layout"): the picture
is part of the console, not a window floating over it. Three modes, the
console's header switch and openhamshack.conf's oneScreen:
  split    the console keeps a column on the left free for the picture
           (its #picslot), with the receive status under it, and the page
           on the right; nothing overlaps. The default with a receiver.
  picture  the picture fills the screen above a bar the console keeps at
           the bottom (Console, TRANSMIT), which may hide itself; the
           console then reports the whole screen. Replaces "video only".
  console  no picture: today's console. Receivers still play (and sound),
           parked on workspace 9. The only mode without a receiver.
The console reports where its slot is (POST /api/screens/slot -> the
daemon writes $XDG_RUNTIME_DIR/openhamshack-slot.json and sends sway a tick),
and mpv is put exactly there. Until it has, a slot is worked out the same
way the console's CSS works it out. The transmit preview, while it runs,
takes the same slot ("Preview here") and the receive pictures wait parked.

Several receivers: each has its own mpv (app_id mpv for receiver 1,
mpv-rx2..mpv-rx4), with a window only while it plays, and the windows present
are tiled - two side by side, three or four in quarters - wherever the single
picture would have been: the TV, or the slot. The daemon names one receiver
"enlarged" in $XDG_RUNTIME_DIR/openhamshack-rxview.json; that one then takes the
single picture's place and the others wait, still playing, on workspace 9.

sway config can only express the two-screen case statically: a floating
window cannot sit over a full-screen one, so the one-screen layout needs the
console un-fullscreened, which depends on the output count at run time.
Run by sway: exec_always python3 /opt/openhamshack/screens.py $rx $ui
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# sway criteria are regular expressions: unanchored, "mpv" would also match
# the transmit preview's "mpv-preview".
MPV = '[app_id="^mpv$"]'
PREVIEW = '[app_id="^mpv-preview$"]'
UI = '[app_id="^(chromium|chrome-)"]'
MARGIN = 16
# The console's footer (Preview + TRANSMIT) as a share of the screen height,
# for a picture placed before the console has said where its slot is.
# 0.13 was measured at 1080; the footer is a fixed 144 px, so at a laptop's
# 768 it is 0.19 and a picture-in-picture sat on top of TRANSMIT.
FOOTER = 0.2
CONF = "/etc/openhamshack/openhamshack.conf"
RX_MAX = 4
VIEW = "openhamshack-rxview.json"
SLOT = "openhamshack-slot.json"       # where the console wants the picture (the daemon writes it)
STATE = "openhamshack-screens.json"   # what this arranger did, for the console's status
ONE_SCREEN = ("split", "picture", "console")
# Split's picture column as a share of the screen width, and the header
# above it, as openhamshack.css has them (body.split): the column is wider on a
# laptop, where 36% of 1366 would be a postage stamp.
SPLIT_SHARE = (0.36, 0.42)       # >= 1600 px wide, narrower
SPLIT_NARROW = 1600
HEADER = 96
BAR = 100                        # Picture mode's bar at the bottom


def app_id(rid: int) -> str:
    return "mpv" if rid == 1 else f"mpv-rx{rid}"


def crit(rid: int) -> str:
    return f'[app_id="^{app_id(rid)}$"]'


def one_screen_mode(layout: str, one: str | None, rx_enabled: bool = True) -> str:
    """The one-screen mode in force. "video" is the old name for picture
    (a box set to it before this version keeps it); no receiver means no
    picture to give room to."""
    if not rx_enabled:
        return "console"
    if layout == "video":
        return "picture"
    return one if one in ONE_SCREEN else "split"


def plan(outputs: list[dict], rx: str, ui: str, layout: str = "auto", rx_present: bool = True,
         tiles: list[int] | None = None, enlarged: int | None = None, one: str = "split",
         slot: dict | None = None, preview: bool = False) -> list[str]:
    """sway commands for the given active outputs. Pure, so it can be tested.

    tiles: the receivers whose windows exist (None: receiver 1 alone, as
    before there were several). enlarged: the one to show alone, if any.

    layout "two": a TV and a touchscreen. If the TV goes away (standby drops
    its HDMI connection) the pictures are parked on a hidden workspace rather
    than piled onto the console, and come back when the TV does.
    layout "auto": a TV if there is one; one screen goes by `one` (split,
    picture or console), with the picture in `slot` - the console's report
    in its own CSS pixels plus its viewport ("vw", "vh") - when there is
    one. preview: the transmit preview's window exists."""
    tiles = [1] if tiles is None else list(tiles)
    if not rx_present:
        tiles = []
    pair = tv_and_console(outputs, rx, ui)
    if not outputs:
        return []
    if not pair:
        if layout == "two":
            return ([park(t) for t in tiles or [1]]
                    + [f"{PREVIEW} fullscreen disable, sticky disable, floating disable, move container to workspace 9",
                       f"{UI} move container to workspace 2, fullscreen enable", "workspace 2"])
        out = next((o for o in outputs if o["name"] == ui), outputs[0])
        return one_screen(out, one_screen_mode(layout, one), tiles, enlarged, slot, preview)
    if enlarged in tiles and len(tiles) > 1:
        # The enlarged one where the single picture goes; the rest parked,
        # still decoding, so going back to the grid is immediate.
        return [park(t) for t in tiles if t != enlarged] + tv_single(outputs, pair, crit(enlarged))
    if len(tiles) <= 1:
        return tv_single(outputs, pair, crit(tiles[0] if tiles else 1))
    return tv_grid(outputs, pair, tiles)


def park(rid: int) -> str:
    # sticky off first: a sticky window follows every workspace change.
    return f"{crit(rid)} fullscreen disable, sticky disable, floating disable, move container to workspace 9"


def tv_and_console(outputs: list[dict], rx: str, ui: str) -> tuple[str, str] | None:
    """(TV, console) output names when both are present, else None. The
    console screen need not be the one sway.config names - a laptop's own
    panel is eDP-1, not HDMI-A-2 - but with exactly one screen besides the
    TV, that is the console. Without this a laptop with a TV put everything
    on its own panel and left the TV dark."""
    names = [o["name"] for o in outputs]
    if rx in names and ui not in names and len(names) == 2:
        ui = next(n for n in names if n != rx)
    return (rx, ui) if rx in names and ui in names else None


def cells(area: dict, n: int) -> list[tuple[int, int, int, int]]:
    """(x, y, w, h) of n 16:9 tiles in area: two side by side, three or four
    in a 2x2 grid (a third centred under the first two), centred."""
    ax, ay, aw, ah = area["x"], area["y"], area["width"], area["height"]
    rows = 1 if n <= 2 else 2
    cw = aw // 2
    ch = min(ah // rows, cw * 9 // 16)
    cw = min(cw, ch * 16 // 9)
    x0 = ax + (aw - 2 * cw) // 2
    y0 = ay + (ah - rows * ch) // 2
    out = []
    for k in range(n):
        row, col = divmod(k, 2)
        x = x0 + col * cw if not (n == 3 and k == 2) else ax + (aw - cw) // 2
        out.append((x, y0 + row * ch, cw, ch))
    return out


def place(sel: str, cell: tuple, workspace: str, sticky: bool) -> str:
    x, y, w, h = cell
    return (f"{sel} fullscreen disable, move container to {workspace}, floating enable, "
            f"sticky {'enable' if sticky else 'disable'}, border none, "
            f"resize set {w} px {h} px, move absolute position {x} px {y} px")


def tv_grid(outputs: list[dict], pair: tuple[str, str], tiles: list[int]) -> list[str]:
    tv_name, ui = pair
    tv = next(o for o in outputs if o["name"] == tv_name)
    return ([place(crit(t), c, "workspace 1", False) for t, c in zip(tiles, cells(tv["rect"], len(tiles)))]
            + [f"{UI} move container to workspace 2, fullscreen enable",
               "workspace 1", f"move workspace to output {tv_name}",
               "workspace 2", f"move workspace to output {ui}",
               preview_cmd(tv, "workspace 1", corner="top-left", share=4)])


def tv_single(outputs: list[dict], pair: tuple[str, str], sel: str = MPV) -> list[str]:
    """One receive picture on the TV, as there always was: sel is whichever receiver's."""
    rx, ui = pair
    tv = next(o for o in outputs if o["name"] == rx)
    # The receive picture is the workspace's only tiled window, which
    # fills the TV just as full screen would, but leaves room for the
    # floating transmit preview above it (sway hides floating windows
    # behind a full-screen one).
    return [
        f"{sel} floating disable, sticky disable, fullscreen disable, move container to workspace 1",
        f"{UI} move container to workspace 2, fullscreen enable",
        # Pin each workspace to its screen: sway.config's assignment names
        # outputs that may not exist (eDP-1 on a laptop), and an unpinned
        # workspace lands on whichever screen sway picks.
        "workspace 1", f"move workspace to output {rx}",
        "workspace 2", f"move workspace to output {ui}",
        preview_cmd(tv, "workspace 1", corner="top-left", share=4),
    ]


def slot_area(out: dict, mode: str, slot: dict | None) -> dict:
    """The picture's rectangle in sway's coordinates: the console's report,
    scaled from its CSS pixels by its viewport (a zoomed or scaled page has
    fewer CSS pixels than the screen), else the console's layout worked out
    from the screen's size."""
    r = out["rect"]
    w, h = r["width"], r["height"]
    try:
        k = w / float(slot["vw"])
        area = {"x": r["x"] + round(float(slot["x"]) * k), "y": r["y"] + round(float(slot["y"]) * k),
                "width": round(float(slot["w"]) * k), "height": round(float(slot["h"]) * k)}
        if area["width"] >= 64 and area["height"] >= 36:
            return area
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        pass
    if mode == "picture":
        return {"x": r["x"], "y": r["y"], "width": w, "height": h - BAR}
    pw = round(w * SPLIT_SHARE[0 if w >= SPLIT_NARROW else 1])
    return {"x": r["x"], "y": r["y"] + HEADER, "width": pw, "height": pw * 9 // 16}


def one_screen(out: dict, mode: str, tiles: list[int], enlarged: int | None, slot: dict | None,
               preview: bool) -> list[str]:
    """One screen: the console tiled (it fills the workspace) and the
    pictures floating over the slot it keeps clear for them."""
    cmds = [f"{UI} fullscreen disable, move container to workspace 2", "workspace 2"]
    if mode == "console":
        # No picture: parked, still playing, so its sound carries on and
        # Split brings it back at once. A preview floats bottom-left above
        # the footer as it always did.
        h = out["rect"]["height"]
        return (cmds + [park(t) for t in tiles]
                + [preview_cmd(out, "workspace 2", corner="bottom-left", share=4, bottom_gap=int(h * FOOTER))])
    area = slot_area(out, mode, slot)
    if preview:
        # "Preview here": what goes out takes the picture's place.
        return (cmds + [park(t) for t in tiles]
                + [place(PREVIEW, (area["x"], area["y"], area["width"], area["height"]), "workspace 2", True)])
    if enlarged in tiles and len(tiles) > 1:
        shown, waiting = [enlarged], [t for t in tiles if t != enlarged]
    else:
        shown, waiting = tiles, []
    boxes = ([(area["x"], area["y"], area["width"], area["height"])] if len(shown) == 1
             else cells(area, len(shown)))
    return (cmds + [park(t) for t in waiting]
            + [place(crit(t), c, "workspace 2", True) for t, c in zip(shown, boxes)])


def preview_cmd(out: dict, workspace: str, corner: str, share: int, bottom_gap: int = 0) -> str:
    """The transmit preview: a small floating picture in a corner."""
    w, h = out["rect"]["width"], out["rect"]["height"]
    pw = w // share
    ph = pw * 9 // 16
    x = out["rect"]["x"] + MARGIN
    y = out["rect"]["y"] + (MARGIN if corner.startswith("top") else h - ph - MARGIN - bottom_gap)
    return (f"{PREVIEW} fullscreen disable, move container to {workspace}, floating enable, sticky enable, "
            f"border none, resize set {pw} px {ph} px, move absolute position {x} px {y} px")


# --- Display settings (Settings > Display) ---------------------------------
# Resolution, refresh, scale and rotation per screen, and which screen is the
# TV. They live where sway itself reads them - /etc/openhamshack/outputs.conf,
# included by sway.config - and not in IPC commands alone: a sway reload
# rebuilds the whole output configuration from the config files
# (load_main_config in sway 1.10), and the daemon reloads sway for every
# layout change and every enlarged receiver, so a mode set only over IPC
# would last until the next tap on a picture. From a file the settings also
# reach a screen plugged in later, applied by sway before it tells this
# script the screen is there; and a reload that re-applies an unchanged mode
# costs nothing - wlroots drops unchanged fields from the commit
# (output_compare_state), so there is no modeset and the TV does not blink.
#
# A change being tried (the console's Keep / Undo) is a second include,
# TRIAL_CONF in XDG_RUNTIME_DIR, with the roles in TRIAL beside it. Both are
# on tmpfs: a box restarted in the middle of a trial comes back on the
# settings that were last kept, whatever the trial did to the screen.
SCALES = (1.0, 1.25, 1.5, 2.0)
TRANSFORMS = ("normal", "90", "180", "270")
TRIAL = "openhamshack-display-trial.json"
TRIAL_CONF = "openhamshack-outputs-trial.conf"
# What may go inside the quotes of a sway config line. An identifier with
# anything else (a quote; a "$", which sway would read as a variable) is not
# worth escaping: the connector name identifies the screen instead.
SAFE_ID = re.compile(r"^[A-Za-z0-9 ._()+,/:-]+$")


def runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def sway_roles(path: str, rx: str = "HDMI-A-1", ui: str = "HDMI-A-2") -> tuple[str, str]:
    """$rx and $ui from sway.config: the TV and console when nothing else is chosen."""
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return rx, ui
    found = dict(re.findall(r"^set \$(rx|ui) (\S+)", text, re.M))
    return found.get("rx", rx), found.get("ui", ui)


def roles(rx: str, ui: str, conf: str | None = None, trial: str | None = None) -> tuple[str, str]:
    """(TV, console): a swap being tried, else the one kept in openhamshack.conf
    ("displayRoles"), else sway.config's $rx/$ui as given. A trial with no
    roles in it is a reset, so it means sway.config's, not the kept ones."""
    try:
        with open(trial or os.path.join(runtime_dir(), TRIAL)) as f:
            chosen = json.load(f).get("roles") or {}
    except (OSError, ValueError, AttributeError):
        try:
            with open(conf or CONF) as f:
                chosen = json.load(f).get("displayRoles") or {}
        except (OSError, ValueError, AttributeError):
            chosen = {}
    if not isinstance(chosen, dict):
        return rx, ui
    tv, console = str(chosen.get("tv") or ""), str(chosen.get("console") or "")
    return (tv, console) if tv and console and tv != console else (rx, ui)


def console_output(outputs: list[dict], rx: str, ui: str) -> str | None:
    """The screen touch belongs to: the console of a TV-and-console pair, or
    the only screen. Mapping touch to a screen is also what makes rotation
    work: wlroots turns touch coordinates through the transform of the
    output a device is mapped to (wlr_cursor.c, apply_output_transform), and
    leaves them alone for a device mapped to none. sway.config's own
    map_to_output names $ui, which on a one-screen box or a laptop is absent."""
    pair = tv_and_console(outputs, rx, ui)
    if pair:
        return pair[1]
    return outputs[0]["name"] if len(outputs) == 1 else None


def touch_cmds(name: str) -> list[str]:
    return [f"input type:touch map_to_output {name}", f"input type:tablet_tool map_to_output {name}"]


def output_keys(outputs: list[dict]) -> dict[str, str]:
    """name -> the key a screen's settings are stored under: "make model
    serial", as sway identifies a screen, so a monitor keeps its settings
    whichever port it is plugged into. The connector name when the screen
    reports no serial, or when two connected screens would share a key
    (identical panels with blank EDID serials): sway would apply one's
    settings to both."""
    def ident(o: dict) -> str | None:
        parts = [str(o.get(k) or "Unknown") for k in ("make", "model", "serial")]
        if parts[0] == "Unknown" or parts[2] in ("Unknown", "0", "0x00000000"):
            return None
        key = " ".join(parts)
        return key if SAFE_ID.match(key) else None
    ids = {o["name"]: ident(o) for o in outputs}
    counts: dict = {}
    for v in ids.values():
        counts[v] = counts.get(v, 0) + 1
    return {n: (v if v and counts[v] == 1 else n) for n, v in ids.items()}


def mode_arg(width: int, height: int, mhz: int) -> str:
    """sway's mode syntax. get_outputs gives refresh in mHz; sway takes Hz
    and picks the listed mode nearest it, so three decimals choose 59.940
    rather than 60.000."""
    return f"{int(width)}x{int(height)}@{int(mhz) / 1000:.3f}Hz"


def native_mode(o: dict) -> list[int] | None:
    """The screen's preferred mode. sway 1.10's get_outputs does not mark it,
    but lists modes in the kernel's order, and the kernel sorts the EDID's
    preferred mode first (drm_mode_sort). A later sway that marks it is
    believed instead."""
    modes = o.get("modes") or []
    pref = next((m for m in modes if m.get("preferred")), modes[0] if modes else None)
    return [int(pref["width"]), int(pref["height"]), int(pref["refresh"])] if pref else None


def mode_groups(o: dict) -> list[dict]:
    """Modes as the console offers them: one entry per resolution, native
    first, then largest first, each with its refresh rates (mHz, fastest
    first). A TV lists 1080p at 60, 59.94, 50, 30, 25, 24 and 23.976, often
    each twice (one per picture aspect ratio), which as one flat row of
    chips is unreadable."""
    rates: dict = {}
    for m in o.get("modes") or []:
        rates.setdefault((int(m["width"]), int(m["height"])), set()).add(int(m["refresh"]))
    native = native_mode(o)

    def order(wh: tuple) -> tuple:
        return (0 if native and list(wh) == native[:2] else 1, -wh[0] * wh[1], -wh[0])
    return [{"width": w, "height": h, "rates": sorted(rates[(w, h)], reverse=True),
             "native": bool(native and [w, h] == native[:2])} for w, h in sorted(rates, key=order)]


def current_setting(o: dict) -> dict:
    """What a screen is doing now, in the form settings are stored in."""
    if not o.get("active"):
        return {"enabled": False}
    cm = o.get("current_mode") or {}
    mode = [int(cm["width"]), int(cm["height"]), int(cm["refresh"])] if cm.get("width") else native_mode(o)
    transform = str(o.get("transform") or "normal")
    return {"enabled": True, "mode": mode, "scale": float(o.get("scale") or 1.0),
            "transform": transform if transform in TRANSFORMS else "normal"}


def clean_setting(want: dict, o: dict) -> dict:
    """A setting asked for by the console, checked against what the screen
    offers; anything left out stays as it is now. ValueError if refused."""
    if not isinstance(want, dict):
        raise ValueError("a display setting must be an object")
    if not want.get("enabled", True):
        return {"enabled": False}
    now = current_setting(o)
    mode = want.get("mode") or now.get("mode") or native_mode(o)
    try:
        mode = [int(v) for v in mode]
    except (TypeError, ValueError):
        raise ValueError("mode must be [width, height, refresh in mHz]") from None
    offered = {(int(m["width"]), int(m["height"]), int(m["refresh"])) for m in o.get("modes") or []}
    if len(mode) != 3 or tuple(mode) not in offered:
        raise ValueError(f"{o['name']} does not offer {mode_arg(*mode) if len(mode) == 3 else mode}")
    try:
        scale = float(want.get("scale", now.get("scale", 1.0)))
    except (TypeError, ValueError):
        scale = -1.0
    if not any(abs(scale - s) < 1e-6 for s in SCALES):
        raise ValueError(f"scale must be one of {', '.join(f'{s:g}' for s in SCALES)}")
    transform = str(want.get("transform", now.get("transform", "normal")))
    if transform not in TRANSFORMS:
        raise ValueError(f"rotation must be one of {', '.join(TRANSFORMS)}")
    return {"enabled": True, "mode": mode, "scale": scale, "transform": transform}


def output_args(setting: dict) -> list[str]:
    """The words after `output <name>`, for IPC and the config file alike.
    Every field explicit: a trial line must override all of a kept one,
    since sway merges two configs for one screen field by field."""
    if not setting.get("enabled", True):
        return ["disable"]
    args = ["enable"]
    if setting.get("mode"):
        args += ["mode", mode_arg(*setting["mode"])]
    return args + ["scale", f"{float(setting.get('scale', 1.0)):g}",
                   "transform", str(setting.get("transform") or "normal")]


def conf_text(displays: dict, why: str) -> str:
    """An outputs file for sway's include: one line per screen in displays,
    keyed by connector name or sway identifier, both quoted."""
    lines = [f"# {why}", "# Written by the OpenHamShack daemon from Settings > Display; edits here are overwritten."]
    for key in sorted(displays):
        if SAFE_ID.match(key):
            lines.append(f'output "{key}" ' + " ".join(output_args(displays[key])))
    return "\n".join(lines) + "\n"


def describe(outputs: list[dict], rx: str, ui: str, stored: dict | None = None) -> list[dict]:
    """Every screen sway knows, switched on or not, as Settings > Display shows it."""
    stored = stored or {}
    active = [o for o in outputs if o.get("active")]
    pair = tv_and_console(active, rx, ui)
    keys = output_keys(outputs)
    out = []
    for o in outputs:
        name = o["name"]
        if not o.get("active"):
            role = "off"
        elif pair:
            role = "tv" if name == pair[0] else "console" if name == pair[1] else "spare"
        else:
            role = "single" if len(active) == 1 else "spare"
        label = " ".join(str(v) for v in (o.get("make"), o.get("model")) if v and v != "Unknown") or name
        out.append({"name": name, "key": keys[name], "label": label, "role": role,
                    "setting": current_setting(o), "native": native_mode(o), "modes": mode_groups(o),
                    "stored": keys[name] in stored,
                    "logical": {k: (o.get("rect") or {}).get(k) for k in ("width", "height")}})
    return out


def swaymsg(*args: str) -> str:
    return subprocess.run(["swaymsg", *args], capture_output=True, text=True).stdout


def settings() -> dict:
    try:
        with open(CONF) as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def layout() -> str:
    return str(settings().get("screenLayout") or "auto")


def has_window(tree: dict, app_id: str) -> bool:
    if tree.get("app_id") == app_id:
        return True
    return any(has_window(c, app_id) for c in tree.get("nodes", []) + tree.get("floating_nodes", []))


def runtime_json(name: str) -> dict | None:
    try:
        with open(os.path.join(runtime_dir(), name)) as f:
            got = json.load(f)
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


def enlarged() -> int | None:
    try:
        value = (runtime_json(VIEW) or {}).get("enlarged")
        return int(value) if value else None
    except (ValueError, TypeError):
        return None


def write_state(state: dict) -> None:
    """What the screens are doing, for the console: whether this is a
    one-screen box and in which mode (status.screens), so the header shows
    its Split / Picture / Console switch only where there is a choice."""
    path = os.path.join(runtime_dir(), STATE)
    try:
        with open(path + ".tmp", "w") as f:
            json.dump(state, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


touch_mapped: list[str] = []    # the console touch was last mapped to, by this process


def arrange(rx: str, ui: str, msg=swaymsg) -> None:
    rx, ui = roles(rx, ui)
    outputs = [o for o in json.loads(msg("-t", "get_outputs") or "[]") if o.get("active")]
    tree = json.loads(msg("-t", "get_tree") or "{}")
    tiles = [rid for rid in range(1, RX_MAX + 1) if has_window(tree, app_id(rid))]
    cfg = settings()
    lay = str(cfg.get("screenLayout") or "auto")
    one = one_screen_mode(lay, cfg.get("oneScreen"), cfg.get("rxEnabled", True) is not False)
    # A slot reported for another mode (Picture's whole screen, just after
    # switching to Split) is not this one's: the worked-out one stands in
    # until the console reports again.
    slot = runtime_json(SLOT)
    if slot and slot.get("mode") != one:
        slot = None
    for cmd in plan(outputs, rx, ui, lay, bool(tiles), tiles or [1], enlarged(), one, slot,
                    has_window(tree, "mpv-preview")):
        msg(cmd)
    single = bool(outputs) and not tv_and_console(outputs, rx, ui)
    write_state({"single": single, "mode": one if single and lay != "two" else None})
    # Only when it changes: re-applying an input config on every new window
    # could drop a touch that is under way.
    target = console_output(outputs, rx, ui)
    if target and touch_mapped != [target]:
        for cmd in touch_cmds(target):
            msg(cmd)
        touch_mapped[:] = [target]


# The daemon's nudge to look again (a new slot, a new mode): a sway tick
# with this payload, which costs nothing, rather than a reload, which
# rebuilds the outputs and restarts this arranger.
TICK = "openhamshack-arrange"


def wants_arranging(ev: dict) -> bool:
    """Outputs: a monitor came or went. Windows: only new and closed ones
    (mpv is restarted by the daemon's watchdog; the preview comes and goes),
    never a window's own fullscreen toggle. Ticks: the daemon's."""
    if ev.get("payload") == TICK:
        return True
    return "change" in ev and ev.get("container") is None or ev.get("change") in ("new", "close")


def main() -> None:
    rx, ui = sys.argv[1], sys.argv[2]
    # One arranger only: sway's exec_always starts a new one on every reload
    # (which is how a layout change is applied), so the old one is retired.
    import signal
    pidfile = os.path.join(runtime_dir(), "openhamshack-screens.pid")
    try:
        os.kill(int(open(pidfile).read()), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    arrange(rx, ui)
    sub = subprocess.Popen(["swaymsg", "-m", "-t", "subscribe", '["output","window","tick"]'],
                           stdout=subprocess.PIPE, text=True)
    for line in sub.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and wants_arranging(ev):
            arrange(rx, ui)


if __name__ == "__main__":
    main()
