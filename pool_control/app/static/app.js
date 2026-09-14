(() => {
  const $ = (id) => document.getElementById(id);
  let state = null;
  const pendingUntil = new Map();

  function fmtTemp(v) { return v == null ? "--" : `${Math.round(v)}°`; }

  async function post(path, body) {
    const el = document.activeElement;
    try {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      if (!r.ok) {
        let msg = `Error ${r.status}`;
        try { msg = (await r.json()).error || msg; } catch (_) {}
        toast(msg);
        clearPending();
      }
    } catch (e) {
      toast("Network error");
      clearPending();
    }
  }

  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.hidden = true; }, 4000);
  }

  function setPending(btn) {
    btn.classList.add("pending");
    pendingUntil.set(btn, Date.now() + 10000);
  }
  function clearPending() {
    for (const [btn] of pendingUntil) btn.classList.remove("pending");
    pendingUntil.clear();
  }

  function render(s) {
    state = s;
    clearPending();
    $("pool-temp").textContent = fmtTemp(s.ha.pool_temp ?? s.aqualink.pool_temp);
    $("air-temp").textContent = fmtTemp(s.aqualink.air_temp);
    const spaOn = s.ha.switches.spa;
    $("spa-reading").hidden = !spaOn;
    $("spa-temp").textContent = fmtTemp(s.ha.spa_temp ?? s.aqualink.spa_temp);

    const aq = s.aqualink;
    $("pumpline").textContent = !s.ha.switches.filter_pump ? "Pump off"
      : aq.rpm != null ? `Pump ${aq.active_preset ? aq.active_preset + " · " : ""}${aq.rpm} RPM` : "Pump on";

    $("spa-label").textContent = s.spa.label;
    for (const btn of document.querySelectorAll("[data-switch]")) {
      const name = btn.dataset.switch;
      let on;
      if (name === "waterfall") on = aq.waterfall_on;
      else if (name === "lights_all") on = Object.values(s.ha.lights).every(Boolean);
      else if (name in s.ha.switches) on = s.ha.switches[name];
      else on = s.ha.lights[name];
      btn.classList.toggle("on", !!on);
      btn.disabled = name === "waterfall" ? !aq.connected : !s.ha.connected;
    }

    const t = s.thermostat;
    $("thermo-enabled").checked = t.settings.enabled;
    $("thermo-target").textContent = `${t.settings.target}°`;
    $("thermo-buffer").textContent = `${t.settings.buffer}°`;
    $("thermo-off-early").textContent = `${t.settings.off_early}°`;
    $("thermo-status").textContent = t.status;

    $("aq-note").hidden = aq.connected;
    const grid = $("presets");
    grid.innerHTML = "";
    for (const p of aq.presets) {
      const b = document.createElement("button");
      b.innerHTML = `<span>${p.label}</span><small>${p.rpm} RPM</small>`;
      b.disabled = !aq.connected;
      b.classList.toggle("active", p.label === aq.active_preset);
      b.addEventListener("click", () => { setPending(b); post(`api/pump/preset/${p.index}`); });
      grid.appendChild(b);
    }
    $("rpm-form").querySelector("button").disabled = !aq.connected;

    $("ha-health").textContent = `HA: ${s.ha.connected ? "connected" : "disconnected"}`;
    $("aq-health").textContent = `iAqualink: ${aq.connected ? "connected" : (aq.error ? "error" : "disconnected")}`;
    const la = t.last_action;
    $("last-action").textContent = la ? `Heater ${la.action} at ${Math.round(la.temp)}° (${la.time.slice(11, 16)}) — ${la.reason}` : "";
  }

  document.body.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (!btn || btn.disabled) return;
    if (btn.dataset.switch) {
      const name = btn.dataset.switch;
      setPending(btn);
      post(`api/switch/${name}`, { on: !btn.classList.contains("on") });
    } else if (btn.dataset.action === "spa-start") {
      setPending(btn); post("api/spa/start");
    } else if (btn.dataset.action === "spa-end") {
      setPending(btn); post("api/spa/end");
    } else if (btn.dataset.thermo) {
      const key = btn.dataset.thermo;
      const cur = state.thermostat.settings[key];
      setPending(btn);
      post("api/thermostat", { [key]: cur + Number(btn.dataset.delta) });
    }
  });

  $("thermo-enabled").addEventListener("change", (ev) => post("api/thermostat", { enabled: ev.target.checked }));

  $("rpm-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const rpm = Number($("rpm-input").value);
    if (!rpm) return;
    setPending(ev.submitter || $("rpm-form").querySelector("button"));
    post("api/pump/rpm", { rpm });
  });

  setInterval(() => {
    const now = Date.now();
    for (const [btn, until] of pendingUntil) if (until < now) { btn.classList.remove("pending"); pendingUntil.delete(btn); }
  }, 1000);

  function connect() {
    let es;
    try { es = new EventSource("api/events"); } catch (_) { return poll(); }
    es.onmessage = (ev) => render(JSON.parse(ev.data));
    es.onerror = () => { es.close(); setTimeout(connect, 3000); };
  }
  async function poll() {
    try { render(await (await fetch("api/state")).json()); } catch (_) {}
    setTimeout(poll, 5000);
  }
  fetch("api/state").then((r) => r.json()).then(render).catch(() => {});
  connect();
})();
