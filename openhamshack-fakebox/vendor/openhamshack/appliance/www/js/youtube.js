  // YouTube Live panel. Its own endpoint (/api/youtube): the key can be set
  // from any signed-in browser but is never sent back, only whether it is
  // set and its last four characters.
  let yt = null, ytOpen = false;
  const ytPost = async (body) => {
    const r = await fetch("/api/youtube", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) { ytNote(j.error || "Not saved"); return null; }
    yt = j; paintYt(); return j;
  };
  const ytLabel = { off: "Off", waiting: "Waiting", live: "LIVE on YouTube", retrying: "Reconnecting", error: "Problem" };
  function dur(s) {
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
  }
  function paintYt() {
    if (!yt) return;
    const live = yt.state === "live";
    $("ytpill").classList.toggle("on", live);
    if (!ytOpen) return;
    const can = auth.authorised;
    $("ytstate").className = live ? "live" : yt.state === "error" ? "error" : "";
    $("ytstatelabel").textContent = (ytLabel[yt.state] || yt.state) + (live ? "  ·  " + dur(Date.now() / 1000 - yt.since) : "");
    $("ytmsg").textContent = [yt.message, live && yt.bitrate ? `${(yt.bitrate / 1000).toFixed(1)} Mb/s going out` : ""].filter(Boolean).join("  ·  ");
    for (const b of $("ytsource").children) { b.classList.toggle("on", b.dataset.ys === yt.source); b.disabled = !can; }
    for (const b of $("ytmode").children) { b.classList.toggle("on", b.dataset.ym === yt.mode); b.disabled = !can; }
    for (const b of $("yturl").children) { b.classList.toggle("on", b.dataset.yu === yt.url); b.disabled = !can; }
    for (const id of ["ytkey", "ytpaste", "ytsave"]) $(id).disabled = !can;
    if (document.activeElement !== $("ytkey") && !$("ytkey").value)
      $("ytkey").placeholder = yt.keySet ? `Key set · ends ${yt.keyHint || "…"}` : "xxxx-xxxx-xxxx-xxxx-xxxx";
    if (!$("ytkeynote").dataset.err) $("ytkeynote").textContent = !can ? "Sign in with the remote PIN to change these."
      : auth.local ? "The key is easiest pasted from a PC: open this OpenHamShack in a browser there, enter the remote PIN, then YouTube."
      : "From YouTube Studio → Go live → Stream key. It is stored on the box and never shown again.";
    const go = $("ytgo");
    go.disabled = !can || (!yt.enabled && !yt.keySet);
    go.className = yt.enabled ? "live" : "";
    go.textContent = !yt.keySet && !yt.enabled ? "Add a stream key to go live" : yt.enabled ? "Stop streaming to YouTube" : "Go live on YouTube";
  }
  async function loadYt() {
    try { yt = await (await fetch("/api/youtube")).json(); paintYt(); } catch (e) { /* daemon restarting */ }
  }
  function ytNote(text) { $("ytkeynote").dataset.err = text ? "1" : ""; $("ytkeynote").textContent = text; }
  $("ytbtn").onclick = () => { ytOpen = true; ytNote(""); $("ytm").classList.add("open"); loadYt(); };
  $("ytclose").onclick = () => { ytOpen = false; $("ytkey").value = ""; $("ytm").classList.remove("open"); };
  $("ytm").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.ys) ytPost({ source: b.dataset.ys });
    else if (b.dataset.ym) ytPost({ mode: b.dataset.ym });
    else if (b.dataset.yu) ytPost({ url: b.dataset.yu });
  });
  async function saveKey() {
    const key = $("ytkey").value.trim();
    if (!key) return ytNote("Paste the stream key first.");
    if (await ytPost({ key })) { $("ytkey").value = ""; ytNote("Key saved."); setTimeout(() => ytNote(""), 3000); }
    else $("ytkeynote").dataset.err = "1";
  }
  $("ytsave").onclick = saveKey;
  $("ytkey").addEventListener("keydown", (e) => { if (e.key === "Enter") saveKey(); });
  $("ytpaste").onclick = async () => {
    // The clipboard API exists only on https or localhost; over plain HTTP on
    // the LAN, Ctrl+V into the field is the way.
    try { $("ytkey").value = (await navigator.clipboard.readText()).trim(); $("ytkey").focus(); }
    catch (e) { $("ytkey").focus(); ytNote("This browser will not hand over the clipboard here: click the field and press Ctrl+V."); }
  };
  $("ytgo").onclick = () => ytPost({ enabled: !yt.enabled });

  loadYt();
  setInterval(() => { if (ytOpen || document.visibilityState === "visible") loadYt(); }, 2000);
