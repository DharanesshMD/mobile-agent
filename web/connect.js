/* "Connect a phone": the dialog behind the button in the phones panel (#connectSlot calls window.openConnectDialog).
   Step 1 opens the phone gateway on this Wi-Fi, step 2 shows a QR code of the one-time link, step 3 waits for the phone.
   Plain script, no build step, no outside requests: the QR encoder is web/vendor/qrcode.js (MIT). Uses the page's el(), api(), toast(), loadBots(). */
(function () {
  const CSS = `
  #connectDlg { width: min(520px, 94vw); max-height: 92vh; overflow: auto; }
  #connectDlg .cdhead { position: sticky; top: -24px; z-index: 1; display: flex; align-items: center; justify-content: space-between; gap: 12px; background: var(--ink-2); margin: -24px 0 0; padding: 24px 0 8px; }
  #connectDlg .cdhead h3 { margin: 0; }
  #connectDlg .step { margin-top: 18px; }
  #connectDlg .step > b { display: flex; gap: 8px; align-items: center; font-weight: 500; }
  #connectDlg .num { font: 500 11px/1 var(--mono); color: var(--text-3); border: 1px solid var(--line-2); border-radius: 50%; width: 20px; height: 20px; display: inline-flex; align-items: center; justify-content: center; flex: none; }
  #connectDlg .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
  #connectDlg .qr { margin: 10px 0 8px; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
  #connectDlg .tile { background: var(--text); border-radius: 12px; padding: 10px; width: min(236px, 62vw); aspect-ratio: 1; flex: none; }
  #connectDlg .tile svg { width: 100%; height: 100%; display: block; }
  #connectDlg .url { font: 400 12px/1.5 var(--mono); color: var(--text-2); word-break: break-all; }
  #connectDlg .code { font: 500 26px/1.1 var(--mono); letter-spacing: .08em; margin: 4px 0; }
  #connectDlg .note { color: var(--text-3); font-size: 12px; margin-top: 10px; }
  #connectDlg .warn { color: var(--amber); font-size: 13px; margin-top: 8px; }
  #connectDlg .status { display: flex; gap: 8px; align-items: center; margin-top: 8px; color: var(--text-2); }
  #connectDlg pre { font: 400 12px/1.6 var(--mono); background: var(--ink-1); border: 1px solid var(--line); border-radius: 8px; padding: 10px; overflow: auto; margin: 8px 0 0; }
  #connectDlg details { margin-top: 18px; color: var(--text-2); font-size: 13px; } #connectDlg summary { cursor: pointer; }
  #connectDlg details.inst { margin-top: 22px; }
  #connectDlg details.inst .way { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--line); color: var(--text-2); }
  #connectDlg .devlist { margin-top: 8px; display: grid; gap: 8px; }
  #connectDlg .dev { display: flex; gap: 10px; align-items: center; justify-content: space-between; flex-wrap: wrap; background: var(--ink-1); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; }
  #connectDlg .devt { display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap; min-width: 0; }
  #connectDlg .chip { font-size: 11px; border: 1px solid var(--line-2); border-radius: 999px; padding: 1px 8px; color: var(--text-3); } #connectDlg .chip.ok { color: var(--ok); border-color: var(--ok); }
  #connectDlg .prog { margin-top: 10px; display: grid; gap: 4px; }
  #connectDlg .stp { display: block; } #connectDlg .stp .mark { display: inline-block; width: 1.2em; font-family: var(--mono); }
  #connectDlg .stp.done .mark { color: var(--ok); } #connectDlg .stp.failed .mark { color: var(--bad); } #connectDlg .stp.running .mark { color: var(--amber); }
  #connectDlg .stp .note { margin: 0 0 0 1.2em; }
  #connectDlg .okmsg { color: var(--text); margin-top: 6px; }
  #connectDlg details.inst input { flex: 1 1 140px; min-width: 0; }
  #connectDlg details.inst .row { flex-wrap: wrap; }
  `;
  const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  let dlg, top, installBox, poll, tick, st = null, since = 0, opening = false, lastUrl = "";

  function build() {
    if (dlg) return;
    const style = document.createElement("style"); style.textContent = CSS; document.head.append(style);
    dlg = document.createElement("dialog"); dlg.id = "connectDlg"; dlg.setAttribute("aria-labelledby", "cdTitle");
    dlg.addEventListener("close", stop);
    dlg.addEventListener("cancel", stop);
    const h = el("h3", "", "Connect a phone"); h.id = "cdTitle";
    const x = el("button", "ghost icon cdx"); x.type = "button"; x.title = "Close"; x.setAttribute("aria-label", "Close");
    x.innerHTML = '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>'; x.onclick = () => dlg.close();  // static markup, no server text
    const head = el("div", "cdhead"); head.append(h, x);
    top = el("div");  // the part that is redrawn every 2 s
    installBox = buildInstall();  // kept as it is, so what the owner typed and the progress it shows survive the redraws
    const acts = el("div", "acts"); const close = el("button", "", "Close"); close.type = "button"; close.onclick = () => dlg.close(); acts.append(close);
    dlg.append(head, top, installBox, acts);
    document.body.append(dlg);
  }

  function stop() { clearInterval(poll); clearInterval(tick); poll = tick = null; stopInstall(); }

  function qrSvg(url) {
    try {
      const q = qrcode(0, "M"); q.addData(url); q.make();
      const holder = document.createElement("div"); holder.innerHTML = q.createSvgTag({ cellSize: 4, margin: 16, scalable: true })  /* 4 modules of quiet zone, as scanners expect */;
      const svg = holder.firstElementChild; svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "QR code for " + url);
      return svg;
    } catch (e) { return el("div", "warn", "Could not draw the QR code. Type the address below into the phone's browser."); }
  }

  async function refresh() {
    try { st = await api("/api/gateway/state"); } catch (e) { return; }
    render();
    const got = (st.connected || []).filter(p => p.since >= (st.opened_at || 0) - 1).concat(st.paired || []);
    if (got.length && !dlg.dataset.done) {
      dlg.dataset.done = "1";
      const fresh = (st.connected || []).find(p => p.since >= (st.opened_at || 0) - 1), name = (fresh && (fresh.name || fresh.model)) || (st.paired[0] && st.paired[0].name) || "The phone";
      render(name);
      if (typeof loadBots === "function") loadBots();
      setTimeout(() => { if (dlg.open) dlg.close(); toast(name + " is connected. Its bot is in the list."); }, 1600);
    }
  }

  function render(connectedName) {
    const s = st || {};
    const open = !!s.window, parts = [];
    parts.push(el("div", "sub", "Scan a code with your Android phone, download the Mobile Agent app and pair it. The app asks for your approval to operate the screen, and shows a notification while it is connected."));

    const s1 = el("div", "step"); const b1 = el("b"); b1.append(el("span", "num", "1"), document.createTextNode(" Allow phones on this Wi-Fi"));
    s1.append(b1);
    const r1 = el("div", "row");
    const toggle = el("button", open ? "" : "primary", open ? "Turn off" : "Allow phones on this Wi-Fi"); toggle.type = "button";
    toggle.onclick = async () => { toggle.disabled = true; try { st = await api(open ? "/api/gateway/close" : "/api/gateway/open", {}); } catch (e) { toast(e.message); } render(); };
    r1.append(toggle);
    if (open) { const left = el("span", "mono", "open for " + fmt(s.window_left)); left.id = "cdLeft"; left.dataset.left = s.window_left; r1.append(left); }
    s1.append(r1);
    if (s.error) s1.append(el("div", "warn", s.error));
    else if (!open && s.lan === false) s1.append(el("div", "warn", "No local network address was found. Connect this computer to Wi-Fi, or use the USB route below."));
    else if (!open) s1.append(el("div", "note", "This opens one small door on your Wi-Fi (" + (s.address || "port " + (s.port || 8765)) + ") for a few minutes. Nothing else of the app is reachable from the network, and the door closes by itself."));
    parts.push(s1);

    if (open && s.url) {
      const s2 = el("div", "step"); const b2 = el("b"); b2.append(el("span", "num", "2"), document.createTextNode(" Scan this with the phone"));
      s2.append(b2);
      const qr = el("div", "qr"), tile = el("div", "tile"); tile.append(qrSvg(s.url)); qr.append(tile);
      const side = el("div"); side.append(el("div", "note", "Pairing code (the page fills it in for you)"), el("div", "code", s.code || ""), el("div", "url", s.url));
      qr.append(side); s2.append(qr);
      if (!s.apk || !s.apk.present) s2.append(el("div", "warn", "The app file is not on this computer yet. Run scripts/build_apk.sh in the project folder, then scan again."));
      else s2.append(el("div", "note", "App file: " + (s.apk.size / 1e6).toFixed(1) + " MB, SHA-256 " + s.apk.sha256.slice(0, 16) + "… (the full value is on the phone's page)."));
      parts.push(s2);

    }

    if ((open && s.url) || connectedName) {
      const s3 = el("div", "step"); const b3 = el("b"); b3.append(el("span", "num", "3"), document.createTextNode(" Waiting for the phone"));
      s3.append(b3);
      const stt = el("div", "status"); stt.setAttribute("role", "status"); stt.setAttribute("aria-live", "polite");
      const lamp = el("span", connectedName ? "lamp idle" : "lamp on"); stt.append(lamp, document.createTextNode(connectedName ? connectedName + " connected" : "Waiting for the phone…"));
      s3.append(stt); parts.push(s3);
    }

    parts.push(el("div", "note", "This link works over plain HTTP on your local network, so use a Wi-Fi you trust. The door closes 15 minutes after you open it, or 2 minutes after a phone pairs; a connected phone keeps working until it leaves."));

    const usb = document.createElement("details"); usb.append(el("summary", "", "Prefer a cable? Use USB instead (nothing is exposed to the network)"));
    const pre = el("pre", "", "adb devices\nadb -s <your-phone> reverse tcp:8000 tcp:8000\n# then in the app: server http://localhost:8000 and a pairing code");
    const codeBtn = el("button", "", "Show a pairing code"); codeBtn.type = "button"; const out = el("div", "code");
    codeBtn.onclick = async () => { try { const r = await api("/api/pair/code", {}); out.textContent = r.code; } catch (e) { toast(e.message); } };
    usb.append(pre, codeBtn, out); parts.push(usb);

    top.replaceChildren(...parts);
  }

  // ---- "Phone says App blocked to protect your device?": install from this computer over adb
  let instPoll = null, instJob = null, instTimer = null;
  const STATE = { device: "Ready", unauthorized: "Allow USB debugging on the phone", offline: "Offline", authorizing: "Waiting for the phone" };

  function stopInstall() { clearInterval(instPoll); clearInterval(instTimer); instPoll = instTimer = null; }

  function buildInstall() {
    const box = document.createElement("details"); box.className = "inst";
    box.append(el("summary", "", 'Phone says "App blocked to protect your device"?'));
    box.append(el("p", "note", "That message is Google Play Protect, not a virus warning about this app. In some countries it blocks apps that can operate the screen when they come from a browser download. "
      + "An install made from this computer is a different route that commonly works (Google does not promise it). Nothing is turned on for you: you still switch on Accessibility on the phone."));

    const a = el("div", "way"); a.append(el("b", "", "A. Install from this computer"));
    const list = el("div", "devlist"); list.setAttribute("aria-live", "polite");
    const prog = el("div", "prog"); prog.setAttribute("aria-live", "polite");
    a.append(el("div", "note", "Plug the phone in with a USB cable (turn on USB debugging in Developer options and tap Allow on the phone), or pair over Wi-Fi in B. Xiaomi: also turn on Install via USB in Developer options."), list, prog);

    const b = el("div", "way"); b.append(el("b", "", "B. No cable? Pair over Wi-Fi"));
    const ol = el("ol");
    for (const t of ["Settings > About phone: tap the MIUI or OS version 7 times to unlock Developer options.", "Settings > Developer options: turn on Wireless debugging (the phone and this computer on the same Wi-Fi).",
                     "Tap Pair device with pairing code. Type the address and the 6 digits it shows here."]) ol.append(el("li", "", t));
    const row = el("div", "row");
    const addr = el("input"); addr.type = "text"; addr.placeholder = "192.168.0.23:37045"; addr.setAttribute("aria-label", "Pairing address and port shown on the phone"); addr.autocomplete = "off"; addr.spellcheck = false;
    const code = el("input"); code.type = "text"; code.inputMode = "numeric"; code.maxLength = 6; code.placeholder = "123456"; code.setAttribute("aria-label", "6-digit pairing code"); code.autocomplete = "off";
    const pair = el("button", "", "Pair"); pair.type = "button";
    row.append(addr, code, pair);
    const say = el("div", "note"); say.setAttribute("role", "status");
    const row2 = el("div", "row"); row2.hidden = true;
    const conn = el("input"); conn.type = "text"; conn.placeholder = "192.168.0.23:41957"; conn.setAttribute("aria-label", "Address and port on the main Wireless debugging screen"); conn.autocomplete = "off"; conn.spellcheck = false;
    const connBtn = el("button", "", "Connect"); connBtn.type = "button"; row2.append(conn, connBtn);
    b.append(ol, row, say, row2);

    const split = (v) => { const m = v.trim().match(/^\[?([^\]]+?)\]?:(\d{1,5})$/); return m ? { host: m[1], port: +m[2] } : null; };
    pair.onclick = async () => {
      const w = split(addr.value);
      if (!w) { say.textContent = "Type the address and port as shown, for example 192.168.0.23:37045."; return; }
      pair.disabled = true; say.textContent = "Pairing…";
      try {
        const r = await api("/api/install/pair", { ...w, code: code.value });
        say.textContent = r.message; code.value = "";
        if (r.needs_port) { row2.hidden = false; conn.value = w.host + ":"; conn.focus(); }
        loadDevices(list, prog);
      } catch (e) { say.textContent = e.message; }
      pair.disabled = false;
    };
    connBtn.onclick = async () => {
      const w = split(conn.value);
      if (!w) { say.textContent = "Type the address and port from the main Wireless debugging screen."; return; }
      connBtn.disabled = true; say.textContent = "Connecting…";
      try { const r = await api("/api/install/connect", w); say.textContent = r.message; if (r.ok) { row2.hidden = true; loadDevices(list, prog); } } catch (e) { say.textContent = e.message; }
      connBtn.disabled = false;
    };

    const c = el("div", "way"); c.append(el("b", "", "C. Or pause Play Protect scanning for a minute"));
    c.append(el("div", "note", "Play Store > your profile icon > Play Protect > the gear > turn off Scan apps with Play Protect. Install the app, then turn it back on. If the block dialog shows More details, choose Install anyway."));

    box.append(a, b, c);
    box.addEventListener("toggle", () => { if (box.open) { loadDevices(list, prog); stopInstall(); instPoll = setInterval(() => loadDevices(list, prog), 4000); } else stopInstall(); });
    return box;
  }

  async function loadDevices(list, prog) {
    let d;
    try { d = await api("/api/install/devices"); } catch (e) { list.replaceChildren(el("div", "warn", e.message)); return; }
    const rows = d.devices.map((p) => {
      const r = el("div", "dev"), t = el("div", "devt");
      t.append(el("span", "", p.model), el("span", "mono small", " " + p.serial + (p.wireless ? " · Wi-Fi" : " · USB")), el("span", "chip" + (p.state === "device" ? " ok" : ""), STATE[p.state] || p.state));
      const go = el("button", "primary", "Install"); go.type = "button"; go.disabled = p.state !== "device" || !!instTimer;
      go.onclick = () => startInstall(p.serial, list, prog);
      r.append(t, go); return r;
    });
    if (!rows.length) rows.push(el("div", "note", "No phone found yet. Check the cable and the USB debugging prompt on the phone, or pair over Wi-Fi below."));
    if (!d.apk) rows.push(el("div", "warn", "The app file is not built on this computer yet: run scripts/build_apk.sh in the project folder first."));
    list.replaceChildren(...rows);
  }

  async function startInstall(serial, list, prog) {
    prog.replaceChildren(el("div", "note", "Starting…"));
    try { instJob = (await api("/api/install/run", { serial })).job; } catch (e) { prog.replaceChildren(el("div", "warn", e.message)); return; }
    clearInterval(instTimer);
    const draw = (j) => {
      const items = j.steps.map((s) => { const li = el("div", "stp " + s.state); li.append(el("span", "mark", { done: "✓", failed: "✗", running: "…", skipped: "–", pending: "·" }[s.state] || "·"), el("span", "", " " + s.label)); if (s.detail) li.append(el("div", "note", s.detail)); return li; });
      if (j.state === "failed" && j.advice) { const w = el("div", "warn"); w.setAttribute("role", "alert"); w.textContent = j.advice; items.push(w); }
      if (j.state === "done" && j.final) items.push(el("div", "okmsg", j.final));
      prog.replaceChildren(...items);
    };
    draw(instJob);
    instTimer = setInterval(async () => {
      try {
        const j = await api("/api/install/jobs/" + instJob.id); draw(j);
        if (j.state !== "running") { clearInterval(instTimer); instTimer = null; loadDevices(list, prog); }
      } catch (e) { clearInterval(instTimer); instTimer = null; }
    }, 1000);
  }

  window.openConnectDialog = async function () {
    build();
    delete dlg.dataset.done; st = null; lastUrl = "";
    if (!dlg.open) dlg.showModal();
    try { st = await api("/api/gateway/state"); } catch (e) { toast(e.message); }
    render();
    stop();
    poll = setInterval(refresh, 2000);
    tick = setInterval(() => { const n = document.getElementById("cdLeft"); if (n && +n.dataset.left > 0) { n.dataset.left = +n.dataset.left - 1; n.textContent = "open for " + fmt(+n.dataset.left); } }, 1000);
  };
})();
