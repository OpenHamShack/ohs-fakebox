"""The daemon's pure parts: code that holds no state and calls nothing in
openhamshack.py, one module per subject.

openhamshack.py imports every name from here, so callers still write
`openhamshack.net_bitrate`. What may move here, and what must not: a name that
the tests or the daemon replace on the openhamshack module (`lb.load = ...`)
stays there, and so does anything that reaches one, because a replacement
made on openhamshack is not seen from here. HANDOVER, "Where code lives", has the
rule and the list.

Modules here import only the standard library and each other, never
openhamshack.
"""
