"""The JavaScript that toggles Steam's global frame limit, run in Steam's SharedJSContext.

Called as ``(SRC)({"action": "toggle", "limitFps": N, "maxFps": M, "budgetMs": B, "notAfter": T})
.then(JSON.stringify)`` over Steam's remote debugging (cdp.py). It writes only while Steam's default performance profile
(gameid 769) is active, with the same setters Quick Access uses, so Quick Access shows the change:
- legacy Quick Access ("Framerate Limit", msgLimits.disable_refresh_rate_management false):
  SystemPerfStore's per-app fps_limit (fps_limit_external on an external display) through
  SteamClient.System.Perf.UpdateSettings;
- unified Quick Access ("Frame Limit"): the client settings gamescope_app_target_framerate and
  gamescope_disable_framelimit through settingsStore.GetClientSetting(name)[1].
It never throws, writes only after its first await (so a DevTools timeout cannot cut a write
short), stays within budgetMs, and returns {v: 1, ok, wrote, changed, confirmed, on, ...}; with
ok false and wrote false nothing was changed. notAfter is a wall-clock time in ms (Date.now()):
a script that only starts running by then, because Steam's UI was busy and the caller has given
up, ends with error "too late" and writes nothing, and every wait ends by then.

Checked against the Steam client UI of October 2026 (steamui build 11123049) and its webpack
modules in a test harness, not on a device.
"""

