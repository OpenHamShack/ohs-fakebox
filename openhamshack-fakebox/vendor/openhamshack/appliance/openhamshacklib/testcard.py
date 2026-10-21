"""Test cards that need no state: the built-in cards' ids and names, what a
stored choice means, and the Morse callsign as samples.

openhamshack.py's Test cards section has the reasoning (cards drawn by ffmpeg's
own filters, never shipped as pictures; pictures copied, not played from a
stick) and keeps the pictures folder (TESTCARD_DIR, which tests move), the
filter graphs that draw the callsign through text files, and the API.
"""

from __future__ import annotations

import math


PIC = "pic:"
# id: (name on the console, the key pad's second line - seven characters)
TESTCARDS = {"pattern": ("Moving pattern", "MOVING"), "bars": ("Colour bars", "BARS"),
             "greyscale": ("Grey scale", "GREY"), "circle": ("Circle and grid", "CIRCLE"),
             "callsign": ("Callsign card", "CALL")}
TESTCARD_AUDIO = ("tone", "silence", "morse")
# The Morse ID: 20 words a minute (a 60 ms dot), 800 Hz, the call once every
# MORSE_PERIOD seconds. 800 Hz rather than the line-up tone's 1 kHz so the
# two are told apart by ear.
MORSE_WPM = 20
MORSE_HZ = 800
MORSE_PERIOD = 30.0
MORSE = {"A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.", "H": "....",
         "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---", "P": ".--.",
         "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
         "Y": "-.--", "Z": "--..", "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
         "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.", "/": "-..-."}


def testcard_choice(cfg: dict) -> str:
    card = str(cfg.get("testCard") or "pattern")
    return card if card in TESTCARDS or card.startswith(PIC) else "pattern"


def testcard_name(card: str) -> str:
    return TESTCARDS[card][0] if card in TESTCARDS else card[len(PIC):]


def morse_samples(text: str, rate: int = 48000) -> bytes:
    """16-bit mono PCM: the text in Morse at MORSE_WPM, MORSE_HZ, peak at
    -18 dBFS like the tone, each element shaped with 5 ms raised-cosine
    edges (hard-keyed it clicks), preceded by a second of silence and padded
    with silence to MORSE_PERIOD seconds."""
    import array
    dot = round(rate * 1.2 / MORSE_WPM)
    amp = 32767 * 10 ** (-18 / 20)
    ramp = round(rate * 0.005)

    def tone(n: int) -> array.array:
        out = array.array("h", bytes(2 * n))
        for i in range(n):
            edge = min(i, n - 1 - i)
            shape = 1.0 if edge >= ramp else 0.5 - 0.5 * math.cos(math.pi * edge / ramp)
            out[i] = int(amp * shape * math.sin(2 * math.pi * MORSE_HZ * i / rate))
        return out
    dit, dah = tone(dot), tone(3 * dot)
    pcm = array.array("h", bytes(2 * rate))
    for w, word in enumerate(text.upper().split()):
        if w:
            pcm.extend(bytes(2 * 4 * dot))       # 7 dots between words (3 already after the letter)
        for ch in word:
            code = MORSE.get(ch)
            if not code:
                continue
            for sym in code:
                pcm.extend(dit if sym == "." else dah)
                pcm.extend(array.array("h", bytes(2 * dot)))
            pcm.extend(array.array("h", bytes(2 * 2 * dot)))   # 3 dots between letters
    total = int(rate * MORSE_PERIOD)
    if len(pcm) < total:
        pcm.extend(array.array("h", bytes(2 * (total - len(pcm)))))
    return pcm.tobytes()
