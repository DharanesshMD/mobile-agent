/* "Setup": the checklist behind the sidebar button. Step 1 saves the model API key, step 2 (Docker only) the computer's Wi-Fi address, step 3 opens
   "Connect a phone". Opens by itself once per browser tab while the key is missing. Plain script; uses the page's el(), api(), toast(). */
(function () {
  const CSS = `
  #setupDlg { width: min(520px, 94vw); max-height: 92vh; overflow: auto; }
  #setupDlg .step { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--line); }
  #setupDlg .step:first-of-type { border-top: 0; padding-top: 0; }
  #setupDlg .step > b { display: flex; gap: 8px; align-items: center; font-weight: 500; }
  #setupDlg .mark { font: 500 12px/1 var(--mono); width: 20px; height: 20px; border: 1px solid var(--line-2); border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; color: var(--text-3); flex: none; }
  #setupDlg .done .mark { color: var(--ok); border-color: var(--ok); }
  #setupDlg .row { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; } #setupDlg .row input { flex: 1 1 200px; min-width: 0; }
  #setupDlg .note { color: var(--text-3); font-size: 12px; margin-top: 8px; line-height: 1.5; } #setupDlg .err { color: var(--bad); font-size: 13px; margin-top: 8px; }
  #setupDlg code { font: 400 12px var(--mono); background: var(--ink-1); border-radius: 4px; padding: 1px 5px; }
  #setupDlg .cdhead { display: flex; align-items: center; justify-content: space-between; }`;
  let dlg, body, st = null, err = "", busy = false;

  const keep = (id) => { const n = document.getElementById(id); return n ? n.value : ""; };

  function step(title, done, ...kids) {
    const s = el("div", "step" + (done ? " done" : "")), b = el("b"), m = el("span", "mark", done ? "✓" : "•");
    b.append(m, document.createTextNode(title)); s.append(b, ...kids); return s;
  }
  function field(id, ph, type, val, btnText, onclick) {
    const row = el("div", "row"), inp = el("input"); inp.id = id; inp.type = type; inp.placeholder = ph; inp.value = val || ""; inp.autocomplete = "off"; inp.spellcheck = false;
    const b = el("button", "pill", busy ? "Working…" : btnText); b.type = "button"; b.disabled = busy; b.onclick = onclick;
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") onclick(); });
    row.append(inp, b); return row;
  }
  async function submit(path, payload) {
    busy = true; err = ""; render();
    try { await api(path, payload); st = await api("/api/setup"); } catch (e) { err = e.message; }
    busy = false; render(); updateBadge(); watching();
  }

  function emulatorStep(em) {
    const ins = em.install || {};
    const hs = em.host;
    if (hs) {
      if (hs.configured) return step("Android phone (emulator on this computer)", true, el("div", "note", `Connected to ${hs.serial} through this computer's adb. The app uses it like a phone on localhost.`));
      if (hs.found || hs.applying) return step("Android phone (emulator on this computer)", false, el("div", "note", "Emulator found. Connecting and restarting the app\u2026 this page reconnects by itself."));
      const cmd = el("pre"); cmd.textContent = hs.command; cmd.style.cssText = "margin:8px 0 0;padding:8px 10px;background:var(--ink-1);border:1px solid var(--line);border-radius:8px;font:12px var(--mono);overflow:auto";
      const copy = el("button", "pill", "Copy command"); copy.type = "button";
      copy.onclick = async () => { try { await navigator.clipboard.writeText(hs.command); copy.textContent = "Copied"; } catch (e) { copy.textContent = "Select and copy it"; } };
      const row = el("div", "row"); row.append(copy);
      return step("Android phone (emulator on this computer)", false,
        el("div", "note", "Docker cannot run an Android emulator itself, but it can use one on your computer, with full speed. Open a terminal in the project folder on this computer and run the command below. It installs the emulator the first time (about 2.5 GB, asks you to accept the Android SDK terms) and starts it. This page notices it within seconds and finishes the setup."),
        cmd, row, el("div", "note", "Waiting for an emulator on this computer\u2026"));
    }
    if (em.available && ins.state !== "restarting") return step("Android phone (emulator)", true, el("div", "note", "Detected. The app starts it when a task needs it and puts it to sleep when idle."));
    if (ins.state === "running" || ins.state === "restarting") {
      const bar = el("progress"); bar.max = 100; bar.value = ins.pct || 0; bar.style.cssText = "width:100%;margin-top:10px";
      return step("Installing the emulator\u2026", false, el("div", "note", ins.step || ""), bar, el("div", "note", ins.message || ""),
        el("div", "note", ins.state === "restarting" ? "Restarting the app: this page reconnects by itself." : "You can close this window; the install keeps going."));
    }
    if (!em.supported) return step("Android phone (emulator)", false, el("div", "note", "Not available here. Google publishes no Android emulator for Linux on ARM, which is what Docker is on an Apple Silicon Mac. Connect a real phone below, or run the app directly on the host (see the README) to use the emulator."));
    const kids = [el("div", "note", `One click installs the Android 14 emulator with Google Play (about ${em.gb} GB download) and the app starts using it by itself afterwards.`)];
    if (em.docker && !em.kvm) kids.push(el("div", "err", "This Docker has no hardware acceleration (/dev/kvm), so the phone runs in software: expect a boot of 10+ minutes and a slow screen. A real phone (below) is much better here."));
    const lab = el("label", "note"), cb = el("input"); cb.type = "checkbox"; cb.id = "suLic"; cb.style.marginRight = "6px";
    const a = el("a", "", "Android SDK terms"); a.href = em.license_url; a.target = "_blank"; a.rel = "noopener";
    lab.append(cb, document.createTextNode("I accept the "), a);
    const go = el("button", "pill", "Install emulator"); go.type = "button";
    go.onclick = () => { if (!cb.checked) { err = "Tick the box to accept the Android SDK terms first."; render(); return; } submit("/api/setup/emulator", { accept: true }); };
    const row = el("div", "row"); row.append(go);
    kids.push(lab, row);
    if (ins.state === "failed") kids.push(el("div", "err", "Last try failed: " + ins.error));
    return step("Android phone (emulator)", false, ...kids);
  }

  function render() {
    if (!st) return;
    const keepKey = keep("suKey"), keepIp = keep("suIp"), kids = [];
    kids.push(step("Model API key", st.key.set,
      el("div", "note", st.key.set ? (st.key.source === "page" ? "Saved from this page." : "Set when the container started.") + " Paste a new one to replace it."
        : "Chat needs a Meta Model API key. Get one at dev.meta.ai, paste it here, and it is checked and saved in this app's data (never shown again)."),
      field("suKey", st.key.set ? "Replace the key" : "Paste your META_API_KEY", "password", keepKey, "Save key", () => submit("/api/setup/key", { key: document.getElementById("suKey").value }))));
    if (st.docker) {
      const guess = /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(location.hostname) ? location.hostname : "";
      kids.push(step("This computer's Wi-Fi address", !st.phone.needs_address,
        el("div", "note", "Docker cannot see your Wi-Fi address, and a phone needs it to reach this app. Type the address of the computer running Docker (not the phone)."),
        field("suIp", "for example 192.168.1.20", "text", keepIp || st.phone.address || guess, "Save address", () => submit("/api/setup/address", { ip: document.getElementById("suIp").value })),
        (() => { const n = el("div", "note"); n.append("Find it: Mac ", Object.assign(el("code"), { textContent: "ipconfig getifaddr en0" }), ", Windows ", Object.assign(el("code"), { textContent: "ipconfig" }), ", Linux ", Object.assign(el("code"), { textContent: "hostname -I" }), "."); return n; })()));
    }
    const ready = st.phone.connected > 0;
    const p = el("div", "note", ready ? `${st.phone.connected} phone connected.` : st.phone.apk ? "Install the Mobile Agent app on the phone (this app offers it), then pair with a code. The phone and this computer must be on the same Wi-Fi."
      : "The Mobile Agent app file is not in this install yet. If the app is already on the phone, you can still pair with the code; otherwise build it with scripts/build_apk.sh and copy it to data/apk/mobile-agent.apk.");
    const go = el("button", "pill", "Connect a phone"); go.type = "button"; go.disabled = st.docker && st.phone.needs_address;
    go.onclick = () => { dlg.close(); window.openConnectDialog && window.openConnectDialog(); };
    const row = el("div", "row"); row.append(go);
    kids.push(step("Connect a real phone", ready, p, row));
    kids.push(emulatorStep(st.emulator));
    if (err) kids.push(el("div", "err", err));
    body.replaceChildren(...kids);
    const ki = document.getElementById("suKey"); if (ki && keepKey) ki.focus();
  }

  function build() {
    if (dlg) return;
    const style = document.createElement("style"); style.textContent = CSS; document.head.append(style);
    dlg = document.createElement("dialog"); dlg.id = "setupDlg"; dlg.setAttribute("aria-labelledby", "suTitle");
    const h = el("h3", "", "Setup"); h.id = "suTitle";
    const x = el("button", "ghost icon"); x.type = "button"; x.title = "Close"; x.setAttribute("aria-label", "Close");
    x.innerHTML = '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>'; x.onclick = () => dlg.close();
    const head = el("div", "cdhead"); head.append(h, x);
    body = el("div");
    const acts = el("div", "acts"), close = el("button", "", "Close"); close.type = "button"; close.onclick = () => dlg.close(); acts.append(close);
    dlg.append(head, body, acts); document.body.append(dlg);
  }

  function updateBadge() {
    const c = document.getElementById("setupCount");
    if (!c || !st) return;
    const open = (st.key.set ? 0 : 1) + (st.docker && st.phone.needs_address ? 1 : 0) + (st.emulator.available || (st.emulator.host && st.emulator.host.configured) || st.phone.connected ? 0 : 1);
    c.textContent = open ? String(open) : "";
  }

  let watch = null;
  function watching() {
    const ins = st && st.emulator && st.emulator.install;
    const hs = st && st.emulator && st.emulator.host;
    const on = dlg && dlg.open && ((ins && (ins.state === "running" || ins.state === "restarting")) || (hs && !hs.configured));
    if (on && !watch) watch = setInterval(async () => { try { st = await api("/api/setup"); render(); updateBadge(); } catch (e) { /* the app is restarting */ } if (!watching()) { clearInterval(watch); watch = null; } }, 2000);
  }
  window.openSetupDialog = async function () {
    build();
    try { st = await api("/api/setup"); } catch (e) { toast(e.message); return; }
    err = ""; render(); updateBadge();
    if (!dlg.open) dlg.showModal();
    watching();
  };

  async function boot() {
    try { st = await api("/api/setup"); } catch (e) { return; }
    updateBadge();
    let seen = false; try { seen = sessionStorage.getItem("setupSeen") === "1"; } catch {}
    if (!st.key.set && !seen) { try { sessionStorage.setItem("setupSeen", "1"); } catch {} window.openSetupDialog(); }
  }
  const btn = document.getElementById("setupBtn"); if (btn) btn.onclick = () => { window.openSetupDialog(); };
  boot();
})();
