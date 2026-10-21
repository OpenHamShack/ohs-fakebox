  // Updates panel. Update buttons arm on the first tap, run on the second.
  let updArmed = null, updOpen = false;
  // PicoTuner: ptChoice is the firmware picked here; null follows what the
  // PicoTuner runs (the server's "firmware"), so only a tap makes it differ.
  let ptChoice = null, ptLast = null;
  const PT_WAITING = ["starting", "downloading", "stopping", "rebooting", "waiting"];
  function ptView(p, j) {
    if (!p) { $("ptver").textContent = "—"; $("ptupd").disabled = true; $("ptupd").textContent = "Nothing to do"; return; }
    ptLast = p;
    const fw = ptChoice || p.firmware, running = p.mode === "usb" ? "usb" : p.mode === "ethernet" ? p.board : null;
    const latest = p.latest[fw];
    $("ptver").textContent = (p.current || "unknown") + (latest ? `  ·  latest ${fw === "usb" ? "v" : ""}${latest}` : "")
      + (p.error && fw === "usb" ? "  ·  " + p.error : "");
    $("ptfw").innerHTML = Object.entries(p.choices).map(([k, label]) =>
      `<button class="chip${k === fw ? " on" : ""}" data-fw="${k}">${label}</button>`).join("");
    for (const c of $("ptfw").querySelectorAll("button")) c.onclick = () => {
      ptChoice = c.dataset.fw === p.firmware ? null : c.dataset.fw;
      if (updArmed === $("ptupd")) updArmed = null;
      ptView(ptLast, null);
    };
    // A switch, said plainly: what it needs and what stops working.
    const sw = running && fw !== running ? fw : !running && fw !== "usb" ? fw : null;
    $("ptwarn").style.display = sw ? "" : "none";
    if (sw) $("ptwarn").textContent = sw === "usb"
      ? "Switching to the USB firmware: the PicoTuner then works over USB with OpenHamShack's own receiver, and no longer on the network. It must be a PicoTuner's own Pico or Pico 2."
      : `Switching to the Ethernet firmware for a WIZnet ${sw.toUpperCase()}-EVB-Pico. It only runs on that board - on a plain Pico, or the other WIZnet board, its LEDs flash NO in Morse and it does nothing until the USB firmware is loaded again (BOOTSEL button). After it, the PicoTuner works over the network only.`;
    const b = $("ptupd");
    // While it runs, the step in hand fills the box - "Hold the BOOTSEL
    // button..." is an instruction to someone at the box, not a status
    // line - and the firmware cannot be changed under it.
    const mine = j && j.kind === "picotuner" && !["idle", "done", "failed"].includes(j.phase);
    $("ptfw").style.display = mine ? "none" : "";
    if (mine) { $("ptwarn").style.display = ""; $("ptwarn").textContent = j.message || "Starting…"; }
    const waiting = mine && PT_WAITING.includes(j.phase);
    if (waiting) { b.disabled = false; b.classList.remove("armed"); b.textContent = "Cancel"; return; }
    if (b === updArmed) return;
    const busy = j && j.phase && !["idle", "done", "failed"].includes(j.phase);
    // An unread version is not "up to date": the tuner may be too old to say.
    const switching = !!sw, upToDate = !switching && running && !p.available && p.version !== null;
    b.disabled = !!busy || !!upToDate || !latest;
    b.classList.remove("armed");
    b.textContent = upToDate ? "Up to date" : !running ? "Load" : switching ? "Switch" : "Update";
  }
  async function loadUpdates(check) {
    try {
      const u = await (await fetch("/api/updates" + (check ? "?check=1" : ""))).json();
      const line = (x) => (x.current || "unknown") + (x.latest ? "  ·  latest " + x.latest : "") + (x.error ? "  ·  " + x.error : "");
      const L = u.openhamshack, j = u.job || {}, a = u.applied;
      const busy = j.phase && !["idle", "done", "failed"].includes(j.phase);
      $("lbver").textContent = line({ ...L, latest: L.latest && L.from === "beta" ? L.latest + " (beta)" : L.latest });
      $("plver").textContent = line(u.pluto);
      // A version gone back from is never "available", but stays one
      // deliberate (two-tap) Reinstall away rather than vanishing.
      $("ohsupd").disabled = !L.available && !L.skipped; $("plupd").disabled = !u.pluto.available;
      if ($("ohsupd") !== updArmed) { $("ohsupd").classList.remove("armed");
        $("ohsupd").textContent = L.available ? "Update" : L.skipped ? `Reinstall ${L.skipped}` : "Up to date"; }
      if ($("plupd") !== updArmed) { $("plupd").classList.remove("armed"); $("plupd").textContent = $("plupd").disabled ? "Up to date" : "Update"; }
      for (const c of $("lbch").querySelectorAll("button")) {
        c.classList.toggle("on", c.dataset.ch === L.channel);
        c.disabled = !auth.authorised;
      }
      const feed = L.from || L.channel;
      const note = L.ahead
        ? `This box runs ${L.current}, newer than ${feed} (${L.latest}). It stays on ${L.current}, and is offered ${feed} again once ${feed} passes it.`
        : L.skipped
        ? `You went back from ${L.skipped}, so it is not offered as an update. Tap Reinstall ${L.skipped} twice to have it again anyway.`
        : L.held && L.available
        ? `OpenHamShack tried to install ${L.held} by itself at two starts and it did not work, so it no longer tries on its own. Tap Update twice to try again, or send a report from Diagnostics.`
        : "";
      $("lbnote").style.display = note ? "" : "none";
      $("lbnote").textContent = note;
      const back = $("lbback");
      $("lbbackrow").style.display = L.previous ? "flex" : "none";
      if (back !== updArmed) { back.classList.remove("armed"); back.textContent = `Go back to ${L.previous}`; }
      back.disabled = !!busy;
      ptView(u.picotuner, j);
      $("updlive").style.display = u.live ? "" : "none";
      if (u.live) { $("ohsupd").disabled = true; $("ohsupd").textContent = "Off while trying"; back.disabled = true; }
      paintOsUpdates(u.os);
      const who = { pluto: "Pluto", picotuner: "PicoTuner" }[j.kind] || "OpenHamShack";
      const ptBoxed = j.kind === "picotuner" && !["idle", "done", "failed"].includes(j.phase);   // said in its row
      $("updjob").textContent = ptBoxed ? "" : j.phase && j.phase !== "idle" ? `${who}: ${j.message || j.phase}`
        : a ? `OpenHamShack: ${a.message}` : "";
    } catch (e) { $("updjob").textContent = "OpenHamShack is restarting…"; }
  }
  // Debian security updates: one line of when and what, and the packages.
  function paintOsUpdates(os) {
    if (!os) return;
    const when = (t) => new Date(t * 1000).toLocaleString("en-GB",
      { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    let line;
    if (os.live) line = "Off while trying OpenHamShack from the USB stick";
    else if (!os.installed) line = "Not set up on this box yet - arrives with the next OpenHamShack update";
    else if (!os.lastRun) line = os.lastCheck ? `First check waiting: ${os.message || "skipped while busy"}`
      : "First check within the hour, when nothing is on air or recording";
    else line = `Last checked ${when(os.lastRun)}: ` + (os.result === "failed" ? `failed - ${os.message}`
      : os.result === "updated" ? os.message : "up to date");
    if (!os.live && os.updatedAt && os.result !== "updated") line += `  ·  last updated ${when(os.updatedAt)}`;
    if (os.restartNeeded) line += "  ·  restart needed";
    $("osver").textContent = line;
    const p = os.live ? [] : os.packages || [];
    $("ospkgs").textContent = p.length
      ? (os.restartNeeded ? "Installed, taking effect at the next restart: " : "Last installed: ")
        + p.slice(0, 12).join(", ") + (p.length > 12 ? ` and ${p.length - 12} more` : "") : "";
  }
  // Trying from the stick: the banner on every page, and Install, armed on
  // the first tap like everything that cannot be taken back. The installer
  // itself still asks which disk, and twice, before it writes anything.
  let liveArmed = false, osLater = false;
  // The security-update restart is news, never urgent, so it keeps out of
  // the way: not over the setup wizard, not on a phone (it took a quarter
  // of an 844 px screen on every page), and not to a browser that could not
  // restart the box anyway.
  const PHONE = matchMedia("(max-width: 640px)");
  function paintLiveOs(body) {
    $("livebanner").classList.toggle("on", !!body.live);
    $("osbanner").classList.toggle("on", !!body.osRestart && !body.live && !osLater && auth.authorised
      && !PHONE.matches && !$("setup").classList.contains("open"));
  }
  $("liveinstall").onclick = async () => {
    const b = $("liveinstall");
    if (!liveArmed) {
      liveArmed = true; b.classList.add("arm"); b.textContent = "Tap again to open the installer";
      setTimeout(() => { if (liveArmed) { liveArmed = false; b.classList.remove("arm"); b.textContent = "Install…"; } }, 5000);
      return;
    }
    liveArmed = false; b.classList.remove("arm");
    const r = await fetch("/api/live/install", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const j = await r.json().catch(() => ({}));
    b.textContent = r.ok ? "Opening the installer…"
      : r.status === 403 ? "Install from the PC's own screen" : (j.error || "Refused");
  };
  $("osok").onclick = () => { osLater = true; $("osbanner").classList.remove("on"); };
  // Updating at start-up (status.autoUpdate): a banner while it installs,
  // and once for what it came to - this start's attempt, or the last
  // start's when that one ended in a restart ("last"). Dismissed per
  // message, so a new one still shows. A page that watched it install
  // reloads when the new version answers: its own script is the old one.
  let autoDismissed = "", autoSawInstall = false, autoFirstVersion = null;
  function autoUpdateBanner(a, here) {
    const last = a.last || null, v = a.version;
    if (a.phase === "installing")
      return ["busy", `Updating OpenHamShack to ${v}… the console will restart in a moment.`,
              a.message && a.message !== "Starting" ? a.message : "Nothing to do: it carries on by itself."];
    if (a.phase === "failed")
      return ["bad", `OpenHamShack could not update itself to ${v}.`, `${a.message || ""} It will try again at the next start.`];
    if (a.phase === "held")
      return ["bad", `OpenHamShack could not update itself to ${v}, at two starts.`,
              `${a.message || ""} It will not try that version again by itself: Settings › Software & power › Updates.`];
    if (last && last.phase === "updated") return ["done", `OpenHamShack updated itself to ${last.version}.`, ""];
    if (last && last.phase === "failed")
      return ["bad", `OpenHamShack could not update itself to ${last.version}, so it is still on ${here}.`, last.message || ""];
    return null;
  }
  function paintAutoUpdate(body) {
    const a = body.autoUpdate || {};
    if (autoFirstVersion === null) autoFirstVersion = body.version;
    if (a.phase === "installing") autoSawInstall = true;
    else if (autoSawInstall && body.version !== autoFirstVersion) { location.reload(); return; }
    const b = body.live ? null : autoUpdateBanner(a, body.version);
    const sig = b ? b.join("|") : "";
    const on = !!b && (b[0] === "busy" || sig !== autoDismissed);
    $("autobanner").classList.toggle("on", on);
    if (!on) return;
    $("autobanner").className = "notice on " + b[0];
    // One word per kind across every notice: OK for news, Dismiss for a problem.
    $("autook").textContent = b[0] === "bad" ? "Dismiss" : "OK";
    $("automsg").textContent = b[1];
    $("autowhy").textContent = b[2];
    $("autobanner").dataset.sig = sig;
  }
  $("autook").onclick = () => { autoDismissed = $("autobanner").dataset.sig || ""; $("autobanner").classList.remove("on"); };
  function armUpdate(btn, kind, body) {
    if (updArmed !== btn) {
      const t = btn.textContent;
      const verb = t === "Switch" ? "switch" : t === "Load" ? "load" : t.startsWith("Reinstall") ? "reinstall"
        : t.startsWith("Go back") ? "go back" : "update";
      updArmed = btn; btn.classList.add("armed"); btn.textContent = "Tap again to " + verb;
      setTimeout(() => { if (updArmed === btn) { updArmed = null; loadUpdates(false); } }, 5000);
      return;
    }
    updArmed = null; btn.classList.remove("armed"); btn.textContent = "Starting…";
    fetch("/api/updates/" + kind, { method: "POST", headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify(body || {}) })
      .then(async (r) => {
        if (r.ok) return loadUpdates(false);
        $("updjob").textContent = (await r.json().catch(() => ({}))).error || "Refused";
      });
  }
  // Shut down / Restart: armed on the first tap, sent on a second within 5 s,
  // the same as the update buttons - a stray tap must not power the box off.
  let pwrArmed = null;
  for (const b of [$("pwroff"), $("pwrboot"), $("osboot")]) b.onclick = async () => {
    const label = b.dataset.power === "poweroff" ? "Shut down" : "Restart";
    if (pwrArmed !== b) {
      if (pwrArmed) { pwrArmed.classList.remove("arm"); pwrArmed.textContent = pwrArmed.dataset.power === "poweroff" ? "Shut down" : "Restart"; }
      pwrArmed = b; b.classList.add("arm"); b.textContent = `Tap again to ${label.toLowerCase()}`;
      setTimeout(() => { if (pwrArmed === b) { pwrArmed = null; b.classList.remove("arm"); b.textContent = label; } }, 5000);
      return;
    }
    pwrArmed = null; b.classList.remove("arm");
    const r = await fetch("/api/power", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify({ action: b.dataset.power }) });
    const j = await r.json().catch(() => ({}));
    b.textContent = r.ok ? (b.dataset.power === "poweroff" ? "Shutting down…" : "Restarting…") : (j.error || "Refused");
  };
  // Until the answer comes, every row says it is being checked and no
  // button looks as if it would do something: the window used to open on
  // a dash in every row and three live-looking Update buttons.
  function updChecking() {
    for (const id of ["lbver", "plver", "ptver", "osver"]) $(id).textContent = "Checking…";
    for (const id of ["ohsupd", "plupd", "ptupd"]) {
      if ($(id) === updArmed) continue;
      $(id).disabled = true; $(id).classList.remove("armed"); $(id).textContent = "Checking…";
    }
  }
  $("updbtn").onclick = () => { updOpen = true; updChecking(); $("upd").classList.add("open"); loadUpdates(false); };
  $("updclose").onclick = () => { updOpen = false; $("upd").classList.remove("open"); };
  $("updcheck").onclick = () => { updChecking(); loadUpdates(true); };
  $("ohsupd").onclick = () => armUpdate($("ohsupd"), "openhamshack");
  $("lbback").onclick = () => armUpdate($("lbback"), "rollback");
  // The channel is an ordinary setting, through /api/config, so a remote
  // browser needs the PIN as for any other; the daemon's refusal is shown.
  for (const c of $("lbch").querySelectorAll("button")) c.onclick = async () => {
    const r = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify({ updateChannel: c.dataset.ch }) });
    if (!r.ok) { $("updjob").textContent = (await r.json().catch(() => ({}))).error || "Refused"; return; }
    loadUpdates(false);
  };
  $("plupd").onclick = () => armUpdate($("plupd"), "pluto");
  // Cancel needs no second tap: it only ever ends a wait, never a copy.
  $("ptupd").onclick = () => $("ptupd").textContent === "Cancel"
    ? fetch("/api/updates/cancel", { method: "POST" }).then(() => loadUpdates(false))
    : armUpdate($("ptupd"), "picotuner", { firmware: ptChoice || (ptLast && ptLast.firmware) || "usb" });
  setInterval(() => { if (updOpen) loadUpdates(false); }, 2000);
