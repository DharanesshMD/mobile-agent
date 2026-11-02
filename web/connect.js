/* "Connect a phone": the dialog behind the button in the phones panel (#connectSlot calls window.openConnectDialog).
   Step 1 opens the phone gateway on this Wi-Fi, step 2 shows a QR code of the one-time link, step 3 waits for the phone.
   Plain script, no build step, no outside requests: the QR encoder is web/vendor/qrcode.js (MIT). Uses the page's el(), api(), toast(), loadBots(). */
(function () {
  const CSS = `
  #connectDlg { width: min(520px, 94vw); max-height: 92vh; overflow: auto; }
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
  `;
  const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  let dlg, poll, tick, st = null, since = 0, opening = false, lastUrl = "";

  function build() {
    if (dlg) return;
    const style = document.createElement("style"); style.textContent = CSS; document.head.append(style);
    dlg = document.createElement("dialog"); dlg.id = "connectDlg"; dlg.setAttribute("aria-labelledby", "cdTitle");
    dlg.addEventListener("close", stop);
    dlg.addEventListener("cancel", stop);
    document.body.append(dlg);
  }

  function stop() { clearInterval(poll); clearInterval(tick); poll = tick = null; }

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
      const fresh = (st.connected || []).find(p => p.since >= (st.opened_at || 0) - 1), name = (fresh && (fresh.name || fresh.model)) || (st.paired[0] && st.paired[0].name) || "Your phone";
      render(name);
      if (typeof loadBots === "function") loadBots();
      setTimeout(() => { if (dlg.open) dlg.close(); toast(name + " is connected. Its bot is in the list."); }, 1600);
    }
  }

  function render(connectedName) {
    const s = st || {};
    const open = !!s.window, parts = [];
    const h = el("h3", "", "Connect a phone"); h.id = "cdTitle";
    parts.push(h, el("div", "sub", "Scan a code with your Android phone, download the Mobile Agent app and pair it. The app asks for your approval to operate the screen, and shows a notification while it is connected."));

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

    const acts = el("div", "acts"); const close = el("button", "", "Close"); close.type = "button"; close.onclick = () => dlg.close(); acts.append(close);
    parts.push(acts);
    dlg.replaceChildren(...parts);
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
