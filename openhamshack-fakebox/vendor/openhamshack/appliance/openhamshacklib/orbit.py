"""Where the ISS is and when it passes over a station: SGP4 from a NORAD
two-line element set, look angles from a Maidenhead locator.

The HamTV section of openhamshack.py fetches the elements and keeps the passes;
everything here is arithmetic on its arguments.
"""

from __future__ import annotations

import calendar
import math
import re


def maidenhead(loc: str) -> tuple[float, float] | None:
    """Centre (lat, lon) of a 4- or 6-character Maidenhead locator."""
    loc = str(loc or "").strip()
    if not re.fullmatch(r"[A-Ra-r]{2}[0-9]{2}([A-Xa-x]{2})?", loc):
        return None
    loc = loc[:2].upper() + loc[2:4] + loc[4:].lower()
    lon = (ord(loc[0]) - 65) * 20 - 180 + int(loc[2]) * 2
    lat = (ord(loc[1]) - 65) * 10 - 90 + int(loc[3])
    if len(loc) == 6:
        lon += (ord(loc[4]) - 97) * 2 / 24 + 1 / 24
        lat += (ord(loc[5]) - 97) / 24 + 1 / 48
    else:
        lon, lat = lon + 1, lat + 0.5
    return round(float(lat), 4), round(float(lon), 4)


# Where the ISS is, from a NORAD two-line element set, by SGP4: the model the
# elements are fitted with, so any other propagator would be wrong in ways
# the elements already allow for. This is the near-earth branch only (orbits
# shorter than 225 minutes: the ISS is ~93), transcribed from David Vallado's
# reference implementation ("Revisiting Spacetrack Report #3", AIAA
# 2006-6753, published through CelesTrak for free use), in the arrangement
# python-sgp4 gives it (Copyright (c) 2012-2016 Brandon Rhodes, MIT licence:
# "Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software ... to deal in the Software without restriction ...
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software."). WGS-72 constants and
# the "improved" operation mode, as python-sgp4's Satrec uses by default.
# test_hamtv.py checks it against Vallado's own published test vectors and
# against python-sgp4 for a 2026 ISS element set. Deep space (the 12- and
# 24-hour resonances, lunar-solar terms) is left out on purpose: about 900
# lines that nothing here would run. sgp4_init refuses such an orbit.
SGP4_MU = 398600.8
SGP4_RE = 6378.135                                   # km
SGP4_XKE = 60.0 / math.sqrt(SGP4_RE ** 3 / SGP4_MU)  # sqrt(GM) in earth radii^1.5 / min
SGP4_J2, SGP4_J3, SGP4_J4 = 0.001082616, -0.00000253881, -0.00000165597
SGP4_J3OJ2 = SGP4_J3 / SGP4_J2
TWO_PI = 2.0 * math.pi
UNIX_JD = 2440587.5                                  # Julian date of 1970-01-01 00:00 UTC


def tle_checksum(line: str) -> int:
    return sum(int(c) if c.isdigit() else 1 if c == "-" else 0 for c in line[:68]) % 10


def tle_float(field: str) -> float:
    """A TLE's packed exponent field (" 10128-3" is 0.10128e-3)."""
    s = field.strip().replace(" ", "")
    if not s:
        return 0.0
    sign = -1.0 if s[0] == "-" else 1.0
    s = s.lstrip("+-")
    mant, exp = (s[:-2], s[-2:]) if len(s) > 2 and s[-2] in "+-" else (s, "0")
    return sign * float("0." + mant) * 10.0 ** int(exp)


def parse_tle(line1: str, line2: str) -> dict:
    """The fields SGP4 needs, from the two lines. ValueError for a damaged
    set: a bad checksum is the usual sign of a truncated download."""
    line1, line2 = line1.rstrip(), line2.rstrip()
    if len(line1) < 69 or len(line2) < 69 or line1[0] != "1" or line2[0] != "2":
        raise ValueError("not a two-line element set")
    for ln in (line1, line2):
        if ln[68].isdigit() and tle_checksum(ln) != int(ln[68]):
            raise ValueError("element set checksum is wrong")
    year = int(line1[18:20])
    year += 2000 if year < 57 else 1900
    day = float(line1[20:32])
    # Day 1.0 is 1 January 00:00 UTC.
    epoch = calendar.timegm((year, 1, 1, 0, 0, 0)) + (day - 1.0) * 86400.0
    deg = math.pi / 180.0
    return {
        "satnum": line1[2:7].strip(), "epoch": epoch, "bstar": tle_float(line1[53:61]),
        "incl": float(line2[8:16]) * deg, "node": float(line2[17:25]) * deg,
        "ecc": float("0." + line2[26:33].strip()), "argp": float(line2[34:42]) * deg,
        "mo": float(line2[43:51]) * deg, "no": float(line2[52:63]) * TWO_PI / 1440.0,  # rad/min
    }


