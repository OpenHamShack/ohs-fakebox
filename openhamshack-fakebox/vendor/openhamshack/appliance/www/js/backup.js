  // Backup and restore. Opening a file (from USB, or uploaded from this
  // computer) asks the daemon to check it and hold it; the card shows what
  // is in it, and Restore - two taps within 5 s, like Shut down - restores
  // exactly what was shown. "Include passwords" is this page's own choice,
  // off every time the page opens: it is a decision about one file.
  let bkView = null, bkSecrets = false, bkBusy = false, bkHeld = null, bkReplace = false, bkArmed = null;
  async function loadBackup() {
    try {
      const r = await fetch("/api/backup");
      if (!r.ok) throw new Error("HTTP " + r.status);
      bkView = await r.json();
    } catch (e) { bkView = null; $("bkfiles").textContent = "Could not look: " + e.message; return; }
    paintBackup();
  }
  function bkWhen(t) {
    return t ? new Date(t * 1000).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "date unknown";
  }
  function paintBackup() {
    const v = bkView; if (!v) return;
    const signedIn = auth.local || auth.authorised;
    $("bksecrets").classList.toggle("on", bkSecrets);
    $("bksecrets").setAttribute("aria-pressed", String(bkSecrets));
    // Passwords leave the box only from its own screen (the daemon refuses
    // otherwise), so a remote browser is not offered the choice at all.
    $("bksecrets").disabled = !auth.local;
    $("bksecwarn").hidden = !bkSecrets;
    $("bkusb").innerHTML = v.drives.length
      ? v.drives.map((d) => `<button class="chip" data-bksave="${esc(d)}"${signedIn && !bkBusy ? "" : " disabled"}>Back up to USB: ${esc(d)}</button>`).join("")
      : `<span class="dim">Plug in a USB stick to back up onto it.</span>`;
    // Downloading and uploading are for a browser on another computer: on
    // the box's own screen there is nowhere for a file to go or come from.
    $("bkdlrow").hidden = auth.local;
    $("bkuprow").hidden = auth.local;
    $("bkdl").classList.toggle("off", !signedIn);
    $("bkuplabel").classList.toggle("off", !signedIn);
    $("bkupload").disabled = !signedIn;
    $("bkfiles").className = v.files.length ? "" : "dim";
    $("bkfiles").innerHTML = v.files.length
      ? v.files.map((f) => `<button class="chip" data-bkdrive="${esc(f.drive)}" data-bkpath="${esc(f.path)}"${signedIn && !bkBusy ? "" : " disabled"}>`
          + `${esc(f.drive)}: ${esc(f.path)} <span class="dim">· ${esc(bkWhen(f.mtime))}</span></button>`).join("")
      : (v.drives.length ? "No backups on the USB drive. They are found at the top of a drive and in its OpenHamShack folder."
                         : "Plug in a USB stick with a backup on it.");
    paintHeld();
  }
  function paintHeld() {
    const s = bkHeld;
    $("bkshow").hidden = !s;
    if (!s) return;
    const n = (k, one, many) => `${s[k]} ${s[k] === 1 ? one : many}`;
    const onAir = bkView && bkView.onAir;
    $("bkshow").innerHTML = `<div class="bkhead">${esc(s.callsign)} · ${esc(bkWhen(s.created))}</div>`
      + `<div>OpenHamShack ${esc(s.version)} · ${n("settings", "setting", "settings")} · ${n("qsos", "log entry", "log entries")}`
      + ` · ${n("images", "overlay picture", "overlay pictures")} · ${n("testCards", "test card", "test cards")} · passwords and keys ${s.secrets.length ? "<b>included</b>" : "not included"}</div>`
      + (s.newer ? `<div class="warnbox">Made by a newer OpenHamShack (${esc(s.version)}; this box runs ${esc(s.thisVersion)}).`
          + ` What this version does not know is skipped.</div>` : "")
      + (s.notes.length ? `<ul>${s.notes.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : "")
      + `<div class="field"><span>Log</span><div class="chips">`
      + `<button class="chip${bkReplace ? "" : " on"}" data-bklog="merge">Add to the log</button>`
      + `<button class="chip${bkReplace ? " on" : ""}" data-bklog="replace">Replace the log</button></div></div>`
      + `<div class="expl">${bkReplace ? "The log on this box is replaced by the backup's." :
          "Contacts not on this box are added; none are lost or doubled."}</div>`
      + `<div class="chips"><button class="chip${bkArmed ? " armed" : ""}" id="bkgo"${onAir || bkBusy ? " disabled" : ""}>`
      + `${bkArmed ? "Tap again to restore" : "Restore"}</button>`
      + `<button class="chip" id="bkcancel">Close</button>`
      + (onAir ? `<span class="dim" style="align-self:center">Stop transmitting first.</span>` : "") + `</div>`;
  }
  function bkSay(id, text, ok) { $(id).textContent = text; $(id).classList.toggle("ok", !!ok); }
  $("bksecrets").onclick = () => { bkSecrets = !bkSecrets; paintBackup(); };
  $("bkdl").onclick = (e) => { if ($("bkdl").classList.contains("off")) e.preventDefault(); };
  $("bkusb").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-bksave]"); if (!b || bkBusy) return;
    bkBusy = true; paintBackup(); bkSay("bkmsg", "Saving…", true);
    try {
      const r = await fetch("/api/backup/save", { method: "POST", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify({ drive: b.dataset.bksave, secrets: bkSecrets }) });
      const j = await r.json().catch(() => ({}));
      if (r.ok) bkSay("bkmsg", `Saved ${j.path} to ${j.drive}: ${j.qsos} log ${j.qsos === 1 ? "entry" : "entries"},`
        + ` ${j.images} overlay ${j.images === 1 ? "picture" : "pictures"}, ${j.testCards} test ${j.testCards === 1 ? "card" : "cards"}`
        + (j.includesSecrets ? ", with passwords and keys." : ".") + (j.notes.length ? " " + j.notes.join(" ") : ""), true);
      else bkSay("bkmsg", j.error || "Could not save");
    } catch (err) { bkSay("bkmsg", "Could not save: " + err.message); }
    bkBusy = false; loadBackup();
  });
  async function bkOpen(req) {
    bkBusy = true; bkHeld = null; bkArmed = null; bkReplace = false; paintBackup(); bkSay("bkrmsg", "Reading…", true);
    try {
      const r = await fetch(req.url, req.init);
      const j = await r.json().catch(() => ({}));
      if (r.ok) { bkHeld = j; bkSay("bkrmsg", ""); }
      else bkSay("bkrmsg", j.error || "Could not read it");
    } catch (err) { bkSay("bkrmsg", "Could not read it: " + err.message); }
    bkBusy = false; paintBackup();
    // Below a list of files, the card can open off the bottom of the screen.
    if (bkHeld && $("bkshow").scrollIntoView) $("bkshow").scrollIntoView({ block: "nearest" });
  }
  $("bkfiles").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-bkpath]"); if (!b || bkBusy) return;
    bkOpen({ url: "/api/backup/inspect", init: { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drive: b.dataset.bkdrive, path: b.dataset.bkpath }) } });
  });
  $("bkupload").onchange = () => {
    const f = $("bkupload").files[0]; $("bkupload").value = "";
    if (!f) return;
    if (bkView && f.size > bkView.maxBytes) return bkSay("bkrmsg", "That file is too big to be a OpenHamShack backup.");
    bkOpen({ url: "/api/backup/upload", init: { method: "POST", headers: { "Content-Type": "application/json" }, body: f } });
  };
  $("bkshow").addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b || !bkHeld) return;
    if (b.dataset.bklog) { bkReplace = b.dataset.bklog === "replace"; clearTimeout(bkArmed); bkArmed = null; return paintHeld(); }
    if (b.id === "bkcancel") { clearTimeout(bkArmed); bkArmed = null; bkHeld = null; bkSay("bkrmsg", ""); return paintHeld(); }
    if (b.id !== "bkgo" || bkBusy) return;
    if (!bkArmed) { bkArmed = setTimeout(() => { bkArmed = null; paintHeld(); }, 5000); return paintHeld(); }
    clearTimeout(bkArmed); bkArmed = null; bkBusy = true; paintHeld(); bkSay("bkrmsg", "Restoring…", true);
    try {
      const r = await fetch("/api/backup/restore", { method: "POST", headers: { "Content-Type": "application/json" },
                                                     body: JSON.stringify({ id: bkHeld.id, replaceLog: bkReplace }) });
      const j = await r.json().catch(() => ({}));
      if (r.ok) {
        const L = j.log;
        bkHeld = null;
        bkSay("bkrmsg", `Restored ${j.settings} settings, ${j.images} overlay ${j.images === 1 ? "picture" : "pictures"}`
          + ` and ${j.testCards} test ${j.testCards === 1 ? "card" : "cards"}. Log: ${L.added} added`
          + (L.updated ? `, ${L.updated} updated` : "") + (L.removed ? `, ${L.removed} replaced` : "") + `, ${L.total} in all.`
          + (j.notes.length ? " " + j.notes.join(" ") : ""), true);
        refresh();
      } else bkSay("bkrmsg", j.error || "Could not restore");
    } catch (err) { bkSay("bkrmsg", "Could not restore: " + err.message); }
    bkBusy = false; loadBackup();
  });
