  // Video file picker. The row under Picture shows what the file source will
  // play, and why it cannot if it cannot (a USB drive pulled); the modal lists
  // /api/media. Uploads send the File itself as the body with XMLHttpRequest:
  // fetch() cannot report upload progress, and a film is gigabytes.
  let media = null, mediaList = null, mediaDelArmed = null, mediaXhr = null;
  const bytes = (n) => n >= 1e9 ? (n / 1e9).toFixed(1) + " GB" : Math.max(0.1, n / 1e6).toFixed(1) + " MB";
  const clock = (s) => { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
                         return (h ? h + ":" + p2(m) : m) + ":" + p2(s % 60); };
  function paintMedia() {
    const show = cfg.source === "file";
    $("mediarow").style.display = show ? "" : "none";
    $("mediaprobrow").style.display = show && media && media.problem ? "" : "none";
    if (!media) return;
    $("mediaprob").textContent = media.problem || "";
    $("mediaopen").textContent = media.name
      ? `${media.chosen ? "" : "First video: "}${media.name} · ${media.location}  ▸` : "Choose a video…";
  }
  // Camera source. The chips are the cameras and sound cards there are now,
  // so plugging one in or pulling it out shows within a second (the status
  // poll). A tap is shown at once from cfg; after that the daemon's own
  // "selected" decides, since it knows which camera an old /dev/video0 or
  // an automatic choice is.
  function camBus(bus) {
    const usb = /^usb-[^-]+-(.+)$/.exec(bus || "");
    return usb ? `USB ${usb[1]}` : /^platform/.test(bus || "") ? "built in" : (bus || "").split(":")[0];
  }
  function paintCamera() {
    const show = cfg.source === "webcam" && !!camera;
    for (const id of ["camrow", "camsndrow"]) $(id).style.display = show ? "" : "none";
    $("camprobrow").style.display = show && camera.problem ? "" : "none";
    $("camsndprobrow").style.display = show && camera.soundProblem ? "" : "none";
    if (!show) return;
    $("camprob").textContent = camera.problem;
    $("camsndprob").textContent = camera.soundProblem;
    const fresh = Date.now() - editing < 1500;
    const cams = camera.cameras.map((c) => ({ ...c, on: fresh ? cfg.videoDevice === c.id || cfg.videoDevice === c.node : c.selected }));
    const want = cfg.audioDevice || "auto";
    const snd = [{ device: "auto", name: "Same as camera", sub: camera.paired || (camera.problem ? "" : "this camera has none: silence") }]
      .concat(camera.sound.map((s) => ({ ...s, sub: "" })), [{ device: "none", name: "None (silent)", sub: "" }]);
    const sig = JSON.stringify([cams, snd, want]);
    if ($("camchips").dataset.sig === sig) return;   // never rebuild a chip under a finger for nothing
    $("camchips").dataset.sig = sig;
    $("camchips").innerHTML = cams.map((c) => `<button class="chip${c.on ? " on" : ""}" data-cam="${esc(c.id)}">`
      + `${esc(c.name)}<small>${esc(camBus(c.bus))}${camera.auto && c.on ? " · chosen automatically" : ""}</small></button>`).join("")
      || `<span class="dim">None found</span>`;
    $("camsnd").innerHTML = snd.map((s) => `<button class="chip${s.device === want ? " on" : ""}" data-snd="${esc(s.device)}">`
      + `${esc(s.name)}${s.sub ? `<small>${esc(s.sub)}</small>` : ""}</button>`).join("");
  }
  // Stream (OBS) source. What the box is listening for, what arrives, and
  // what to type into OBS for each address the box has. Port and passphrase
  // are saved with their own request rather than save(): the daemon refuses
  // a port OpenHamShack uses itself or a passphrase SRT would not take, and that
  // must be said here rather than the chip quietly showing a value that was
  // never stored.
  let stream = null, streamMsg = null;
  const CODECS = { h264: "H.264", hevc: "H.265", aac: "AAC", mp2: "MP2", mp3: "MP3", opus: "Opus", ac3: "AC-3" };
  function streamArrives(st) {
    const v = st.video, a = st.audio;
    const parts = [];
    if (v) parts.push([v.width && v.height ? `${v.width}×${v.height}` : "", CODECS[v.codec] || v.codec,
                       v.fps ? `${+v.fps.toFixed(2)} fps` : ""].filter(Boolean).join(" "));
    parts.push(a ? [CODECS[a.codec] || a.codec, a.rate ? `${a.rate / 1000} kHz` : "", a.channels || ""].filter(Boolean).join(" ")
                 : "no sound");
    if (st.kbps) parts.push(`${(st.kbps / 1000).toFixed(1)} Mb/s`);
    return parts.join(" · ");
  }
  function paintStream() {
    const show = cfg.source === "stream";
    for (const id of ["streamrow", "streaminforow", "larixrow"]) $(id).style.display = show ? "" : "none";
    $("streamlatrow").style.display = show && (cfg.streamProtocol || "srt") === "srt" ? "" : "none";
    if (!show || !stream) return;
    const st = stream, proto = st.protocol;
    $("streamport").textContent = `Port ${st.port}`;
    $("streamsecret").textContent = proto === "udp" ? "No passphrase over UDP"
      : (st.secretSet ? (proto === "rtmp" ? "Stream key set ▸" : "Passphrase set ▸") : (proto === "rtmp" ? "Stream key…" : "Passphrase…"));
    $("streamsecret").disabled = proto === "udp";
    const s = $("streamstate");
    if (st.state === "receiving") {
      s.textContent = (st.onAir === "card" ? "Receiving (on air: the stream-lost card until it settles) · " : st.onAir === "stream" ? "On air · " : "Receiving · ")
        + streamArrives(st);
      s.className = "rx";
    } else if (st.state === "error") {
      s.textContent = st.message; s.className = "bad";
    } else {
      s.textContent = st.onAir === "card" ? "Stream lost: the card is on air" : "Listening: nothing connected yet";
      s.className = st.onAir === "card" ? "bad" : "";
    }
    const note = streamMsg && Date.now() < streamMsg.until ? streamMsg : st.note ? { text: st.note, err: true } : null;
    $("streamnote").textContent = note ? note.text : "";
    $("streamnote").className = note && note.err ? "err" : "dim";
    const urls = st.urls || [];
    const code = (t) => `<code>${esc(t)}</code>`;
    let h = "";
    if (!urls.length) h = "This box has no network address yet.";
    else if (proto === "srt") {
      h = "<div>In OBS: Settings → Stream → Service <b>Custom…</b>, Server:</div>"
        + urls.map((u) => `<div>${code(u.server + (st.secretSet ? "?passphrase=" : ""))}${st.secretSet ? "<i>your passphrase</i>" : ""}`
                         + ` (${esc(u.via)})</div>`).join("")
        + "<div>Stream Key: leave empty.</div>";
    } else if (proto === "rtmp") {
      h = "<div>In OBS: Settings → Stream → Service <b>Custom…</b>, Server:</div>"
        + urls.map((u) => `<div>${code(u.server)} (${esc(u.via)})</div>`).join("")
        + `<div>Stream Key: ${st.secretSet ? "the key set here" : "anything (no key set)"}.</div>`;
    } else {
      h = "<div>Send MPEG-TS (vMix, ffmpeg, or OBS's Custom Output (FFmpeg) recording) to:</div>"
        + urls.map((u) => `<div>${code(u.server)} (${esc(u.via)})</div>`).join("")
        + "<div>Anyone on the network can send to it: UDP has no passphrase.</div>";
    }
    $("streamobs").innerHTML = h;
    paintLarix();
  }

  // Larix on a phone: a QR code per address, read from the daemon only
  // while the Stream source shows and its settings change (or half a
  // minute has passed: an address may have come or gone).
  let larix = null, larixSig = "", larixAt = 0, larixVia = 0;
  async function loadLarix() {
    larixAt = Date.now();
    try { larix = await (await fetch("/api/stream/larix")).json(); } catch (e) { return; }
    larixSig = "";
    paintLarix();
  }
  function paintLarix() {
    const show = cfg.source === "stream" && !!stream && stream.protocol !== "udp";
    $("larixrow").style.display = show ? "" : "none";
    if (cfg.source === "stream" && stream && stream.protocol === "udp") {
      $("larixrow").style.display = "";
      $("larixqr").innerHTML = "";
      $("larixvia").innerHTML = "";
      $("larixaddr").textContent = "";
      $("larixnote").textContent = "Larix sends SRT or RTMP, not UDP: choose one of those for a phone.";
      return;
    }
    if (!show) return;
    const want = JSON.stringify([stream.protocol, stream.port, stream.secretSet, stream.urls, auth.authorised]);
    if (want !== $("larixrow").dataset.want || Date.now() - larixAt > 30000) {
      $("larixrow").dataset.want = want;
      return loadLarix();
    }
    if (!larix) return;
    const links = larix.links || [];
    larixVia = Math.min(larixVia, Math.max(0, links.length - 1));
    const sig = JSON.stringify([links, larixVia]);
    if (sig === larixSig) return;
    larixSig = sig;
    const l = links[larixVia];
    $("larixqr").innerHTML = l ? QR.svg(l.larix, { ecl: "M" }) : "";
    $("larixvia").innerHTML = links.length > 1 ? links.map((x, i) =>
      `<button class="chip ro${i === larixVia ? " on" : ""}" data-larixvia="${i}">${esc(x.via)}</button>`).join("") : "";
    $("larixaddr").textContent = l ? l.server : "This box has no network address yet.";
    $("larixnote").textContent = larix.secretSet && !larix.secretIncluded
      ? "The passphrase is not in this code: type it into the connection in Larix, or open this console with the PIN."
      : larix.secretIncluded ? "The code holds the passphrase: show it only to a phone you trust." : "";
  }
  $("larixvia").addEventListener("click", (e) => {
    const b = e.target.closest("[data-larixvia]"); if (!b) return;
    larixVia = Number(b.dataset.larixvia); paintLarix();
  });
  async function streamSave(patch, okText) {
    if (!auth.authorised) { streamMsg = { text: "Remote: view only. Enter the PIN first.", err: true, until: Date.now() + 5000 }; return paintStream(); }
    const r = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch) }).catch(() => null);
    const j = r ? await r.json().catch(() => ({})) : {};
    streamMsg = !r ? { text: "OpenHamShack did not answer", err: true } : !r.ok ? { text: j.error || "Not saved", err: true } : { text: okText, err: false };
    streamMsg.until = Date.now() + 6000;
    refresh();
  }
  $("streamport").onclick = () => openPad("streamPort", "Listen port (0: the usual one for the protocol)", "num",
    (v) => streamSave({ streamPort: Number(v) || 0 }, "Port saved: the listener restarts on it"));
  $("streamsecret").onclick = () => Keyboard.open({
    title: (cfg.streamProtocol === "rtmp" ? "RTMP stream key" : "SRT passphrase")
      + ": 10 to 79 letters, digits, . _ - (OK on an empty one removes it)",
    mask: true, maxLength: 79,
    onOk: (v) => streamSave({ streamPassphrase: v.trim() }, v.trim() ? "Saved. It is not shown again: put the same in OBS." : "Removed: anyone on the network may send a picture"),
  });
  function mediaMsg(text, err) { $("mediamsg").textContent = text || ""; $("mediamsg").className = err ? "err" : "dim"; }
  async function loadMedia() {
    try { mediaList = await (await fetch("/api/media")).json(); paintMediaList(); }
    catch (e) { mediaMsg("OpenHamShack did not answer", true); }
  }
  function paintMediaList() {
    const L = mediaList; if (!L) return;
    $("medialist").innerHTML = L.files.map((f) => {
      const facts = [f.location + (f.folder ? " / " + f.folder : ""), bytes(f.size),
                     f.duration != null ? clock(f.duration) : "", f.width ? `${f.width}×${f.height}` : ""].filter(Boolean).join(" · ");
      const armed = mediaDelArmed === f.path;
      return `<div class="mrow${f.selected ? " sel" : ""}"><div><b>${esc(f.name)}</b><div class="dim">${esc(facts)}</div></div>`
        + (f.selected ? `<span class="inuse">✓ In use</span>` : `<button class="chip" data-muse="${esc(f.path)}">Use</button>`)
        + (f.internal ? `<button class="chip${armed ? " armed" : ""}" data-mdel="${esc(f.path)}">${armed ? "Tap again to delete" : "Delete"}</button>` : "")
        + `</div>`;
    }).join("") || `<div class="dim">No videos yet: upload one, or plug in a USB drive with films on it.</div>`;
    $("mediaspace").textContent = (L.maxUpload != null ? `Room for ${bytes(L.maxUpload)} (${bytes(L.margin)} is kept free for recordings).` : "")
      + (L.truncated ? " Not every folder on the USB drive was searched: keep films near the top of the drive." : "");
  }
  async function mediaPost(path, body) {
    if (!auth.authorised) { mediaMsg("Remote: view only. Enter the PIN first.", true); return null; }
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).catch(() => null);
    if (!r) { mediaMsg("OpenHamShack did not answer", true); return null; }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { mediaMsg(j.error || "Refused", true); return null; }
    return j;
  }
  $("mediaopen").onclick = () => { mediaMsg(""); $("media").classList.add("open"); paintMediaList(); loadMedia(); };
  $("mediaclose").onclick = () => $("media").classList.remove("open");
  $("medialist").addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.muse !== undefined) {
      if (await mediaPost("/api/config", { videoFile: b.dataset.muse })) { mediaMsg(""); refresh(); loadMedia(); }
      return;
    }
    if (b.dataset.mdel === undefined) return;
    const path = b.dataset.mdel;
    if (mediaDelArmed !== path) {
      mediaDelArmed = path; paintMediaList();
      setTimeout(() => { if (mediaDelArmed === path) { mediaDelArmed = null; paintMediaList(); } }, 5000);
      return;
    }
    mediaDelArmed = null; b.disabled = true; b.textContent = "Deleting…";
    const j = await mediaPost("/api/media/delete", { path });
    if (j) mediaMsg("Deleted " + j.name);
    refresh(); loadMedia();
  });
  function paintUpload(f, sent) {
    $("mediaupl").style.display = f ? "grid" : "none";
    $("mediapick").style.display = f ? "none" : "inline-flex";
    if (!f) return;
    const pct = f.size ? sent / f.size * 100 : 100;
    $("mediaupltext").textContent = `Uploading ${f.name}: ${bytes(sent)} of ${bytes(f.size)} (${Math.floor(pct)}%)`;
    $("mediabar").firstChild.style.width = pct.toFixed(1) + "%";
  }
  $("mediafile").onchange = (ev) => {
    const f = ev.target.files[0]; ev.target.value = "";
    if (!f) return;
    if (!auth.authorised) return mediaMsg("Remote: view only. Enter the PIN first.", true);
    // Checked here as well as by the daemon: refused there, a browser sees the
    // connection close mid-send rather than the reason.
    if (mediaList && mediaList.maxUpload != null && f.size > mediaList.maxUpload)
      return mediaMsg(`${f.name} is ${bytes(f.size)}; there is room for ${bytes(mediaList.maxUpload)}.`, true);
    const xhr = new XMLHttpRequest();
    mediaXhr = xhr;
    const done = (text, err) => { mediaXhr = null; paintUpload(null); mediaMsg(text, err); loadMedia(); };
    xhr.open("POST", "/api/media/upload?name=" + encodeURIComponent(f.name));
    xhr.upload.onprogress = (p) => paintUpload(f, p.loaded);
    xhr.onload = () => {
      let j = {}; try { j = JSON.parse(xhr.responseText || "{}"); } catch (e) { /* not JSON */ }
      if (xhr.status === 200) done(`Uploaded ${j.name}. Tap Use to play it.`);
      else done(j.error || `Upload refused (${xhr.status})`, true);
    };
    xhr.onerror = () => done("The upload was cut off; nothing was kept.", true);
    xhr.onabort = () => done("Upload cancelled; nothing was kept.");
    mediaMsg("");
    paintUpload(f, 0);
    xhr.send(f);
  };
  $("mediacancel").onclick = () => { if (mediaXhr) mediaXhr.abort(); };
  // Leaving the page would cut the upload off; ask first.
  window.addEventListener("beforeunload", (e) => { if (mediaXhr) { e.preventDefault(); e.returnValue = ""; } });

  // Test card. The chips are status.testcard's cards, each with a thumbnail
  // the daemon draws from the card itself; the URL carries what the picture
  // depends on (callsign and locator for a built-in card, the file's size
  // for a picture), so the browser's cached copy is never a stale one and a
  // poll does not fetch them again. Rebuilt only when the list changes:
  // never under a finger for nothing.
  let tcView = null, tcDelArmed = null, tcBusy = false;
  const tcThumb = (c) => "/api/testcards/thumb?card=" + encodeURIComponent(c.id) + "&v="
    + encodeURIComponent(c.builtin ? `${cfg.callsign || ""}|${cfg.locator || ""}` : c.bytes);
  function paintTestcard() {
    const show = cfg.source === "testcard" && !!testcard;
    for (const id of ["tcrow", "tcsndrow"]) $(id).style.display = show ? "" : "none";
    $("tcprobrow").style.display = show && testcard.problem ? "" : "none";
    if (!show) return;
    $("tcprob").textContent = testcard.problem;
    const fresh = Date.now() - editing < 1500;
    const want = fresh && cfg.testCard ? cfg.testCard : testcard.card;
    const sig = JSON.stringify([testcard.cards, want, cfg.callsign, cfg.locator]);
    if ($("tcchips").dataset.sig === sig) return;
    $("tcchips").dataset.sig = sig;
    $("tcchips").innerHTML = testcard.cards.map((c) => `<button class="chip${c.id === want ? " on" : ""}" data-tc="${esc(c.id)}">`
      + `<img src="${tcThumb(c)}" alt=""><span>${esc(c.builtin ? c.name : c.name.replace(/\.[^.]+$/, ""))}</span></button>`).join("");
  }
  function tcMsg(text, err) { $("tcmsg").textContent = text || ""; $("tcmsg").className = err ? "err" : "dim"; }
  async function loadTc() {
    try { tcView = await (await fetch("/api/testcards")).json(); paintTcList(); }
    catch (e) { tcMsg("OpenHamShack did not answer", true); }
  }
  const tcSize = (u) => [u.width ? `${u.width}×${u.height}` : "", bytes(u.bytes)].filter(Boolean).join(" · ");
  function paintTcList() {
    const V = tcView; if (!V) return;
    const pics = V.cards.filter((c) => !c.builtin);
    $("tclist").innerHTML = pics.map((c) => {
      const on = c.id === V.card, armed = tcDelArmed === c.name;
      return `<div class="mrow tcrow${on ? " sel" : ""}"><img src="${tcThumb(c)}" alt=""><div><b>${esc(c.name)}</b>`
        + `<div class="dim">${esc(bytes(c.bytes))}</div></div>`
        + (on ? `<span class="inuse">✓ In use</span>` : `<button class="chip" data-tcuse="${esc(c.id)}">Use</button>`)
        + `<button class="chip${armed ? " armed" : ""}" data-tcdel="${esc(c.name)}">${armed ? "Tap again to delete" : "Delete"}</button></div>`;
    }).join("") || `<div class="dim">No pictures of your own yet: upload one, or plug in a USB stick with one on it.</div>`;
    $("tcroom").textContent = `${pics.length} of ${V.max} pictures`;
    $("tcusb").innerHTML = V.usb.map((u) => `<div class="mrow"><div><b>${esc(u.name)}</b>`
      + `<div class="dim">${esc([u.location + (u.folder ? " / " + u.folder : ""), tcSize(u)].join(" · "))}</div></div>`
      + `<button class="chip" data-tcadd="${esc(u.path)}"${tcBusy ? " disabled" : ""}>Add</button></div>`).join("")
      || `<div class="dim">${V.truncated ? "" : "No PNG or JPEG pictures on a USB stick."}</div>`;
    if (V.truncated) $("tcusb").insertAdjacentHTML("beforeend", `<div class="dim">Not every folder on the stick was searched: keep pictures near the top.</div>`);
  }
  async function tcPost(path, body) {
    if (!auth.authorised) { tcMsg("Remote: view only. Enter the PIN first.", true); return null; }
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body }).catch(() => null);
    if (!r) { tcMsg("OpenHamShack did not answer", true); return null; }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { tcMsg(j.error || "Refused", true); return null; }
    return j;
  }
  $("tcopen").onclick = () => { tcMsg(""); $("tcm").classList.add("open"); paintTcList(); loadTc(); };
  $("tcclose").onclick = () => $("tcm").classList.remove("open");
  $("tcm").addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.tcuse) {
      if (await tcPost("/api/config", JSON.stringify({ testCard: b.dataset.tcuse }))) { tcMsg(""); refresh(); loadTc(); }
      return;
    }
    if (b.dataset.tcadd) {
      tcBusy = true; paintTcList(); tcMsg("Checking the picture…");
      const j = await tcPost("/api/testcards/add", JSON.stringify({ path: b.dataset.tcadd }));
      tcBusy = false;
      if (j) tcMsg(`Added ${j.name}${j.scaled ? ", kept at 1920×1080 at most" : ""}. Tap Use to send it.`);
      refresh(); loadTc();
      return;
    }
    if (!b.dataset.tcdel) return;
    const name = b.dataset.tcdel;
    if (tcDelArmed !== name) {
      tcDelArmed = name; paintTcList();
      setTimeout(() => { if (tcDelArmed === name) { tcDelArmed = null; paintTcList(); } }, 5000);
      return;
    }
    tcDelArmed = null; b.disabled = true; b.textContent = "Deleting…";
    const j = await tcPost("/api/testcards/delete", JSON.stringify({ name }));
    if (j) tcMsg("Deleted " + j.name);
    refresh(); loadTc();
  });
  $("tcfile").onchange = async (ev) => {
    const f = ev.target.files[0]; ev.target.value = "";
    if (!f) return;
    if (!auth.authorised) return tcMsg("Remote: view only. Enter the PIN first.", true);
    // Checked here as well as by the daemon: refused there unread, a browser
    // sees the connection close rather than the reason.
    if (tcView && f.size > tcView.maxBytes) return tcMsg(`${f.name} is ${bytes(f.size)}; pictures are limited to ${bytes(tcView.maxBytes)}.`, true);
    tcMsg(`Uploading ${f.name}…`);
    const r = await fetch("/api/testcards/upload?name=" + encodeURIComponent(f.name), { method: "POST", body: f }).catch(() => null);
    const j = r ? await r.json().catch(() => ({})) : {};
    if (!r) tcMsg("OpenHamShack did not answer", true);
    else if (!r.ok) tcMsg(j.error || `Upload refused (${r.status})`, true);
    else tcMsg(`Uploaded ${j.name}${j.scaled ? ", kept at 1920×1080 at most" : ""}. Tap Use to send it.`);
    refresh(); loadTc();
  };