def gmst(jd_ut1: float) -> float:
    """Greenwich mean sidereal time (IAU 1982), radians: the angle SGP4's
    TEME frame turns through to become earth-fixed."""
    t = (jd_ut1 - 2451545.0) / 36525.0
    sec = -6.2e-6 * t ** 3 + 0.093104 * t * t + (876600.0 * 3600 + 8640184.812866) * t + 67310.54841
    return (sec * math.pi / 180.0 / 240.0) % TWO_PI


def sgp4_init(tle: dict) -> dict:
    """Vallado's initl and sgp4init for a near-earth orbit: the constants
    sgp4() needs, worked out once per element set."""
    e, i, no_kozai, bstar = tle["ecc"], tle["incl"], tle["no"], tle["bstar"]
    x2o3 = 2.0 / 3.0
    # initl: recover the original mean motion (Brouwer from Kozai).
    eccsq = e * e
    omeosq = 1.0 - eccsq
    rteosq = math.sqrt(omeosq)
    cosio = math.cos(i)
    cosio2 = cosio * cosio
    ak = (SGP4_XKE / no_kozai) ** x2o3
    d1 = 0.75 * SGP4_J2 * (3.0 * cosio2 - 1.0) / (rteosq * omeosq)
    dl = d1 / (ak * ak)
    adel = ak * (1.0 - dl * dl - dl * (1.0 / 3.0 + 134.0 * dl * dl / 81.0))
    dl = d1 / (adel * adel)
    no = no_kozai / (1.0 + dl)
    if TWO_PI / no >= 225.0:
        raise ValueError("deep-space orbit (period of 225 minutes or more): not supported")
    ao = (SGP4_XKE / no) ** x2o3
    sinio = math.sin(i)
    po = ao * omeosq
    con42 = 1.0 - 5.0 * cosio2
    con41 = -con42 - cosio2 - cosio2
    posq = po * po
    rp = ao * (1.0 - e)

    s = dict(tle, no_unkozai=no, con41=con41, x1mth2=1.0 - cosio2, x7thm1=7.0 * cosio2 - 1.0,
             isimp=rp < 220.0 / SGP4_RE + 1.0)
    ss = 78.0 / SGP4_RE + 1.0
    qzms2t = ((120.0 - 78.0) / SGP4_RE) ** 4
    sfour, qzms24 = ss, qzms2t
    perige = (rp - 1.0) * SGP4_RE
    if perige < 156.0:
        sfour = perige - 78.0
        if perige < 98.0:
            sfour = 20.0
        qzms24 = ((120.0 - sfour) / SGP4_RE) ** 4
        sfour = sfour / SGP4_RE + 1.0
    pinvsq = 1.0 / posq
    tsi = 1.0 / (ao - sfour)
    eta = ao * e * tsi
    etasq = eta * eta
    eeta = e * eta
    psisq = abs(1.0 - etasq)
    coef = qzms24 * tsi ** 4
    coef1 = coef / psisq ** 3.5
    cc2 = coef1 * no * (ao * (1.0 + 1.5 * etasq + eeta * (4.0 + etasq))
                        + 0.375 * SGP4_J2 * tsi / psisq * con41 * (8.0 + 3.0 * etasq * (8.0 + etasq)))
    cc1 = bstar * cc2
    cc3 = -2.0 * coef * tsi * SGP4_J3OJ2 * no * sinio / e if e > 1.0e-4 else 0.0
    cc4 = 2.0 * no * coef1 * ao * omeosq * (
        eta * (2.0 + 0.5 * etasq) + e * (0.5 + 2.0 * etasq)
        - SGP4_J2 * tsi / (ao * psisq) * (-3.0 * con41 * (1.0 - 2.0 * eeta + etasq * (1.5 - 0.5 * eeta))
                                          + 0.75 * s["x1mth2"] * (2.0 * etasq - eeta * (1.0 + etasq))
                                          * math.cos(2.0 * tle["argp"])))
    cc5 = 2.0 * coef1 * ao * omeosq * (1.0 + 2.75 * (etasq + eeta) + eeta * etasq)
    cosio4 = cosio2 * cosio2
    temp1 = 1.5 * SGP4_J2 * pinvsq * no
    temp2 = 0.5 * temp1 * SGP4_J2 * pinvsq
    temp3 = -0.46875 * SGP4_J4 * pinvsq * pinvsq * no
    xhdot1 = -temp1 * cosio
    s.update(
        eta=eta, cc1=cc1, cc4=cc4, cc5=cc5,
        mdot=no + 0.5 * temp1 * rteosq * con41 + 0.0625 * temp2 * rteosq * (13.0 - 78.0 * cosio2 + 137.0 * cosio4),
        argpdot=(-0.5 * temp1 * con42 + 0.0625 * temp2 * (7.0 - 114.0 * cosio2 + 395.0 * cosio4)
                 + temp3 * (3.0 - 36.0 * cosio2 + 49.0 * cosio4)),
        nodedot=xhdot1 + (0.5 * temp2 * (4.0 - 19.0 * cosio2) + 2.0 * temp3 * (3.0 - 7.0 * cosio2)) * cosio,
        omgcof=bstar * cc3 * math.cos(tle["argp"]),
        xmcof=-x2o3 * coef * bstar / eeta if e > 1.0e-4 else 0.0,
        nodecf=3.5 * omeosq * xhdot1 * cc1, t2cof=1.5 * cc1,
        xlcof=-0.25 * SGP4_J3OJ2 * sinio * (3.0 + 5.0 * cosio) / (1.0 + cosio if abs(cosio + 1.0) > 1.5e-12 else 1.5e-12),
        aycof=-0.5 * SGP4_J3OJ2 * sinio, delmo=(1.0 + eta * math.cos(tle["mo"])) ** 3,
        sinmao=math.sin(tle["mo"]), d2=0.0, d3=0.0, d4=0.0, t3cof=0.0, t4cof=0.0, t5cof=0.0)
    if not s["isimp"]:
        # Perigee above 220 km: the fuller drag terms.
        cc1sq = cc1 * cc1
        d2 = 4.0 * ao * tsi * cc1sq
        temp = d2 * tsi * cc1 / 3.0
        d3 = (17.0 * ao + sfour) * temp
        d4 = 0.5 * temp * ao * tsi * (221.0 * ao + 31.0 * sfour) * cc1
        s.update(d2=d2, d3=d3, d4=d4, t3cof=d2 + 2.0 * cc1sq,
                 t4cof=0.25 * (3.0 * d3 + cc1 * (12.0 * d2 + 10.0 * cc1sq)),
                 t5cof=0.2 * (3.0 * d4 + 12.0 * cc1 * d3 + 6.0 * d2 * d2 + 15.0 * cc1sq * (2.0 * d2 + cc1sq)))
    return s


