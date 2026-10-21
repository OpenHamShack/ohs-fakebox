  // Diagnostics. The first look collects (a second or two: vainfo, a ping,
  // the journal); the daemon keeps the result for 30 s, Refresh asks again.
  // Save, Download and Send all take a fresh report on the box.
  // The note lives in this page only until it goes with a report: it is
  // the tester's words about now, not a setting.
  let diagView = null, diagSendArmed = null, diagBusy = false, diagNote = "";
  const DIAGLEVEL = { error: "Problem", warn: "Warning", info: "Note" };
  async function loadDiag(refresh) {
    if (refresh || !diagView) $("diagprobs").textContent = "Checking…";
    try {
      const r = await fetch("/api/diagnostics" + (refresh ? "?refresh=1" : ""));
      if (!r.ok) throw new Error("HTTP " + r.status);
      diagView = await r.json();
    } catch (e) {
      $("diagprobs").textContent = "Could not check: " + e.message;
      return;
    }
    paintDiag();
  }
  function paintDiag() {
    const v = diagView; if (!v) return;
    const signedIn = auth.local || auth.authorised;
    $("diagprobs").className = v.problems.length ? "" : "dim";
    $("diagprobs").innerHTML = v.problems.length
      ? `<div class="dim">Problems found:</div>` + v.problems.map((p) =>
          `<div class="diagp ${esc(p.level)}"><b>${DIAGLEVEL[p.level] || esc(p.level)}</b><span>${esc(p.text)}</span></div>`).join("")
      : "No problems found";
    $("diagradio").textContent = v.summary && v.summary.radio ? "Radio: " + v.summary.radio : "";
    $("diagusb").innerHTML = v.drives.length
      ? v.drives.map((d) => `<button class="chip" data-diagsave="${esc(d.name)}"${signedIn ? "" : " disabled"}>Save report to USB: ${esc(d.name)}</button>`).join("")
      : `<span class="dim">Plug in a USB stick to save the report onto it.</span>`;
    $("diagdl").classList.toggle("off", !signedIn);
    $("diagnote").disabled = !signedIn;
    paintNote();
    paintAuto(v.auto);
    $("diagsend").disabled = !signedIn || !v.sendTo || diagBusy;
    $("diagwhat").innerHTML = v.sendTo
      ? `Send posts hardware details, logs and settings with passwords removed, to <b>${esc(v.sendTo)}</b>.`
      : "Send needs an https update address (Updates).";
    $("diaghint").textContent = signedIn
      ? "Download report also works from a browser on another computer, once the remote PIN is entered there."
      : "Enter the remote PIN to save, download or send the report from this browser.";
    paintDiagSent(v.sent);
  }
  function paintDiagSent(sent) {
    if (!sent || !sent.ref) { $("diagref").innerHTML = ""; return; }
    const when = new Date(sent.at * 1000).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
    $("diagref").innerHTML = `Sent ${esc(when)}. Reference <b>${esc(sent.ref)}</b> — give this to whoever asked for it.`;
  }
  function paintNote() {
    $("diagnote").textContent = diagNote || "Add a note…";
    $("diagnote").classList.toggle("empty", !diagNote);
    $("diagnoteclear").hidden = !diagNote;
    $("diagdl").href = "/api/diagnostics/report" + (diagNote ? "?note=" + encodeURIComponent(diagNote) : "");
  }
  $("diagnote").onclick = () => Keyboard.open({
    title: "What happened? It goes at the top of the report", value: diagNote, symbols: true,
    maxLength: (diagView && diagView.noteMax) || 1000,
    onOk: (v) => { diagNote = v.trim(); paintNote(); } });
  $("diagnoteclear").onclick = () => { diagNote = ""; paintNote(); };
  function paintAuto(a) {
    if (!a) { $("autolast").innerHTML = ""; return; }
    const parts = [];
    if (a.last && a.last.ref) {
      const when = new Date(a.last.at * 1000).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
      parts.push(`Last automatic report: ${esc(a.last.trigger || "")}, ${esc(when)}. Reference <b>${esc(a.last.ref)}</b>`);
    }
    if (a.enabled && a.pending && a.pending.length) parts.push("Waiting to send: " + a.pending.map(esc).join("; ")
      + " - after transmitting, or once the box is back on the internet.");
    $("autolast").innerHTML = parts.join("<br>");
  }
  for (const b of document.querySelectorAll("#diagsec .whatbtn")) b.onclick = () => {
    const box = $(b.dataset.what), open = box.hidden;
    box.hidden = !open; b.setAttribute("aria-expanded", String(open));
  };
  function diagSay(text, ok) { $("diagmsg").textContent = text; $("diagmsg").classList.toggle("ok", !!ok); }
  $("diagrefresh").onclick = () => { diagSay(""); loadDiag(true); };
  $("diagusb").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-diagsave]"); if (!b || diagBusy) return;
    diagBusy = true; b.disabled = true; diagSay("Saving…", true);
    try {
      const r = await fetch("/api/diagnostics/save", { method: "POST", headers: { "Content-Type": "application/json" },
                                                       body: JSON.stringify({ drive: b.dataset.diagsave, note: diagNote }) });
      const j = await r.json().catch(() => ({}));
      if (r.ok) diagSay(`Saved ${j.path} to ${j.drive} — email it to whoever asked.`, true);
      else diagSay(j.error || "Could not save");
    } catch (e) { diagSay("Could not save: " + e.message); }
    diagBusy = false; b.disabled = false; paintDiag();
  });
  // Two taps within 4 s, like transmit and the updates: it goes off the box.
  $("diagsend").onclick = async () => {
    const b = $("diagsend");
    if (!diagSendArmed) {
      b.classList.add("armed"); b.textContent = "Tap again to send";
      diagSendArmed = setTimeout(() => { diagSendArmed = null; b.classList.remove("armed"); b.textContent = "Send to the developer"; }, 4000);
      return;
    }
    clearTimeout(diagSendArmed); diagSendArmed = null;
    b.classList.remove("armed"); b.textContent = "Sending…"; diagBusy = true; b.disabled = true; diagSay("");
    try {
      const r = await fetch("/api/diagnostics/send", { method: "POST", headers: { "Content-Type": "application/json" },
                                                       body: JSON.stringify({ note: diagNote }) });
      const j = await r.json().catch(() => ({}));
      if (r.ok) { if (diagView) diagView.sent = j.sent; paintDiagSent(j.sent); }
      else diagSay(j.error || "Could not send");
    } catch (e) { diagSay("Could not send: " + e.message); }
    diagBusy = false; b.textContent = "Send to the developer"; paintDiag();
  };