STEAM_FPS_TOGGLE_JS = r'''
async (OPTS) => {
  // Toggle the frame limit of Steam's DEFAULT perf profile (gameid 769) so Quick Access shows it.
  // Never throws or rejects; resolves to a plain object. Nothing is written unless the default profile is active.
  const DEFAULT_PROFILE = "769";
  const o = Object.assign({ action: "toggle", limitFps: 0, maxFps: 0, budgetMs: 1800, notAfter: 0 }, OPTS || {});
  const t0 = Date.now();
  // notAfter: the caller's wall-clock deadline; a run that starts late must not write after the caller gave up.
  const notAfter = Number(o.notAfter) > 0 ? Number(o.notAfter) : Infinity;
  const late = () => Date.now() >= notAfter;
  const left = () => Math.max(0, Math.min(Number(o.budgetMs) - (Date.now() - t0), notAfter - Date.now()));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const TO = {};
  const within = (p, ms) => Promise.race([Promise.resolve(p), sleep(Math.max(0, Math.min(ms, left()))).then(() => TO)]);
  const nums = (a) => { try { return Array.from(a || [], Number).filter(Number.isFinite); } catch (e) { return []; } };
  const nearest = (want, c) => c.reduce((b, v) => (b == null || Math.abs(v - want) < Math.abs(b - want) || (Math.abs(v - want) === Math.abs(b - want) && v < b)) ? v : b, null);
  const out = { v: 1, ok: false, wrote: false, changed: false, confirmed: false, action: String(o.action), mode: null, warnings: [] };
  const done = (extra) => { Object.assign(out, extra || {}); out.ms = Date.now() - t0; try { return JSON.parse(JSON.stringify(out)); } catch (e) { return { v: 1, ok: false, wrote: out.wrote, error: "unserialisable result" }; } };
  try {
    if (!["toggle", "on", "off", "status"].includes(out.action)) return done({ error: "bad action" });
    // webpack require: only used when a store singleton does not exist yet
    let req;
    const getReq = () => {
      if (req !== undefined) return req;
      req = null;
      try {
        const chunks = window.webpackChunksteamui;
        const entry = [[Symbol("oxp3-tool")], {}, (r) => { req = r; }];
        chunks.push(entry);
        const i = chunks.lastIndexOf(entry);
        if (i >= 0) chunks.splice(i, 1);
      } catch (e) { req = null; }
      return req;
    };
    const findExport = (needle, pred) => {
      const r = getReq();
      if (!r || !r.m) return null;
      for (const id of Object.keys(r.m)) {
        let src;
        try { src = Function.prototype.toString.call(r.m[id]); } catch (e) { continue; }
        if (src.indexOf(needle) < 0) continue;
        let ex;
        try { ex = r(id); } catch (e) { continue; }
        for (const k of Object.keys(ex || {})) { try { if (pred(ex[k])) return ex[k]; } catch (e) { /* next */ } }
      }
      return null;
    };
    // ---- SystemPerfStore (window.SystemPerfStore is set by its own Get()) ----
    let ps = window.SystemPerfStore;
    if (!ps) {
      const C = findExport("window.SystemPerfStore=", (v) => typeof v === "function" && typeof v.Get === "function" && v.prototype && typeof v.prototype.CreateSettingsUpdateRequest === "function");
      try { ps = C ? C.Get() : null; } catch (e) { ps = null; }
      if (ps) out.warnings.push("SystemPerfStore created");
    }
    if (!ps || typeof ps.CreateSettingsUpdateRequest !== "function") return done({ error: "SystemPerfStore unavailable" });
    for (const end = Date.now() + Math.min(1000, left()); !(ps.msgState && ps.msgState.settings && ps.msgState.limits) && Date.now() < end;) await sleep(50);
    await sleep(0); // everything below runs outside Runtime.evaluate's synchronous timeout window
    const st = ps.msgState || {};
    if (!st.settings || !st.limits) return done({ error: "perf state not received yet" });
    const limits = st.limits;
    const activeId = () => String((ps.msgState || {}).active_profile_game_id ?? "");
    out.activeProfileGameId = activeId();
    out.currentGameId = String(st.current_game_id ?? "");
    out.perGameProfileActive = out.activeProfileGameId !== DEFAULT_PROFILE;
    out.mode = limits.disable_refresh_rate_management ? "unified" : "legacy";
    if (out.activeProfileGameId === "" || out.activeProfileGameId === "0") return done({ error: "no active perf profile yet" });
    const blocked = out.perGameProfileActive;
    const wantLimit = (limited) => out.action === "on" ? true : out.action === "off" ? false : !limited;

    if (out.mode === "legacy") {
      // ---- legacy: per-app fps_limit / fps_limit_external of the active profile (Quick Access "Framerate Limit") ----
      let dm = window.SystemDisplayManagerStore, dms = null;
      if (!dm) {
        const C = findExport("window.SystemDisplayManagerStore=", (v) => typeof v === "function" && typeof v.Get === "function" && v.prototype && typeof v.prototype.GetState === "function" && typeof v.prototype.SetMode === "function");
        try { dm = C ? C.Get() : null; } catch (e) { dm = null; }
      }
      try { if (dm && typeof dm.GetState === "function") { const r = await within(dm.GetState(), 500); if (r !== TO) dms = r; } } catch (e) { dms = null; }
      const ds = dms && Array.isArray(dms.displays) ? dms.displays : [];
      const disp = ds.find((d) => d.is_primary === true) || ds.find((d) => d.is_enabled === true);
      const internal = disp ? disp.is_internal : undefined;
      out.external = (internal == null || !dms || dms.is_mode_switching_supported !== true)
        ? !!(window.settingsStore && window.settingsStore.settings && window.settingsStore.settings.bDisplayIsExternal)
        : !internal;
      const field = out.external ? "fps_limit_external" : "fps_limit";
      const options = nums(out.external ? limits.fps_limit_options_external : limits.fps_limit_options);
      const pos = options.filter((v) => v > 0);
      const maxOpt = pos.length ? Math.max(...pos) : 0;
      const read = () => Number((ps.msgSettingsPerApp || {})[field] ?? 0);
      const isLimited = (v) => v > 0 && (maxOpt === 0 || v < maxOpt);
      const cur = read();
      Object.assign(out, { field, options, before: cur, limitedBefore: isLimited(cur) });
      if (out.action === "status") return done({ ok: true });
      if (blocked) return done({ error: "per-game profile active" });
      const on = wantLimit(out.limitedBefore);
      out.on = on;
      let target;
      if (!on) target = options.includes(0) ? 0 : maxOpt;
      else {
        const cands = pos.filter((v) => v < maxOpt);
        if (!cands.length) return done({ error: "no frame limit below the refresh rate in fps_limit_options" });
        const want = Math.round(Number(o.limitFps)) > 0 ? Math.round(Number(o.limitFps)) : Math.max(...cands);
        if (want >= maxOpt) return done({ error: "limitFps " + want + " is not below " + maxOpt });
        target = cands.includes(want) ? want : nearest(want, cands);
        if (target !== want) out.warnings.push("limitFps " + want + " not offered; using " + target);
      }
      if (!on && !options.includes(0) && maxOpt === 0) return done({ error: "no off value in fps_limit_options" });
      out.target = target;
      if (target === cur) return done({ ok: true, confirmed: true, after: cur, limitedAfter: out.limitedBefore });
      const perf = window.SteamClient && SteamClient.System && SteamClient.System.Perf;
      if (!perf || typeof perf.UpdateSettings !== "function") return done({ error: "SteamClient.System.Perf.UpdateSettings unavailable" });
      if (activeId() !== DEFAULT_PROFILE) return done({ error: "active profile changed; nothing written" });
      if (late()) return done({ error: "too late" });
      const rq = ps.CreateSettingsUpdateRequest(DEFAULT_PROFILE);
      const pa = rq.settings_delta().per_app(true);
      if (out.external) pa.set_fps_limit_external(target); else pa.set_fps_limit(target);
      const b64 = rq.serializeBase64String();
      out.wrote = true;
      try { await within(perf.UpdateSettings(b64), 500); } catch (e) { return done({ error: "UpdateSettings threw: " + String(e && e.message || e) }); }
      while (read() !== target && left() > 0) await sleep(50);
      const after = read();
      return done({ ok: true, changed: true, confirmed: after === target, after, limitedAfter: isLimited(after) });
    }

    // ---- unified: client settings gamescope_app_target_framerate / gamescope_disable_framelimit (Quick Access "Frame Limit").
    // Both are PerGamePerDisplay settings, stored in the slot of the ACTIVE perf profile, so the same guard applies.
    const ss = window.settingsStore;
    if (!ss || typeof ss.GetClientSetting !== "function" || !ss.clientSettings) return done({ error: "settingsStore unavailable" });
    const get = (k) => ss.clientSettings[k];
    let rates = [];
    const GS = findExport("Gamescope.GetState#1", (v) => v && typeof v === "object" && typeof v.GetState === "function" && typeof v.SetBlurParams === "function");
    if (GS) {
      try {
        const r = await within(GS.GetState({}), 500);
        if (r !== TO && r && typeof r.BSuccess === "function" && r.BSuccess()) {
          const g = (r.Body().toObject() || {}).state || {};
          const adi = g.active_display_info || {};
          const range = (a, b) => Array.from({ length: Math.max(0, b - a + 1) }, (_, i) => a + i);
          rates = nums(adi.supported_frame_rates);
          if (!rates.length) rates = range(10, 60);
          if ((ps.msgSettingsPerApp || {}).is_vrr_enabled) rates = range(rates[0], rates[rates.length - 1]);
          out.appRefreshSupported = !!g.is_app_refresh_rate_supported;
          out.displayExternal = !!adi.is_external;
        } else out.warnings.push("Gamescope.GetState: no answer");
      } catch (e) { out.warnings.push("Gamescope.GetState failed"); }
    } else out.warnings.push("GamescopeService not found");
    const maxFps = rates.length ? rates[rates.length - 1] : (Math.round(Number(o.maxFps)) > 0 ? Math.round(Number(o.maxFps)) : 0);
    const readU = () => ({ fps: Number(get("gamescope_app_target_framerate") ?? 0), disabled: !!get("gamescope_disable_framelimit") });
    const isLimitedU = (s) => maxFps ? (!s.disabled && s.fps > 0 && s.fps < maxFps) : null;
    const b = readU();
    Object.assign(out, { field: "gamescope_app_target_framerate", options: rates, maxFps, before: b, limitedBefore: isLimitedU(b) });
    if (out.action === "status") return done({ ok: true });
    if (blocked) return done({ error: "per-game profile active" });
    if (!maxFps) return done({ error: "unknown maximum frame rate; pass maxFps" });
    const on = wantLimit(out.limitedBefore);
    out.on = on;
    let target;
    if (!on) target = maxFps;
    else {
      const cands = (rates.length ? rates : []).filter((v) => v > 0 && v < maxFps);
      const want = Math.round(Number(o.limitFps)) > 0 ? Math.round(Number(o.limitFps)) : Math.round(maxFps / 2);
      if (want >= maxFps) return done({ error: "limitFps " + want + " is not below " + maxFps });
      target = !cands.length || cands.includes(want) ? want : nearest(want, cands);
      if (target !== want) out.warnings.push("limitFps " + want + " not offered; using " + target);
    }
    out.target = target;
    const writes = [];
    if (b.fps !== target) writes.push(["gamescope_app_target_framerate", target]);
    if (on && b.disabled) writes.push(["gamescope_disable_framelimit", false]);
    if (!writes.length) return done({ ok: true, confirmed: true, after: b, limitedAfter: out.limitedBefore });
    if (!(window.SteamClient && SteamClient.Settings && typeof SteamClient.Settings.SetSetting === "function")) return done({ error: "SteamClient.Settings.SetSetting unavailable" });
    if (activeId() !== DEFAULT_PROFILE) return done({ error: "active profile changed; nothing written" });
    if (late()) return done({ error: "too late" });
    // No checks between the two writes: stopping after the first would leave the limit half set.
    for (const [k, v] of writes) {
      const setter = ss.GetClientSetting(k)[1];
      out.wrote = true;
      try { await within(setter(v), 500); } catch (e) { return done({ error: "SetSetting threw: " + String(e && e.message || e) }); }
    }
    const good = () => { const s = readU(); return s.fps === target && (!on || !s.disabled); };
    while (!good() && left() > 0) await sleep(50);
    const a = readU();
    return done({ ok: true, changed: true, confirmed: good(), after: a, limitedAfter: isLimitedU(a) });
  } catch (e) {
    return done({ ok: false, error: "exception: " + String(e && e.message || e) });
  }
}
'''
