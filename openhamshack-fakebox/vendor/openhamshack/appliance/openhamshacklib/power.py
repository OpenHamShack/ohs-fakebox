"""The battery, for a OpenHamShack run on a laptop: what the kernel's
power_supply class says, read under a given root so the tests can hand
it a fake tree. The N150 boxes have no battery, and then this is None.

Only the files are read: no upower, no D-Bus, no subprocess. /api/status
is polled every second or so by every console, and a dozen small sysfs
reads cost nothing, where asking upower would add a daemon and a bus to
an appliance that has neither for anything else.
"""

from __future__ import annotations

from pathlib import Path

from openhamshacklib.sysfs import diag_attr

# On battery only: at or below these the console warns, the second
# urgently. Higher than a desktop's (upower: 10% low, 3% critical): a
# laptop that switches itself off takes the transmitter with it mid-over,
# and its last few percent go quickly with a USB radio drawing on it.
# Time left counts too, where the battery gives it, since a worn battery's
# 20% may be ten minutes.
BATTERY_LOW_PCT, BATTERY_CRITICAL_PCT = 20, 10
BATTERY_LOW_MIN, BATTERY_CRITICAL_MIN = 20, 10
# A time from a nearly idle battery (a few mW) is days, and a noisy one;
# anything past this is not said rather than said wrong.
BATTERY_MAX_MIN = 24 * 60
# The power_supply types that mean "plugged in": mains, and the USB-C
# chargers ucsi and PD report as USB_*.
AC_TYPES = ("Mains", "USB", "USB_C", "USB_PD", "USB_PD_DRP", "USB_DCP", "USB_CDP", "USB_ACA")
BATTERY_STATES = {"Charging": "charging", "Discharging": "discharging", "Full": "full",
                  "Not charging": "notCharging"}


def battery_int(dev: Path, name: str) -> int | None:
    try:
        return int(diag_attr(dev, name))
    except ValueError:
        return None


def power_supplies(root: Path) -> list[Path]:
    try:
        return sorted(p for p in (root / "class" / "power_supply").iterdir())
    except OSError:
        return []


def system_supply(dev: Path) -> bool:
    # scope "Device" is a peripheral's own battery (a wireless mouse, a
    # gamepad, a phone over USB): not what keeps this computer running.
    # Most laptops' batteries have no scope file at all.
    return diag_attr(dev, "scope") != "Device"


def battery_read(root: Path) -> dict | None:
    """The computer's battery under root (normally /sys), several combined
    as one, or None without one. percent 0-100; state charging,
    discharging, full, notCharging or unknown; ac True/False, or None when
    no charger is reported at all; onBattery; minutesLeft to empty while
    discharging, to full while charging, None when the battery does not
    say enough; level ok, low or critical (low and critical only on
    battery: a laptop on its charger at 5% is not about to stop)."""
    bats, ac = [], None
    for dev in power_supplies(root):
        kind = diag_attr(dev, "type")
        if not system_supply(dev):
            continue
        if kind == "Battery":
            if diag_attr(dev, "present") == "0":
                continue                 # an empty second bay
            bats.append(dev)
        elif kind in AC_TYPES:
            online = diag_attr(dev, "online")
            if online in ("0", "1"):
                ac = bool(ac) or online == "1"
    if not bats:
        return None
    each = [battery_one(dev) for dev in bats]
    percent = battery_percent(each)
    if percent is None:
        return None
    states = {b["state"] for b in each}
    # One draining is the laptop on battery, whatever the other says (a
    # ThinkPad empties its two in turn, the idle one "Not charging").
    state = next((s for s in ("discharging", "charging", "notCharging", "full") if s in states), "unknown")
    # No charger reported (some desktops-turned-laptops, some VMs): the
    # battery's own state is the only word on it.
    on_battery = ac is False or (ac is None and state == "discharging")
    left = battery_minutes(each, state)
    level = "ok"
    if on_battery:
        if percent <= BATTERY_CRITICAL_PCT or (left is not None and left <= BATTERY_CRITICAL_MIN):
            level = "critical"
        elif percent <= BATTERY_LOW_PCT or (left is not None and left <= BATTERY_LOW_MIN):
            level = "low"
    return {"percent": percent, "state": state, "ac": ac, "onBattery": on_battery, "minutesLeft": left,
            "level": level, "batteries": len(each)}


def battery_one(dev: Path) -> dict:
    """One battery's raw numbers. energy_* are µWh with power_now in µW;
    charge_* are µAh with current_now in µA. A battery has one family or
    the other, and some firmware reports a negative current while
    discharging, so it is taken as a size."""
    out = {"state": BATTERY_STATES.get(diag_attr(dev, "status"), "unknown"), "capacity": battery_int(dev, "capacity")}
    for family, rate in (("energy", "power_now"), ("charge", "current_now")):
        now, full = battery_int(dev, f"{family}_now"), battery_int(dev, f"{family}_full")
        if now is not None and full:
            r = battery_int(dev, rate)
            out.update(family=family, now=now, full=full, rate=abs(r) if r else None)
            break
    return out


def battery_percent(each: list[dict]) -> int | None:
    # Weighted by what each holds when they all say in the same unit (a
    # ThinkPad's 23 Wh internal and 72 Wh external battery are not 50/50);
    # otherwise the plain mean of what the kernel calls capacity.
    families = {b.get("family") for b in each}
    if len(families) == 1 and None not in families:
        return max(0, min(100, round(100 * sum(b["now"] for b in each) / sum(b["full"] for b in each))))
    pcts = [b["capacity"] if b["capacity"] is not None
            else round(100 * b["now"] / b["full"]) if b.get("family") else None for b in each]
    pcts = [p for p in pcts if p is not None]
    return max(0, min(100, round(sum(pcts) / len(pcts)))) if pcts else None


def battery_minutes(each: list[dict], state: str) -> int | None:
    """To empty or to full, from what is left (or still to go) over the
    rate it is going at. Only when every battery reports both in one
    unit: a time from half the batteries would be a wrong time."""
    if state not in ("discharging", "charging"):
        return None
    families = {b.get("family") for b in each}
    if len(families) != 1 or None in families:
        return None
    rate = sum(b["rate"] or 0 for b in each if b["state"] == state)
    if not rate:
        return None                      # power_now 0 or missing: not known
    amount = sum(b["now"] for b in each) if state == "discharging" else sum(b["full"] - b["now"] for b in each)
    minutes = round(60 * max(amount, 0) / rate)
    return minutes if minutes <= BATTERY_MAX_MIN else None
