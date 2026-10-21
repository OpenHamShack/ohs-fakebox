  // Network. The summary rides on /api/status; Settings fetches /api/network
  // as well while it is open, for the saved Wi-Fi networks (root's to read,
  // so the daemon asks openhamshack-net). Changes go through the PIN gate.
  const NETTYPE = { wired: "Wired", wifi: "Wi-Fi", "usb-radio": "USB radio", tailscale: "Tailscale", other: "Other" };
  let net = null, netView = null, netBannerGone = "", netArmed = null, wifiIf = null, wifiNets = null, wifiWatch = null;
  try { netBannerGone = localStorage.getItem("lbNetBanner") || ""; } catch (e) { /* private window: shown again */ }
  const lanAddrs = (n) => (n ? n.interfaces : []).filter((i) => i.type === "wired" || i.type === "wifi")
    .flatMap((i) => i.ipv4.map((a) => ({ ...a, iface: i.name, type: i.type })));
  const consoleUrl = (host, n) => `http://${host}${n.port === 80 ? "" : ":" + n.port}`;
  const netSig = () => lanAddrs(net).map((a) => a.address).join(",") + "|" + (net.tailnet ? net.tailnet.ip : "");

  function paintNet(n) {
    if (!n) return;
    net = n;
    if (netView) netView = { ...netView, ...n };   // fresher addresses, saved networks kept
    paintNetBanner(); paintNetPending();
    if (setOpen) paintNetList();
  }

  // Shown on the box's own screen for the first minutes after boot and after
  // an address changes; a browser elsewhere already knows an address.
  function paintNetBanner() {
    const n = net, el = $("netbanner"), sig = netSig();
    const recent = n.uptime < n.bannerSeconds || (n.changedAgo != null && n.changedAgo < n.bannerSeconds);
    const show = auth.local && recent && sig !== netBannerGone;
    el.classList.toggle("on", show);
    el.classList.toggle("warn", !lanAddrs(n).length);   // no network is a problem, an address is news
    if (!show || el.dataset.sig === sig) return;
    el.dataset.sig = sig;
    const addrs = lanAddrs(n);
    el.innerHTML = addrs.length
      ? `<span>Console from another device:</span>`
        + addrs.map((a) => `<b>${esc(consoleUrl(a.address, n))}</b><span class="dim">${NETTYPE[a.type]}</span>`).join("")
        + (n.tailnet ? `<b>${esc(consoleUrl(n.tailnet.name || n.tailnet.ip, n))}</b>`
          + `<span class="dim">tailnet${n.tailnet.name ? " · " + esc(n.tailnet.ip) : ""}</span>` : "")
        + `<button class="chip ro" data-nb="ok">OK</button>`
      : `<span>No network yet. Plug in a cable, or join Wi-Fi in Settings → Network.</span>`
        + `<button class="chip" data-nb="set">Network…</button><button class="chip ro" data-nb="ok">Dismiss</button>`;
  }
  $("netbanner").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-nb]"); if (!b) return;
    netBannerGone = netSig();
    try { localStorage.setItem("lbNetBanner", netBannerGone); } catch (err) { /* this page only */ }
    $("netbanner").classList.remove("on");
    if (b.dataset.nb === "set") showSettings(true, "network");
  });

  function paintNetPending() {
    const p = net.pending, el = $("netpend");
    el.classList.toggle("on", !!p);
    if (!p) { el.dataset.sig = ""; return; }
    const there = location.hostname === p.address;
    const url = consoleUrl(p.address, net);
    const sig = p.iface + p.address + there;
    if (el.dataset.sig !== sig) {
      el.dataset.sig = sig;
      el.innerHTML = `<span><b>${esc(p.iface)}</b> is now <b>${esc(p.address)}</b>. Keep it?
          Otherwise it goes back in <b id="netpendleft"></b> s.</span>`
        + (!auth.local && !there ? `<span>Check it works: open <a href="${esc(url)}">${esc(url)}</a> and tap Keep there.</span>` : "")
        + `<div class="chips"><button class="chip on" data-np="confirm">Keep</button><button class="chip" data-np="revert">Undo</button></div>`;
    }
    $("netpendleft").textContent = p.secondsLeft;
  }
  $("netpend").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-np]"); if (!b) return;
    b.disabled = true;
    await netPost("/api/network/" + b.dataset.np, {}, (m) => { $("netmsg").textContent = m; });
    refresh(); if (setOpen) loadNet();
  });

  async function netPost(path, body, onErr) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .catch(() => null);
    if (!r) { onErr("OpenHamShack did not answer"); return null; }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { onErr(j.error || "Refused"); return null; }
    onErr("");
    return j;
  }

  async function loadNet() {
    try { netView = await (await fetch("/api/network")).json(); } catch (e) { return; }
    net = { ...(net || {}), ...netView };
    paintNetList();
    if (wifiIf) paintWifi();
  }
  const WPA = { COMPLETED: "connected", SCANNING: "looking for a saved network", ASSOCIATING: "connecting…",
                AUTHENTICATING: "connecting…", "4WAY_HANDSHAKE": "checking the passphrase…", GROUP_HANDSHAKE: "connecting…",
                DISCONNECTED: "not connected", INACTIVE: "no saved network in range", INTERFACE_DISABLED: "switched off" };
  function wifiLine(w) {
    if (!w) return "";
    if (w.error) return esc(w.error);
    const st = w.state || {}, rf = w.rfkill || {};
    // Before these, a card nobody had scanned on yet read "Wi-Fi off",
    // exactly like one switched off - Ray's "no wifi" could have been either.
    if (rf.hard) return `<span class="err">Switched off by the laptop (wireless switch, Fn key or BIOS)</span>`;
    if (rf.soft) return "Switched off in software: Join a network… switches it on";
    if (w.running === false) return "Not started yet: Join a network… looks for networks";
    const refused = (w.saved || []).filter((s) => s.disabled).map((s) => s.ssid);
    return (st.wpa_state === "COMPLETED" && st.ssid ? `on ${esc(st.ssid)}` : WPA[st.wpa_state] || (st.wpa_state ? esc(st.wpa_state) : "Wi-Fi off"))
      + (refused.length ? ` · <span class="err">${refused.map(esc).join(", ")} refused: check the passphrase</span>` : "");
  }
  function paintNetList() {
    const n = netView || net; if (!n) return;
    const here = location.hostname;
    const rows = n.interfaces.map((i) => {
      const w = netView && netView.wifi ? netView.wifi[i.name] : null;
      const addr = i.ipv4.map((a) => `${a.address}/${a.prefix}`).join(", ");
      const dyn = i.ipv4.length ? i.ipv4.every((a) => a.dynamic) : null;
      const cls = i.type === "usb-radio" ? " radio" : !i.up ? " down" : "";
      let out = `<div class="netif${cls}"><div class="nethead"><b>${NETTYPE[i.type] || esc(i.type)}</b>`
        + `<span class="dim">${esc(i.name)} · ${i.up ? "link up" : "no link"}</span>`
        + (i.ipv4.some((a) => a.address === here) ? `<span class="dim">· this browser's way in</span>` : "") + `</div>`
        + `<div class="netaddr">${addr ? esc(addr) : "—"}</div>`;
      if (i.type === "usb-radio")
        out += `<div class="expl">The radio's own link (Pluto or LibreSDR on USB). It reaches the radio, not the console.</div>`;
      if (i.type === "tailscale" && n.tailnet)
        out += `<div class="expl">${esc(n.tailnet.name)}: the console at <b>${esc(consoleUrl(n.tailnet.name || n.tailnet.ip, n))}</b> from your tailnet.</div>`;
      if (i.type === "wifi") {
        out += `<div class="expl">${wifiLine(w)}</div>`;
        const saved = (w && w.saved) || [];
        // Saved networks forget themselves when tapped, so they look like
        // what they do - grey with a red ✕ - not like a blue chosen setting.
        if (saved.length) out += `<div class="chips ctl">` + saved.map((s) => {
          const armed = netArmed === "forget" + i.name + s.id;
          return `<button class="chip forget${armed ? " armed" : ""}" data-nf="${esc(i.name)}" data-id="${esc(s.id)}">`
            + (armed ? `Tap again to forget ${esc(s.ssid)}` : `${esc(s.ssid)}${s.current ? " (in use)" : ""} <i>✕</i>`) + `</button>`;
        }).join("") + `</div><div class="dim">Saved networks; tap one to forget it.</div>`;
        out += `<div class="chips ctl"><button class="chip" data-nw="${esc(i.name)}">Join a network…</button></div>`;
      }
      if (i.type === "wired" || i.type === "wifi") {
        const armed = netArmed === "dhcp" + i.name;
        out += `<div class="chips ctl"><span class="dim" style="align-self:center;margin-right:6px">Address</span>`
          + `<button class="chip${armed ? " armed" : dyn ? " on" : ""}" data-nd="${esc(i.name)}">${armed ? "Tap again to use DHCP" : "Automatic (DHCP)"}</button>`
          + `<button class="chip${dyn === false ? " on" : ""}" data-ns="${esc(i.name)}">Static…</button></div>`;
      }
      return out + `</div>`;
    });
    $("netlist").className = "";
    $("netlist").style.cssText = "display:grid;gap:10px";
    $("netlist").innerHTML = rows.join("") || `<div class="dim">No network interfaces found.</div>`;
  }
  const netMsg = (m) => { $("netmsg").textContent = m; };
  function arm(key) {
    netArmed = key; paintNetList();
    setTimeout(() => { if (netArmed === key) { netArmed = null; paintNetList(); } }, 5000);
  }
  $("netlist").addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (!auth.authorised) return netMsg("Remote: view only. Enter the PIN first.");
    if (b.dataset.nw) return openWifi(b.dataset.nw);
    if (b.dataset.ns) return openStatic(b.dataset.ns);
    if (b.dataset.nd) {
      // Two taps: from a browser elsewhere, or off a static address, the
      // address may change under this page.
      if (netArmed !== "dhcp" + b.dataset.nd) return arm("dhcp" + b.dataset.nd);
      netArmed = null; b.disabled = true; b.textContent = "Applying…";
      if (await netPost("/api/network/ipv4", { iface: b.dataset.nd, mode: "dhcp" }, netMsg)) netMsg("");
      return loadNet();
    }
    if (b.dataset.nf) {
      const key = "forget" + b.dataset.nf + b.dataset.id;
      if (netArmed !== key) return arm(key);
      netArmed = null; b.disabled = true;
      await netPost("/api/network/forget", { iface: b.dataset.nf, id: b.dataset.id }, netMsg);
      return loadNet();
    }
  });

  // Wi-Fi: scan, tap a network, type the passphrase.
  const SEC = { open: "open", wpa2: "WPA2", wpa3: "WPA3 only: not supported yet", enterprise: "enterprise: not supported", wep: "WEP: not supported" };
  const wifiMsg = (m, err) => { $("wifimsg").innerHTML = m; $("wifimsg").className = err ? "err" : "dim"; };
  function openWifi(iface) {
    wifiIf = iface; wifiNets = null;
    $("wifititle").textContent = `Wi-Fi (${iface})`;
    const i = (net.interfaces || []).find((x) => x.name === iface);
    wifiMsg(i && i.ipv4.some((a) => a.address === location.hostname)
      ? "This browser reaches OpenHamShack over this Wi-Fi: joining another network may cut it off. Find the new address on the box's screen." : "");
    $("wifim").classList.add("open");
    paintWifi(); scanWifi();
  }
  async function scanWifi() {
    $("wifiscan").disabled = true;
    $("wifilist").innerHTML = `<div class="dim">Looking for networks… (about 5 seconds)</div>`;
    const j = await netPost("/api/network/scan", { iface: wifiIf }, (m) => m && wifiMsg(esc(m), true));
    $("wifiscan").disabled = false;
    // A refusal (switched off, service not starting) is the message and
    // nothing else: listing "No networks found" under it would send the
    // operator looking for a router instead of the switch.
    if (!j) { wifiNets = null; $("wifilist").innerHTML = ""; return; }
    wifiNets = j.networks || [];
    if (j.note) wifiMsg(esc(j.note), true);
    else if (j.unblocked) wifiMsg("Wi-Fi was switched off in software; OpenHamShack switched it on.");
    paintWifi();
    loadNet();
  }
  const bars = (s) => { const n = s >= -55 ? 4 : s >= -67 ? 3 : s >= -75 ? 2 : 1; return "▮".repeat(n) + "▯".repeat(4 - n); };
  function paintWifi() {
    if (!wifiNets) return;
    const w = netView && netView.wifi ? netView.wifi[wifiIf] : null;
    const saved = new Set(((w && w.saved) || []).map((s) => s.id));
    const cur = w && w.state && w.state.wpa_state === "COMPLETED" ? w.state.ssid : null;
    $("wifilist").innerHTML = wifiNets.map((nw) => {
      const ok = nw.security === "open" || nw.security === "wpa2";
      return `<div class="wrow${nw.ssid === cur ? " cur" : ""}"><span class="bars">${bars(nw.signal)}</span>`
        + `<div><b>${esc(nw.ssid)}</b><div class="dim" style="font-size:17px">${esc(SEC[nw.security] || nw.security)} · ${nw.band}`
        + `${nw.ssid === cur ? " · connected" : saved.has(nw.id) ? " · saved" : ""}</div></div>`
        + `<button class="chip" data-wj="${esc(nw.id)}"${ok ? "" : " disabled"}>${nw.ssid === cur ? "Rejoin" : "Join"}</button></div>`;
    }).join("") || `<div class="dim">No networks found. Scan again, or add a hidden one.</div>`;
  }
  function askPassphrase(ssid, hidden) {
    Keyboard.open({ title: `Passphrase for ${ssid} (leave empty for an open network)`, mask: true, symbols: true, maxLength: 64,
      onOk: (psk) => joinWifi(ssid, psk, hidden) });
  }
  async function joinWifi(ssid, psk, hidden) {
    if (psk && (psk.length < 8 || psk.length > 64)) return wifiMsg("A passphrase is 8 to 63 characters.", true);
    wifiMsg(`Joining ${esc(ssid)}…`);
    if (!await netPost("/api/network/wifi", { iface: wifiIf, ssid, psk, hidden }, (m) => m && wifiMsg(esc(m), true))) return;
    // Watch for the outcome: connected, refused (wrong passphrase), or still
    // trying after half a minute.
    const until = Date.now() + 30000;
    clearInterval(wifiWatch);
    wifiWatch = setInterval(async () => {
      await loadNet();
      const w = netView && netView.wifi ? netView.wifi[wifiIf] : null, st = (w && w.state) || {};
      const me = ((w && w.saved) || []).find((s) => s.ssid === ssid);
      if (st.wpa_state === "COMPLETED" && st.ssid === ssid) {
        const i = net.interfaces.find((x) => x.name === wifiIf), a = i && i.ipv4[0];
        wifiMsg(`Connected to ${esc(ssid)}` + (a ? `: the console is at <b>${esc(consoleUrl(a.address, net))}</b>` : ", waiting for an address…"));
        if (a) clearInterval(wifiWatch);
      } else if (me && me.disabled) {
        wifiMsg(`${esc(ssid)} refused the connection: check the passphrase and join again.`, true);
        clearInterval(wifiWatch);
      } else if (Date.now() > until) {
        wifiMsg(`Not connected to ${esc(ssid)} yet. It is saved and OpenHamShack keeps trying while it is in range.`, true);
        clearInterval(wifiWatch);
      }
    }, 2000);
  }
  $("wifilist").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-wj]"); if (!b || !wifiNets) return;
    const nw = wifiNets.find((x) => x.id === b.dataset.wj); if (!nw) return;
    if (nw.security === "open") return joinWifi(nw.ssid, "", false);
    askPassphrase(nw.ssid, false);
  });
  $("wifiscan").onclick = scanWifi;
  $("wifihidden").onclick = () => Keyboard.open({ title: "Name of the hidden network (exactly as set on the router)", symbols: true, maxLength: 32,
    onOk: (ssid) => { if (ssid) askPassphrase(ssid, true); } });
  $("wificlose").onclick = () => { clearInterval(wifiWatch); wifiIf = null; $("wifim").classList.remove("open"); };

  // Static address. Each value comes from the number pad; the daemon's
  // helper checks them all again. Held for 60 s until kept (the bar above).
  let nip = null;
  const maskOf = (p) => { const m = p ? (0xffffffff << (32 - p)) >>> 0 : 0; return [24, 16, 8, 0].map((s) => (m >>> s) & 255).join("."); };
  function openStatic(iface) {
    const i = net.interfaces.find((x) => x.name === iface) || { ipv4: [] };
    const a = i.ipv4[0] || {};
    const gw = a.address ? a.address.split(".").slice(0, 3).join(".") + ".1" : "";
    nip = { iface, address: a.address || "", prefix: a.prefix || 24, gateway: gw, dns: gw };
    $("netiptitle").textContent = `Static address: ${iface}`;
    $("netiperr").textContent = "";
    const there = i.ipv4.some((x) => x.address === location.hostname);
    $("netipwarn").innerHTML = (there ? "<b>This browser reaches OpenHamShack through this interface.</b> After Apply, open the new address and tap Keep there. "
      : !auth.local ? "Changing the address may cut this browser off. " : "")
      + "If nobody taps <b>Keep</b> within 60 seconds, OpenHamShack goes back to the settings it had. "
      + "Automatic (DHCP) on the box's screen always brings the wired port back.";
    paintStatic();
    $("netip").classList.add("open");
  }
  function paintStatic() {
    for (const b of document.querySelectorAll("[data-nip]")) b.textContent = nip[b.dataset.nip] || "none";
    $("netipmask").textContent = "= " + maskOf(Number(nip.prefix));
  }
  $("netipform").addEventListener("click", (e) => {
    const b = e.target.closest("[data-nip]"); if (!b) return;
    const k = b.dataset.nip;
    const titles = { address: "IPv4 address", prefix: "Prefix length (24 = 255.255.255.0)", gateway: "Gateway (router)", dns: "DNS server" };
    openPad("__net", titles[k], "ip", (v) => { nip[k] = k === "prefix" ? Number(v) : v; paintStatic(); });
  });
  $("netipclose").onclick = () => $("netip").classList.remove("open");
  $("netipapply").onclick = async () => {
    const body = { iface: nip.iface, mode: "static", address: nip.address, prefix: Number(nip.prefix),
                   gateway: nip.gateway, dns: nip.dns ? [nip.dns] : [] };
    $("netipapply").disabled = true;
    const j = await netPost("/api/network/ipv4", body, (m) => { $("netiperr").textContent = m; });
    $("netipapply").disabled = false;
    if (!j) return;
    $("netip").classList.remove("open");
    refresh(); loadNet();
  };
  setInterval(() => { if (setOpen) loadNet(); }, 5000);
