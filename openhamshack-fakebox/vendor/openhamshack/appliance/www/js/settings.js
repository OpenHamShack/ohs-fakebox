  // Station text, entered from the overlay editor, the header and Settings
  // alike: one editor each, so the rules cannot drift apart.
  function editLocator(value, onErr, after) {
    if (!canChange()) return;
    Keyboard.open({ title: "Maidenhead locator, 4 or 6 characters (e.g. IO85 or IO85jw): for the weather and ISS passes",
      value: value || "", maxLength: 6, shift: true, onOk: async (v) => {
        v = v.trim();
        if (v && !/^[A-Ra-r]{2}[0-9]{2}([A-Xa-x]{2})?$/.test(v)) return onErr(`${v} is not a locator`);
        onErr("");
        await save({ locator: v });
        if (after) after();
      } });
  }
  function editCallsign(onErr) {
    if (!canChange()) return;
    const err = onErr || ((m) => { $("stationerr").textContent = m; });
    // Capitals, digits and "/" are all the daemon keeps (callsign()); shift
    // starts on so a callsign is typed the way it is written. OPENHAMSHACK is the
    // placeholder a new box comes with, which the daemon will not key under.
    Keyboard.open({ title: "Callsign (letters, digits and /)", value: hasCall() ? cfg.callsign : "", maxLength: 12, shift: true,
      onOk: (v) => {
        v = v.trim().toUpperCase();
        if (!/^[A-Z0-9/]+$/.test(v)) return err(v ? `${v}: only letters, digits and / are sent` : "");
        if (v === "OPENHAMSHACK") return err("OPENHAMSHACK is the placeholder: enter your own callsign");
        err("");
        save({ callsign: v });
      } });
  }
  // Whether the station has a callsign of its own: what the daemon's
  // callsign() would send, other than the placeholder.
  function hasCall() {
    const c = String(cfg.callsign || "").toUpperCase().replace(/[^A-Z0-9/]/g, "");
    return !!c && c !== "OPENHAMSHACK";
  }
  // The licence region as the daemon's licence_region() reads it: anything
  // it does not know is Scotland, the strictest.
  function licRegion() {
    const r = cfg.licenceRegion || "scotland";
    return ["scotland", "uk", "iaru1", "other"].includes(r) ? r : "scotland";
  }
  $("setcall").onclick = () => editCallsign();
  $("setupagain").onclick = () => openSetup(0);
  $("setloc").onclick = () => editLocator(cfg.locator, (m) => { $("stationerr").textContent = m; });

  // The PIN is a secret only the box's own screen may set; the daemon refuses
  // it from anywhere else, and says why, so the refusal is shown, not lost.
  async function savePin(pin) {
    if (!/^[0-9]{4,8}$/.test(pin)) { pinNote("A PIN is 4 to 8 digits", true); return; }
    const r = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remotePin: pin }) }).catch(() => null);
    if (!r) return pinNote("OpenHamShack did not answer", true);
    if (!r.ok) return pinNote((await r.json().catch(() => ({}))).error || "Not saved", true);
    pinNote("PIN saved", false);
    refresh();
  }
  let pinMsg = null;
  function pinNote(text, err) { pinMsg = { text, err, until: Date.now() + 5000 }; paintPin(); }
  let pinSet = false;
  function paintPin() {
    const note = $("pinnote");
    if (pinMsg && Date.now() < pinMsg.until) { note.textContent = pinMsg.text; note.className = pinMsg.err ? "err" : "dim"; }
    else { note.className = "dim"; note.textContent = pinSet ? "set" : "not set: remote browsers can only look"; }
    $("setpin").textContent = pinSet ? "Change…" : "Set…";
    $("setpin").disabled = !auth.local;
    if (!auth.local) $("setpin").textContent = "On the box only";
  }

  function paintSettings(body) {
    pinSet = !!body.config.remotePinSet;
    paintPin();
    $("aboutver").textContent = body.version || "—";
    // The box's own screen reaches the console on localhost, which is no use
    // to anyone wanting to connect; a remote browser's address is the box's.
    const h = location.hostname;
    $("aboutaddr").textContent = !h || h === "localhost" || h.startsWith("127.") ? "this screen (localhost)" : location.host;
  }

  // Settings page.
  // Categories, in menu order. The last one chosen is remembered per browser
  // (a convenience: a private window or blocked storage starts at Station).
  // Station is everything set once per station - callsign, locator, tuner,
  // radio, licence - which were three categories, two of them a single row;
  // Diagnostics is its own, rather than half of Software & power.
  const SETCATS = [["station", "Station"], ["screens", "Screens"], ["network", "Network"],
                   ["transmission", "Transmission"], ["security", "Security"], ["controller", "Controller"],
                   ["backup", "Backup"], ["software", "Software & power"], ["diagnostics", "Diagnostics"]];
  let setOpen = false, setCat = "station";
  try {
    const c = localStorage.getItem("lbSetCat"), was = { radio: "station", licence: "station" }[c] || c;
    if (SETCATS.some(([k]) => k === was)) setCat = was;
  } catch (e) { /* default */ }
  $("setcats").innerHTML = SETCATS.map(([k, label]) => `<button class="chip" data-cat="${k}">${esc(label)}</button>`).join("");
  function showSetCat(cat) {
    setCat = cat;
    try { localStorage.setItem("lbSetCat", cat); } catch (e) { /* this page only */ }
    for (const b of $("setcats").children) b.classList.toggle("on", b.dataset.cat === cat);
    for (const s of $("setgrid").children) s.classList.toggle("cur", s.dataset.cat === cat);
    $("settings").scrollTop = 0;
    const on = $("setcats").querySelector(".on");
    if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (setOpen && cat === "screens" && typeof loadDisplay === "function") loadDisplay();
    if (setOpen && cat === "diagnostics" && typeof loadDiag === "function") loadDiag(false);
    if (setOpen && cat === "backup" && typeof loadBackup === "function") loadBackup();
  }
  $("setcats").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-cat]"); if (b) showSetCat(b.dataset.cat);
  });
  // cat: open at that category (a link from elsewhere, e.g. the network
  // banner); without it, the one last used.
  function showSettings(open, cat) {
    setOpen = open;
    if (open) { showQo(false); if (typeof showAim === "function") showAim(false); if (typeof showHam === "function") showHam(false); if (typeof showLog === "function") showLog(false); }
    document.body.classList.toggle("insettings", open);
    if (open) { showSetCat(cat || setCat); loadPad(); loadNet(); }
  }
  showSetCat(setCat);
  $("setbtn").onclick = () => showSettings(true);
  $("setback").onclick = () => { showSettings(false); showQo(false); showAim(false); showHam(false); showLog(false); };
