"""Receiving through the Pluto's own longmynd (PlutoDVB2): the MQTT words.

PlutoDVB2 0.5.x carries F5OEO's build of longmynd and starts it on the
Pluto when told to, with the MiniTiouner (or PicoTuner) on a USB hub on the
Pluto's OTG port. The tuner is then driven over the Pluto's own MQTT broker
and the transport stream comes over the network; nothing runs on the box
but the relay and the player it always had.

What is here is the translation, with no state: the tuning commands for a
receiver's settings, and each status message turned into the line
longmynd itself would have sent on its status socket, so that
parse_longmynd, rx_lock_status, the MER/margin, the advice, the signal log
and the gate serve this receiver unchanged. openhamshack.py's "Pluto's own
longmynd" section runs the processes and says why the sequence is what it
is.

Sources, read on 4 October 2026 (no Pluto was asked anything):
- F5OEO/longmynd at 2a29f0b, mymqtt.c: the commands longmynd takes
  (cmd/longmynd/frequency in kHz at the tuner, sr in kS, swport 0/1, polar
  h/v/n, tsip an address with the port always 1234), and every status topic
  dt/longmynd/<name>, named by its position in longmynd's own status list,
  with the state as a word, MER as dB with one decimal, the symbol rate in
  kS, and the MODCOD as two topics, modulation and fec. main.c: frequency
  is only taken between 144000 and 2450000 kHz and the symbol rate between
  33 and 27500 kS; anything else is dropped without a word.
- F5OEO/pluto-ori-ps, mqtt_longmynd.sh and pluto_mqtt_ctrl.cpp: see
  PLUTO_NO_CALL below.
"""

from __future__ import annotations

from .dvb import MODCODS


# longmynd's status list (mymqtt.c StatusString): the topic's name is the
# field's number on the status socket, which is what parse_longmynd reads.
PLUTO_LM_FIELDS = {name: str(i) for i, name in enumerate(
    ["", "rx_state", "lna_gain", "puncrate", "poweri", "powerq", "carrier_frequency", "constel_i", "constel_q",
     "symbolrate", "viterbi_error", "ber", "mer", "service_name", "provider_name", "ts_null", "es_pid", "es_type",
     "modcod", "short_frame", "pilots", "ldpc_errors", "bch_errors", "bch_uncorect", "lnb_supply", "polarisation",
     "agc1", "agc2", "matype1", "matype2"]) if name}
# mymqtt.c StateString, in longmynd's order: 3 and 4 are the two locks.
PLUTO_LM_STATES = {"Init": "0", "Hunting": "1", "found header": "2", "demod_s": "3", "demod_s2": "4"}
MODCOD_NUMBER = {(const, fec): n for n, (const, fec) in MODCODS.items()}
# What longmynd on the Pluto will take (main.c config_set_*). Outside these
# it ignores the command and stays where it was, so the box refuses first.
PLUTO_LM_KHZ = (144000, 2450000)
PLUTO_LM_KS = (33, 27500)
# The port the firmware's longmynd always sends the TS to (config_set_tsip).
PLUTO_TS_PORT = 1234
# The callsign the Pluto's MQTT control program uses when none is set.
# pluto_mqtt_ctrl.cpp reads `fw_printenv -n call` and, empty, publishes
# under dt/pluto/nocall/; mqtt_longmynd.sh reads the same variable and,
# empty, waits on cmd/pluto//system/longmynd - two slashes. So a Pluto that
# shows "nocall" is either one with no callsign (the script listens on the
# empty root) or one whose callsign really is NOCALL (it listens on that):
# from outside the two cannot be told apart, and a start sent to the wrong
# one is silently lost. OpenHamShack does not guess: it asks for the Pluto's own
# callsign to be set first, which makes both halves of the firmware agree.
PLUTO_NO_CALL = "nocall"


def pluto_lm_commands(freq_mhz: float, offset_mhz: float, sr_ks: float, polar: str) -> list[tuple[str, str]]:
    """The cmd/longmynd/<name> messages for one tuning, polar first: the
    firmware starts longmynd with 18 V on the LNB (-p h), and the supply the
    operator chose is the first thing that should reach it. swport 0 is the
    top F input, which is the one a USB tuner's longmynd uses here too."""
    khz = int(round((float(freq_mhz) - float(offset_mhz)) * 1000))
    return [("polar", polar), ("frequency", str(khz)), ("sr", str(int(round(float(sr_ks))))), ("swport", "0")]


def pluto_lm_range_problem(freq_mhz: float, offset_mhz: float, sr_ks: float) -> str:
    """Why longmynd on the Pluto would silently ignore this tuning, or ""."""
    khz = int(round((float(freq_mhz) - float(offset_mhz)) * 1000))
    if not PLUTO_LM_KHZ[0] <= khz <= PLUTO_LM_KHZ[1]:
        return (f"The Pluto's longmynd tunes {PLUTO_LM_KHZ[0] // 1000}-{PLUTO_LM_KHZ[1] // 1000} MHz at the tuner; "
                f"{khz / 1000:g} MHz is outside it (check the LNB offset)")
    sr = int(round(float(sr_ks)))
    if not PLUTO_LM_KS[0] <= sr <= PLUTO_LM_KS[1]:
        return f"The Pluto's longmynd takes {PLUTO_LM_KS[0]}-{PLUTO_LM_KS[1]} kS; {sr} kS is outside it"
    return ""


def pluto_lm_status(name: str, value: str, memo: dict) -> str:
    """One dt/longmynd/<name> message as longmynd's own status text
    ("$<n>,<value>"), or "" for one that has no field there. memo carries
    the modulation and FEC between their two messages: the MODCOD number
    longmynd reports on its socket arrives over MQTT as words, in two parts."""
    value = value.strip()
    if name == "rx_state":
        state = PLUTO_LM_STATES.get(value)
        if state is None:
            return ""
        memo["state"] = state
        return f"$1,{state}"
    if name == "mer":
        try:
            return f"$12,{int(round(float(value) * 10))}"   # dB with one decimal; the socket has tenths
        except ValueError:
            return ""
    if name in ("modulation", "fec"):
        memo[name] = value
        n = MODCOD_NUMBER.get((memo.get("modulation", ""), memo.get("fec", "")))
        return f"$18,{n}" if n else ""
    field = PLUTO_LM_FIELDS.get(name)
    if field is None or field == "18":
        return ""
    return f"${field},{value}"


def pluto_lm_line(line: str) -> tuple[str, str] | None:
    """mosquitto_sub -v's 'dt/longmynd/<name> <value>' as (name, value);
    the value may hold spaces (a service name) or be empty."""
    topic, _, value = line.rstrip("\r\n").partition(" ")
    if not topic.startswith("dt/longmynd/"):
        return None
    return topic[len("dt/longmynd/"):], value
