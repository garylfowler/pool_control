(() => {
  const $ = (id) => document.getElementById(id);
  let state = null;
  const pendingUntil = new Map();

  function fmtTemp(v) { return v == null ? "--" : `${Math.round(v)}°`; }

  async function post(path, body) {
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
    $("air-temp").textContent = fmtTemp(s.ha.air_temp ?? s.aqualink.air_temp);  // weather station first, panel sensor as fallback
    const spaOn = s.ha.switches.spa;
    $("spa-reading").hidden = !spaOn;
    $("spa-temp").textContent = fmtTemp(s.ha.spa_temp ?? s.aqualink.spa_temp);

    const aq = s.aqualink;
    $("pump-summary").textContent = !s.ha.switches.filter_pump ? "Pump off"
      : aq.active_preset ? `${aq.active_preset}${aq.rpm != null ? " · " + aq.rpm + " RPM" : ""}`
      : aq.rpm != null ? `${aq.rpm} RPM` : (aq.connected ? "Pump on" : "Speed unknown");

    $("spa-label").textContent = s.spa.label;
    for (const btn of document.querySelectorAll("[data-switch]")) {
      const name = btn.dataset.switch;
      let on;
      if (name === "waterfall") on = aq.waterfall_on;
      else if (name === "boost") on = !!(s.ha.chlorinator && s.ha.chlorinator.boost);
      else if (name === "lights_all") on = Object.values(s.ha.lights).every(Boolean);
      else if (name in s.ha.switches) on = s.ha.switches[name];
      else on = s.ha.lights[name];
      btn.classList.toggle("on", !!on);
      const stale = (s.ha.unavailable || []).includes(name);
      btn.classList.toggle("stale", stale);
      btn.disabled = name === "waterfall" ? !aq.connected : (!s.ha.connected || stale);
    }

    const t = s.thermostat;
    $("thermo-target").textContent = `${t.settings.target}°`;
    $("thermo-buffer").textContent = `${t.settings.buffer}°`;
    $("thermo-off-early").textContent = `${t.settings.off_early}°`;
    $("thermo-status").textContent = t.status;

    $("aq-note").hidden = aq.connected;
    $("aq-note-equipment").hidden = aq.connected;
    const grid = $("presets");
    grid.innerHTML = "";
    for (const p of aq.presets) {
      const b = document.createElement("button");
      const label = document.createElement("span");
      label.textContent = p.label;
      const rpm = document.createElement("small");
      rpm.textContent = `${p.rpm} RPM`;
      b.append(label, rpm);
      b.disabled = !aq.connected;
      b.classList.toggle("active", p.label === aq.active_preset);
      b.addEventListener("click", () => { setPending(b); post(`api/pump/preset/${p.index}`); });
      grid.appendChild(b);
    }
    $("rpm-form").querySelector("button").disabled = !aq.connected;

    const c = s.ha.chlorinator || {};
    $("chlorinator-card").hidden = !c.available;
    if (c.available) {
      const state = c.producing ? "Chlorinating" : (c.flow === false ? "No flow" : "Idle");
      $("chlor-state").textContent = state;
      $("chlor-state").classList.toggle("on", !!c.producing);
      $("chlor-output").textContent = c.efficiency != null ? `${Math.round(c.efficiency)}%` : "--";
      $("chlor-salt").textContent = `Salt ${c.salt ?? "--"}`;
      for (const b of document.querySelectorAll("[data-chlor]")) b.disabled = !s.ha.connected || c.efficiency == null;
    }
    const ch = s.ha.chemistry || {};
    $("chemistry-card").hidden = !ch.available;
    if (ch.available) {
      const tag = (el, alert) => {
        const a = (alert || "").toLowerCase();
        el.textContent = a === "ok" ? "OK" : a === "old" ? "old" : a ? a : "";
        el.className = "tag " + (a === "ok" ? "good" : a === "old" ? "stale" : a ? "warn" : "");
      };
      $("chem-fc").textContent = ch.free_chlorine != null ? `${ch.free_chlorine} ppm` : "--";
      tag($("chem-fc-tag"), ch.free_chlorine_alert);
      $("chem-ph").textContent = ch.ph != null ? `${ch.ph}` : "--";
      tag($("chem-ph-tag"), ch.ph_alert);
      const more = [];
      if (ch.alkalinity != null) more.push(`Alk ${Math.round(ch.alkalinity)}${(ch.alkalinity_alert || "").toLowerCase() === "old" ? " (old)" : ""}`);
      if (ch.cya != null) more.push(`CYA ${Math.round(ch.cya)}${(ch.cya_alert || "").toLowerCase() === "old" ? " (old)" : ""}`);
      if (ch.cassette_days != null) more.push(`Cassette ${Math.round(ch.cassette_days)} d`);
      $("chem-more").textContent = more.join(" · ");
      if (ch.last_measurement) {
        const d = new Date(ch.last_measurement);
        const sameDay = d.toDateString() === new Date().toDateString();
        $("chem-when").textContent = "Measured " + (sameDay ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : d.toLocaleDateString([], { month: "short", day: "numeric" }));
      } else $("chem-when").textContent = "";
    }
    $("ha-health").textContent = `HA: ${s.ha.connected ? "connected" : "disconnected"}` + (s.ha.connected && s.ha.panel_online === false ? " · panel offline" : "");
    $("aq-health").textContent = `iAqualink: ${aq.connected ? "connected" : (aq.error ? "error" : "disconnected")}`;
    const la = t.last_action;
    $("last-action").textContent = la ? ` · heater ${la.action} at ${Math.round(la.temp)}° (${la.time.slice(11, 16)})` : "";
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
    } else if (btn.dataset.chlor) {
      if (!state || !state.ha.chlorinator) return;
      const c = state.ha.chlorinator;
      const opts = c.output_options || [];
      const cur = Math.round(c.efficiency ?? 0);
      let i = opts.indexOf(cur);
      if (i < 0) i = opts.findIndex((v) => v >= cur);
      const next = opts[Math.max(0, Math.min(opts.length - 1, i + Number(btn.dataset.chlor)))];
      if (next === undefined || next === cur) return;
      setPending(btn);
      post("api/chlorinator/output", { percent: next });
    } else if (btn.dataset.thermo) {
      if (!state) return;  // no snapshot yet: nothing to step from
      const key = btn.dataset.thermo;
      const cur = state.thermostat.settings[key];
      setPending(btn);
      post("api/thermostat", { [key]: cur + Number(btn.dataset.delta) });
    }
  });

  $("thermo-settings-btn").addEventListener("click", () => {
    const panel = $("thermo-settings");
    panel.hidden = !panel.hidden;
    $("thermo-settings-btn").textContent = panel.hidden ? "Settings ▾" : "Settings ▴";
    $("thermo-settings-btn").setAttribute("aria-expanded", String(!panel.hidden));
  });

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

  let polling = false;
  let pollTimer = null;
  let reconnectTimer = null;

  function startPolling() {
    if (polling) return;
    polling = true;
    (async function tick() {
      try { render(await (await fetch("api/state")).json()); } catch (_) {}
      if (polling) pollTimer = setTimeout(tick, 5000);
    })();
  }
  function stopPolling() {
    polling = false;
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  function connect() {
    let es;
    try { es = new EventSource("api/events"); } catch (_) { return startPolling(); }
    es.onopen = () => stopPolling();
    es.onmessage = (ev) => { stopPolling(); render(JSON.parse(ev.data)); };
    es.onerror = () => {
      es.close();
      startPolling();
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 5000);
    };
  }
  fetch("api/state").then((r) => r.json()).then(render).catch(() => {});
  connect();
})();