def sgp4(s: dict, tsince: float) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Position (km) and velocity (km/s) in the TEME frame, tsince minutes
    after the element set's epoch. ValueError once the model breaks down
    (a decayed or nonsensical orbit) rather than a position that is not."""
    t = tsince
    xmdf = s["mo"] + s["mdot"] * t
    argpdf = s["argp"] + s["argpdot"] * t
    nodedf = s["node"] + s["nodedot"] * t
    argpm, mm = argpdf, xmdf
    t2 = t * t
    nodem = nodedf + s["nodecf"] * t2
    tempa = 1.0 - s["cc1"] * t
    tempe = s["bstar"] * s["cc4"] * t
    templ = s["t2cof"] * t2
    if not s["isimp"]:
        delomg = s["omgcof"] * t
        delm = s["xmcof"] * ((1.0 + s["eta"] * math.cos(xmdf)) ** 3 - s["delmo"])
        temp = delomg + delm
        mm = xmdf + temp
        argpm = argpdf - temp
        t3 = t2 * t
        t4 = t3 * t
        tempa = tempa - s["d2"] * t2 - s["d3"] * t3 - s["d4"] * t4
        tempe = tempe + s["bstar"] * s["cc5"] * (math.sin(mm) - s["sinmao"])
        templ = templ + s["t3cof"] * t3 + t4 * (s["t4cof"] + t * s["t5cof"])
    nm, em, inclm = s["no_unkozai"], s["ecc"], s["incl"]
    if nm <= 0.0:
        raise ValueError("mean motion is not positive")
    am = (SGP4_XKE / nm) ** (2.0 / 3.0) * tempa * tempa
    nm = SGP4_XKE / am ** 1.5
    em = em - tempe
    if em >= 1.0 or em < -0.001:
        raise ValueError("mean eccentricity out of range")
    em = max(em, 1.0e-6)
    mm = mm + s["no_unkozai"] * templ
    xlm = mm + argpm + nodem
    nodem = nodem % TWO_PI if nodem >= 0.0 else -(-nodem % TWO_PI)
    argpm = argpm % TWO_PI
    xlm = xlm % TWO_PI
    mm = (xlm - argpm - nodem) % TWO_PI
    sinip, cosip = math.sin(inclm), math.cos(inclm)

    # Long-period periodics.
    axnl = em * math.cos(argpm)
    temp = 1.0 / (am * (1.0 - em * em))
    aynl = em * math.sin(argpm) + temp * s["aycof"]
    xl = mm + argpm + nodem + temp * s["xlcof"] * axnl
    # Kepler's equation, Newton-Raphson with the step limited as Vallado does.
    u = (xl - nodem) % TWO_PI
    eo1, tem5, ktr = u, 9999.9, 1
    sineo1 = coseo1 = 0.0
    while abs(tem5) >= 1.0e-12 and ktr <= 10:
        sineo1, coseo1 = math.sin(eo1), math.cos(eo1)
        tem5 = (u - aynl * coseo1 + axnl * sineo1 - eo1) / (1.0 - coseo1 * axnl - sineo1 * aynl)
        tem5 = max(-0.95, min(0.95, tem5))
        eo1 += tem5
        ktr += 1
    # Short-period periodics.
    ecose = axnl * coseo1 + aynl * sineo1
    esine = axnl * sineo1 - aynl * coseo1
    el2 = axnl * axnl + aynl * aynl
    pl = am * (1.0 - el2)
    if pl < 0.0:
        raise ValueError("semi-latus rectum is negative")
    rl = am * (1.0 - ecose)
    rdotl = math.sqrt(am) * esine / rl
    rvdotl = math.sqrt(pl) / rl
    betal = math.sqrt(1.0 - el2)
    temp = esine / (1.0 + betal)
    sinu = am / rl * (sineo1 - aynl - axnl * temp)
    cosu = am / rl * (coseo1 - axnl + aynl * temp)
    su = math.atan2(sinu, cosu)
    sin2u = (cosu + cosu) * sinu
    cos2u = 1.0 - 2.0 * sinu * sinu
    temp = 1.0 / pl
    temp1 = 0.5 * SGP4_J2 * temp
    temp2 = temp1 * temp
    mrt = rl * (1.0 - 1.5 * temp2 * betal * s["con41"]) + 0.5 * temp1 * s["x1mth2"] * cos2u
    su = su - 0.25 * temp2 * s["x7thm1"] * sin2u
    xnode = nodem + 1.5 * temp2 * cosip * sin2u
    xinc = inclm + 1.5 * temp2 * cosip * sinip * cos2u
    mvt = rdotl - nm * temp1 * s["x1mth2"] * sin2u / SGP4_XKE
    rvdot = rvdotl + nm * temp1 * (s["x1mth2"] * cos2u + 1.5 * s["con41"]) / SGP4_XKE
    if mrt < 1.0:
        raise ValueError("the satellite has decayed")
    sinsu, cossu = math.sin(su), math.cos(su)
    snod, cnod = math.sin(xnode), math.cos(xnode)
    sini, cosi = math.sin(xinc), math.cos(xinc)
    xmx, xmy = -snod * cosi, cnod * cosi
    ux, uy, uz = xmx * sinsu + cnod * cossu, xmy * sinsu + snod * cossu, sini * sinsu
    vx, vy, vz = xmx * cossu - cnod * sinsu, xmy * cossu - snod * sinsu, sini * cossu
    mr = mrt * SGP4_RE
    vkm = SGP4_RE * SGP4_XKE / 60.0
    return ((mr * ux, mr * uy, mr * uz),
            ((mvt * ux + rvdot * vx) * vkm, (mvt * uy + rvdot * vy) * vkm, (mvt * uz + rvdot * vz) * vkm))


WGS84_A, WGS84_F = 6378.137, 1 / 298.257223563
EARTH_RATE = 7.292115146706979e-5        # rad/s
LIGHT_KMS = 299792.458


def observer_ecef(lat: float, lon: float, alt_km: float = 0.0) -> tuple[float, float, float]:
    """A place on the WGS-84 ellipsoid, in earth-fixed km."""
    la, lo = math.radians(lat), math.radians(lon)
    e2 = WGS84_F * (2 - WGS84_F)
    n = WGS84_A / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return ((n + alt_km) * math.cos(la) * math.cos(lo), (n + alt_km) * math.cos(la) * math.sin(lo),
            (n * (1 - e2) + alt_km) * math.sin(la))


def look(s: dict, unix: float, lat: float, lon: float, obs: tuple | None = None) -> dict:
    """Azimuth and elevation (degrees), range (km) and range rate (km/s,
    positive while receding) of the satellite from a place at a UTC time.
    TEME becomes earth-fixed by sidereal time alone: polar motion moves the
    answer by metres, far inside what a day-old element set is good for."""
    r, v = sgp4(s, (unix - s["epoch"]) / 60.0)
    th = gmst(unix / 86400.0 + UNIX_JD)
    c, sn = math.cos(th), math.sin(th)
    x, y, z = c * r[0] + sn * r[1], -sn * r[0] + c * r[1], r[2]
    # Earth-fixed velocity: rotated, less the frame's own turning.
    vx = c * v[0] + sn * v[1] + EARTH_RATE * y
    vy = -sn * v[0] + c * v[1] - EARTH_RATE * x
    vz = v[2]
    ox, oy, oz = obs or observer_ecef(lat, lon)
    dx, dy, dz = x - ox, y - oy, z - oz
    la, lo = math.radians(lat), math.radians(lon)
    sla, cla, slo, clo = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
    east = -slo * dx + clo * dy
    north = -sla * clo * dx - sla * slo * dy + cla * dz
    up = cla * clo * dx + cla * slo * dy + sla * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    return {"az": math.degrees(math.atan2(east, north)) % 360.0, "el": math.degrees(math.asin(up / rng)),
            "range": rng, "rate": (dx * vx + dy * vy + dz * vz) / rng}


def iss_passes(s: dict, lat: float, lon: float, start: float, hours: float = 72.0,
               min_el: float = 0.0, step: float = 30.0) -> list[dict]:
    """Every pass over the horizon between start and hours later. A 30 s
    step finds each one (the ISS is up for ten minutes on a good pass and a
    minute or two on a grazing one); rise and set are then bisected to under
    a second and the top found by golden-section search, so the times are
    as good as the element set, not as coarse as the step. A pass already
    under way at start is reported from start (inProgress). Passes whose
    top is under min_el are left out."""
    obs = observer_ecef(lat, lon)

    def el(t: float) -> float:
        return look(s, t, lat, lon, obs)["el"]

    def edge(a: float, b: float) -> float:          # el(a) and el(b) on opposite sides of 0
        up = el(a) > 0
        while b - a > 0.5:
            m = (a + b) / 2
            if (el(m) > 0) == up:
                a = m
            else:
                b = m
        return (a + b) / 2

    out: list[dict] = []
    end = start + hours * 3600
    t, prev = start, el(start)
    rise = start if prev > 0 else None
    while t < end:
        t2 = min(t + step, end)
        e2 = el(t2)
        if prev <= 0 < e2:
            rise = edge(t, t2)
        if rise is not None and (e2 <= 0 or t2 >= end):
            fall = edge(t, t2) if e2 <= 0 else t2
            g = (math.sqrt(5) - 1) / 2
            a, b = rise, fall
            while b - a > 1.0:
                c, d = b - g * (b - a), a + g * (b - a)
                if el(c) > el(d):
                    b = d
                else:
                    a = c
            top = (a + b) / 2
            hi, aos, los = look(s, top, lat, lon, obs), look(s, rise, lat, lon, obs), look(s, fall, lat, lon, obs)
            if hi["el"] >= min_el:
                out.append({"aos": round(rise), "tca": round(top), "los": round(fall),
                            "maxEl": round(hi["el"], 1), "aosAz": round(aos["az"]), "maxAz": round(hi["az"]),
                            "losAz": round(los["az"]), "minRangeKm": round(hi["range"]),
                            # The range rate at either end is the most the
                            # downlink is ever shifted by Doppler.
                            "aosRateKms": round(aos["rate"], 2), "losRateKms": round(los["rate"], 2),
                            "inProgress": rise <= start, "complete": e2 <= 0})
            rise = None
        t, prev = t2, e2
    return out
