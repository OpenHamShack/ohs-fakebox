  // Log page. The daemon does the work (sessions, suggestions, checking an
  // entry, ADIF); this shows the log, filters it (a few thousand entries is
  // nothing to filter here, and it stays instant on the touchscreen) and
  // edits one entry at a time. Times are UTC throughout, as logs are kept.
  let logOpen = false, logData = null, logQ = "", logBand = "", logBusy = false;
  const LOG_SR = [33, 66, 125, 250, 333, 500, 1000, 1500, 2000, 3000, 4000];
  const LOG_MODS = ["QPSK", "8PSK", "16APSK", "32APSK"];
  const LOG_CODECS = ["H.264", "H.265", "MPEG-2"];
  const p2l = (n) => String(n).padStart(2, "0");
  const utcDate = (t) => { const d = new Date(t * 1000); return `${d.getUTCFullYear()}-${p2l(d.getUTCMonth() + 1)}-${p2l(d.getUTCDate())}`; };
  const utcTime = (t) => { const d = new Date(t * 1000); return `${p2l(d.getUTCHours())}:${p2l(d.getUTCMinutes())}`; };
  const mhzText = (f) => f == null ? "" : Number(f).toFixed(3).replace(/\.?0+$/, "");
  const modeText = (m) => !m ? "" : [m.standard, m.modulation, m.fec, m.srKs ? m.srKs + " kS" : "", m.codec].filter(Boolean).join(" ");

  function showLog(open) {
    if (open === logOpen) return;
    logOpen = open;
    if (open) { showSettings(false); showQo(false); showAim(false); showHam(false); }
    document.body.classList.toggle("inlog", open);
    if (open) { $("logp").scrollTop = 0; paintLog(); loadLog(); }
  }
  $("logbtn").onclick = () => showLog(true);
  // Called from refresh() with /api/status's count of finished suggestions.
  function paintLogBadge(n) {
    $("logbadge").textContent = n || "";
    $("logbadge").classList.toggle("on", n > 0);
    $("logbtn").title = n ? `${n} contact${n === 1 ? "" : "s"} waiting to be logged` : "";
  }
  async function loadLog() {
    if (logBusy) return;
    logBusy = true;
    try { logData = await (await fetch("/api/log")).json(); paintLog(); }
    catch (e) { /* daemon restarting: keep what is shown */ }
    finally { logBusy = false; }
  }
  setInterval(() => { if (logOpen && !$("loged").classList.contains("open")) loadLog(); }, 10000);

  function paintLog() {
    const d = logData;
    if (!d) { $("loglist").innerHTML = '<div class="dim">Loading…</div>'; return; }
    const sugs = d.suggestions || [];
    $("logsugsec").style.display = sugs.length ? "" : "none";
    $("logsugs").innerHTML = sugs.map((s, i) => `<div class="logsug${s.live ? " live" : ""}">
        <div><b>${esc(s.call)}</b>${s.live ? '<span class="logtag">On now</span> ' : ""}${esc(s.summary.replace(/^\S+ /, ""))}
          ${s.entry.rstRcvd ? `<div class="dim">They reported ${esc(s.entry.rstRcvd)}</div>` : ""}</div>
        <div class="chips"><button class="chip on ctl" data-slog="${i}">Log</button><button class="chip ctl" data-sedit="${i}">Edit</button>
          <button class="chip ctl" data-sdis="${i}">Dismiss</button></div></div>`).join("");
    const st = d.stats || {};
    $("logcounts").textContent = st.qsos ? `${st.qsos} contact${st.qsos === 1 ? "" : "s"} · ${st.calls} callsign${st.calls === 1 ? "" : "s"}`
      + (st.bands || []).map((b) => ` · ${b.band} ${b.count}`).join("") + (st.sat ? ` · QO-100 ${st.sat}` : "") : "";
    $("logsearch").textContent = logQ ? `Search: ${logQ}` : "Search…";
    $("logsearch").classList.toggle("on", !!logQ);
    const bands = (st.bands || []).map((b) => b.band);
    if (logBand && !bands.includes(logBand)) logBand = "";
    $("logbands").innerHTML = bands.length > 1 ? [["", "All bands"], ...bands.map((b) => [b, b])]
      .map(([v, l]) => `<button class="chip${logBand === v ? " on" : ""}" data-lband="${v}">${esc(l)}</button>`).join("") : "";
    const q = logQ.toUpperCase();
    const rows = (d.qsos || []).filter((x) => (!logBand || x.band === logBand)
      && (!q || [x.call, x.name, x.qth, x.locator, x.notes].some((v) => String(v || "").toUpperCase().includes(q))));
    $("loglist").innerHTML = rows.map((x) => `<button class="logrow" data-lid="${esc(x.id)}">
        <span class="lt">${utcDate(x.on)}<br>${utcTime(x.on)}${x.off ? "–" + utcTime(x.off) : ""}&nbsp;UTC</span>
        <span><span class="lc">${esc(x.call)}</span><small>${esc([x.name, x.qth, x.locator].filter(Boolean).join(" · "))}</small></span>
        <span>${esc(x.band || "")}<small>${esc([x.freqMHz != null ? "tx " + mhzText(x.freqMHz) : "", x.freqRxMHz != null ? "rx " + mhzText(x.freqRxMHz) : ""].filter(Boolean).join(" · "))}</small></span>
        <span class="lr">${x.rstSent ? "sent " + esc(x.rstSent) : ""}${x.rstSent && x.rstRcvd ? "<br>" : ""}${x.rstRcvd ? "rcvd " + esc(x.rstRcvd) : ""}
          <small>${esc(modeText(x.rxMode))}</small></span>
        <span>${x.sat ? '<span class="logtag sat">QO-100</span>' : ""}</span></button>`).join("")
      || `<div class="dim">${(d.qsos || []).length ? "Nothing matches." : "No contacts logged yet."}</div>`;
    $("lognote").textContent = (d.station && d.station.callsign ? `Exports carry ${d.station.callsign}` : "Set your callsign (Settings → Station) for the export")
      + (d.station && d.station.locator ? ` and ${d.station.locator}` : "") + ". ADIF imports into most loggers; CSV opens in a spreadsheet.";
  }

  $("logp").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const sugs = (logData && logData.suggestions) || [];
    if (b.dataset.slog !== undefined) {
      const s = sugs[Number(b.dataset.slog)]; if (!s) return;
      b.disabled = true; b.textContent = "Logging…";
      const r = await logPost("/api/log/save", { qso: s.entry, fromSuggestion: s.id });
      if (!r.ok) { b.disabled = false; b.textContent = "Log"; alertLog(r.error); }
      return loadLog();
    }
    if (b.dataset.sedit !== undefined) { const s = sugs[Number(b.dataset.sedit)]; if (s) openLogEd(s.entry, s.id); return; }
    if (b.dataset.sdis !== undefined) {
      const s = sugs[Number(b.dataset.sdis)]; if (!s) return;
      b.disabled = true;
      await logPost("/api/log/dismiss", { id: s.id });
      return loadLog();
    }
    if (b.dataset.lband !== undefined) { logBand = b.dataset.lband; return paintLog(); }
    if (b.dataset.lid) { const x = (logData.qsos || []).find((q) => q.id === b.dataset.lid); if (x) openLogEd(x); return; }
    if (b.id === "logsearch") return Keyboard.open({ title: "Search the log: callsign, name, QTH or notes", value: logQ, maxLength: 30,
                                                    shift: true, onOk: (v) => { logQ = v.trim(); paintLog(); } });
    if (b.id === "lognew") return openLogEd(null);
  });
  function alertLog(msg) { $("lognote").textContent = msg || "Not saved"; }
  async function logPost(path, body) {
    try {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const j = await r.json().catch(() => ({}));
      return { ok: r.ok, ...j };
    } catch (e) { return { ok: false, error: "OpenHamShack did not answer" }; }
  }

  // The entry being edited. Times are kept as date + HH:MM[:SS] strings
  // while editing, so a typed value is shown as typed until it is saved.
  let LE = null, logDelArm = null;
  function txModeNow() {
    return { standard: cfg.mode === "dvbs2" ? "DVB-S2" : "DVB-S", modulation: String(cfg.constellation || "qpsk").toUpperCase(),
             fec: cfg.fec || null, srKs: Number(cfg.symbolRateKs) || null, codec: { hevc: "H.265", h264: "H.264" }[cfg.codec] || null };
  }
  function hms(t) { const d = new Date(t * 1000); return `${p2l(d.getUTCHours())}:${p2l(d.getUTCMinutes())}:${p2l(d.getUTCSeconds())}`; }
  function openLogEd(q, sid) {
    const now = Date.now() / 1000;
    const base = q || { call: "", on: now, off: null, freqMHz: Number(cfg.frequencyMHz) || null,
                        freqRxMHz: Number(cfg.rxFrequencyMHz) || null, rxMode: null, txMode: txModeNow(),
                        sat: Number(cfg.rxOffsetMHz) > 0 && Number(cfg.rxFrequencyMHz) >= 10000, gainDb: cfg.gainDb ?? null,
                        source: "manual" };
    LE = { ...JSON.parse(JSON.stringify(base)), sid: sid || null, date: utcDate(base.on), ton: hms(base.on),
           toff: base.off ? hms(base.off) : "" };
    LE.rxMode = LE.rxMode || {}; LE.txMode = LE.txMode || {};
    $("logedtitle").textContent = q && q.id ? `Contact with ${q.call}` : sid ? "Log this contact" : "New contact";
    $("logedsrc").textContent = q && q.source === "suggested" ? "from what OpenHamShack received" : "";
    $("logeddel").style.display = q && q.id ? "" : "none";
    $("logederr").textContent = auth.authorised ? "" : "View only: enter the PIN to change the log.";
    logDelArm = null; $("logeddel").classList.remove("arm"); $("logeddel").textContent = "Delete";
    paintLogEd();
    $("loged").classList.add("open");
  }
  // A signal's mode is a line of text until tapped; open, it is the chips
  // to change it. Both open at once made the form three screens long.
  function modeBlock(key, m) {
    const head = `<div class="row2"><button class="chip lv wide ctl${modeText(m) ? "" : " empty"}" data-lmopen="${key}">`
      + `${esc(modeText(m) || "Not known")}${LE.open === key ? " ▴" : " ▾"}</button></div>`;
    if (LE.open !== key) return head;
    const fecs = (m.standard === "DVB-S" ? FEC.dvbs : FEC.dvbs2);
    const row = (field, vals, label) => `<div class="chips">${vals.map((v) =>
      `<button class="chip ctl${String(m[field] ?? "") === String(v) ? " on" : ""}" data-lm="${key}" data-lmf="${field}" data-lmv="${v}">${label ? label(v) : v}</button>`).join("")}</div>`;
    return head + row("standard", ["DVB-S", "DVB-S2"]) + row("modulation", m.standard === "DVB-S" ? ["QPSK"] : LOG_MODS)
      + row("fec", fecs) + row("srKs", LOG_SR.includes(Number(m.srKs)) || !m.srKs ? LOG_SR : [...LOG_SR, m.srKs], (v) => v + " kS")
      + `<div class="chips"><button class="chip ctl" data-lsr="${key}">Other rate…</button>${LOG_CODECS.map((v) =>
        `<button class="chip ctl${m.codec === v ? " on" : ""}" data-lm="${key}" data-lmf="codec" data-lmv="${v}">${v}</button>`).join("")}</div>`;
  }
  function paintLogEd() {
    const show = (f, text, empty) => {
      const b = document.querySelector(`#loged [data-lf="${f}"]`);
      b.textContent = text || empty; b.classList.toggle("empty", !text);
    };
    show("call", LE.call, "Their callsign");
    show("date", LE.date, "YYYY-MM-DD");
    show("ton", LE.ton.slice(0, 5), "start");
    show("toff", LE.toff.slice(0, 5), "end");
    show("freqMHz", LE.freqMHz != null ? mhzText(LE.freqMHz) + " MHz" : "", "not sent");
    show("freqRxMHz", LE.freqRxMHz != null ? mhzText(LE.freqRxMHz) + " MHz" : "", "—");
    for (const f of ["rstSent", "rstRcvd", "name", "qth", "locator", "notes"]) show(f, LE[f], { rstSent: "e.g. D4.3 (8.6 dB)", rstRcvd: "e.g. D3 (7.5 dB)", notes: "Notes" }[f] || "—");
    show("gainDb", LE.gainDb != null ? `${LE.gainDb} dB` : "", "—");
    show("powerW", LE.powerW != null ? `${LE.powerW} W` : "", "— W");
    $("logsat").classList.toggle("on", !!LE.sat);
    $("logrxmode").innerHTML = modeBlock("rxMode", LE.rxMode);
    $("logtxmode").innerHTML = modeBlock("txMode", LE.txMode);
  }
  // Date and time from the pad: digits, with "." standing in for any separator.
  function parseDate(s) {
    const m = String(s).replace(/\D+/g, ".").match(/^(\d{4})\.?(\d{2})\.?(\d{2})$/) || String(s).match(/^(\d{4})(\d{2})(\d{2})$/);
    return m ? `${m[1]}-${m[2]}-${m[3]}` : null;
  }
  function parseTime(s) {
    const digits = String(s).replace(/\D/g, "");
    const m = digits.match(/^(\d{2})(\d{2})(\d{2})?$/) || String(s).match(/^(\d{1,2})\.(\d{2})(?:\.(\d{2}))?$/);
    if (!m) return null;
    const [h, mi, se] = [Number(m[1]), Number(m[2]), Number(m[3] || 0)];
    return h < 24 && mi < 60 && se < 60 ? `${p2l(h)}:${p2l(mi)}:${p2l(se)}` : null;
  }
  const epoch = (date, time) => Date.parse(`${date}T${time}Z`) / 1000;

  $("loged").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b || !LE) return;
    const f = b.dataset.lf;
    const err = (m) => { $("logederr").textContent = m; };
    if (f && ["call", "rstSent", "rstRcvd", "name", "qth", "locator", "notes"].includes(f)) {
      const titles = { call: "Their callsign", rstSent: "Report you sent (e.g. D4.3 (8.6 dB))", rstRcvd: "Report you received",
                       name: "Their name", qth: "Their QTH", locator: "Their locator (e.g. IO85jw)", notes: "Notes" };
      return Keyboard.open({ title: titles[f], value: LE[f] || "", maxLength: { call: 15, locator: 10, notes: 500, qth: 60 }[f] || 40,
                             shift: ["call", "locator", "rstSent", "rstRcvd"].includes(f), onOk: (v) => { LE[f] = f === "call" ? v.toUpperCase().replace(/\s/g, "") : v; paintLogEd(); } });
    }
    if (f === "date") return openPad("__logdate", "Date, UTC: YYYYMMDD", "num", (v) => { const d = parseDate(v); if (d) LE.date = d; else err("Date as YYYYMMDD, e.g. 20261004"); paintLogEd(); });
    if (f === "ton" || f === "toff") return openPad("__logtime", `${f === "ton" ? "Start" : "End"} time, UTC: HHMM`, "num", (v) => {
      const t = parseTime(v); if (t) LE[f] = t; else err("Time as HHMM, e.g. 1850"); paintLogEd(); });
    // 0 clears it: a report-only entry has no transmit frequency.
    if (f === "freqMHz" || f === "freqRxMHz") return openPad("__logf", `You ${f === "freqMHz" ? "transmitted" : "received"} on (MHz); 0 for none`, "num", (v) => {
      const n = Number(v); if (Number.isFinite(n) && n >= 0) LE[f] = n || null; else err("A frequency in MHz"); paintLogEd(); });
    if (f === "gainDb") return openPad("__loggain", "Transmit gain: dB below full (10 means -10 dB)", "num", (v) => {
      const n = Number(v); if (Number.isFinite(n)) LE.gainDb = -Math.abs(n); paintLogEd(); });
    if (f === "powerW") return openPad("__logpwr", "Power at the antenna, watts; 0 for not known", "num", (v) => {
      const n = Number(v); if (Number.isFinite(n) && n >= 0) LE.powerW = n || null; paintLogEd(); });
    if (b.id === "lognow") { LE.toff = hms(Date.now() / 1000); return paintLogEd(); }
    if (b.id === "logsat") { LE.sat = !LE.sat; return paintLogEd(); }
    if (b.id === "logcur") {
      Object.assign(LE, { freqMHz: Number(cfg.frequencyMHz) || null, freqRxMHz: Number(cfg.rxFrequencyMHz) || null,
                          txMode: txModeNow(), gainDb: cfg.gainDb ?? null,
                          sat: Number(cfg.rxOffsetMHz) > 0 && Number(cfg.rxFrequencyMHz) >= 10000 });
      return paintLogEd();
    }
    if (b.dataset.lmopen) { LE.open = LE.open === b.dataset.lmopen ? null : b.dataset.lmopen; return paintLogEd(); }
    if (b.dataset.lm) {
      const m = LE[b.dataset.lm], fld = b.dataset.lmf, v = fld === "srKs" ? Number(b.dataset.lmv) : b.dataset.lmv;
      m[fld] = m[fld] === v ? null : v;                    // a second tap clears it: "not known"
      if (fld === "standard" && v === "DVB-S") { m.modulation = "QPSK"; if (m.fec && !FEC.dvbs.includes(m.fec)) m.fec = null; }
      return paintLogEd();
    }
    if (b.dataset.lsr) return openPad("__logsr", "Symbol rate, kS", "num", (v) => { const n = Number(v); if (n > 0) LE[b.dataset.lsr].srKs = n; paintLogEd(); });
    if (b.id === "logedcancel") { $("loged").classList.remove("open"); LE = null; return; }
    if (b.id === "logeddel") {
      if (logDelArm !== LE.id) {
        logDelArm = LE.id; b.classList.add("arm"); b.textContent = "Tap again to delete";
        setTimeout(() => { if (logDelArm) { logDelArm = null; b.classList.remove("arm"); b.textContent = "Delete"; } }, 4000);
        return;
      }
      const r = await logPost("/api/log/delete", { id: LE.id });
      if (!r.ok) return err(r.error || "Not deleted");
      $("loged").classList.remove("open"); LE = null;
      return loadLog();
    }
    if (b.id === "logedsave") {
      const on = epoch(LE.date, LE.ton);
      let off = LE.toff ? epoch(LE.date, LE.toff) : null;
      if (off != null && off < on) off += 86400;          // over midnight UTC
      const { sid, date, ton, toff, open, ...qso } = LE;
      Object.assign(qso, { on, off });
      b.disabled = true;
      const r = await logPost("/api/log/save", { qso, fromSuggestion: sid || undefined });
      b.disabled = false;
      if (!r.ok) return err(r.error || "Not saved");
      $("loged").classList.remove("open"); LE = null;
      return loadLog();
    }
  });
