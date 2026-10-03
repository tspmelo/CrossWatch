// auth.myanimelist.js - MyAnimeList auth (instance-aware)
(function (w, d) {
  "use strict";

  const Shared = w.CW.AuthShared;
  const $ = Shared.el;
  const notify = Shared.notify;
  const bust = () => `?ts=${Date.now()}`;
  const profile = Shared.createProfileAdapter({
    provider: "myanimelist",
    configKey: "myanimelist",
    label: "MyAnimeList",
    sectionId: "sec-myanimelist",
    selectId: "myanimelist_instance",
    storageKey: "cw.ui.myanimelist.auth.instance.v1",
    title: "Select which MyAnimeList account this config applies to.",
  });

  let connected = false;
  let pollHandle = null;

  const api = (path) => (profile ? profile.api(path) : String(path || ""));
  const cfgBlock = (cfg) => (profile ? profile.cfgBlock(cfg, true) : {});
  const computeRedirect = () => location.origin + "/callback/myanimelist";

  function setMyAnimeListSuccess(on, txt) {
    connected = !!on;
    try { Shared.setConnectLocked("btn-connect-myanimelist", connected); } catch {}
    return Shared.setStatusPill("myanimelist_msg", on ? "ok" : (txt ? "warn" : null), txt || (on ? "Connected" : ""));
  }

  function updateMyAnimeListButtonState() {
    profile?.ensureUI(async () => {
      await hydrateFromConfig(true);
      updateMyAnimeListButtonState();
    });
    const ok = Shared.readSecretField($("myanimelist_client_id")).hasValue;
    const btn = $("btn-connect-myanimelist");
    const rid = $("redirect_uri_preview_myanimelist");
    if (rid && rid.textContent !== computeRedirect()) rid.textContent = computeRedirect();
    try { Shared.setConnectLocked("btn-connect-myanimelist", connected); } catch {}
    if (btn) btn.disabled = !ok || btn.classList.contains("busy");
    $("myanimelist_hint")?.classList.toggle("hidden", ok);
  }

  async function hydrateFromConfig(force = false) {
    try {
      const cfg = await fetch("/api/config" + bust(), { cache: "no-store" }).then((r) => (r.ok ? r.json() : null));
      if (!cfg) return;
      const blk = cfgBlock(cfg);
      for (const key of ["client_id", "client_secret"]) {
        const el = $("myanimelist_" + key);
        if (el && (force || !el.value || el.dataset.masked === "1")) Shared.markSecretField(el, String(blk[key] || "").trim());
      }
      setMyAnimeListSuccess(!!String(blk.access_token || "").trim(), "");
      updateMyAnimeListButtonState();
    } catch {}
  }

  async function startMyAnimeList() {
    try { setMyAnimeListSuccess(false, ""); } catch {}
    const cidState = Shared.readSecretField($("myanimelist_client_id"));
    const secState = Shared.readSecretField($("myanimelist_client_secret"));
    if (!cidState.hasValue) return;

    let authCompleted;
    try { authCompleted = await Shared.captureAuthCompletion(profile); }
    catch (e) { notify(e.message); return; }

    const payload = {};
    if (cidState.value) payload.client_id = cidState.value;
    if (secState.value) payload.client_secret = secState.value;
    if (Object.keys(payload).length) {
      try {
        await fetch(api("/api/myanimelist/save"), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), cache: "no-store" });
      } catch {}
    }

    const j = await fetch(api("/api/myanimelist/authorize"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin: location.origin }),
      cache: "no-store",
    }).then((r) => r.json()).catch(() => null);
    if (!j?.ok || !j.authorize_url) {
      if (j?.error) notify(j.error);
      return;
    }
    w.open(j.authorize_url, "_blank");

    if (pollHandle) clearTimeout(pollHandle);
    const deadline = Date.now() + 120000;
    const back = [1000, 2500, 5000, 7500, 10000, 15000, 20000];
    let i = 0;
    const poll = async () => {
      if (Date.now() >= deadline) { pollHandle = null; return; }
      if (d.hidden) { pollHandle = setTimeout(poll, 5000); return; }
      let cfg = null;
      try { cfg = await fetch("/api/config" + bust(), { cache: "no-store" }).then((r) => r.json()); } catch {}
      if (String(cfgBlock(cfg || {})?.access_token || "").trim() && authCompleted(cfg)) {
        setMyAnimeListSuccess(true);
        pollHandle = null;
        try { w.dispatchEvent(new CustomEvent("auth-changed")); } catch {}
        return;
      }
      pollHandle = setTimeout(poll, back[Math.min(i++, back.length - 1)]);
    };
    pollHandle = setTimeout(poll, 1000);
  }

  async function myanimelistDeleteToken() {
    const btn = $("btn-delete-myanimelist");
    if (btn) { btn.disabled = true; btn.classList.add("busy"); }
    try {
      const r = await fetch(api("/api/myanimelist/token/delete"), { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", cache: "no-store" });
      const j = await r.json().catch(() => ({}));
      if (Shared.reportProviderUsage({ status: r.status, data: j })) return;
      if (r.ok && j.ok !== false) {
        notify("MyAnimeList disconnected");
        try { w.dispatchEvent(new CustomEvent("auth-changed")); } catch {}
        setMyAnimeListSuccess(false, "Disconnected");
      } else {
        setMyAnimeListSuccess(false, "Could not disconnect");
      }
    } catch {
      setMyAnimeListSuccess(false, "Could not disconnect");
    } finally {
      if (btn) { btn.disabled = false; btn.classList.remove("busy"); }
      updateMyAnimeListButtonState();
    }
  }

  function initMyAnimeListAuthUI() {
    for (const id of ["myanimelist_client_id", "myanimelist_client_secret"]) {
      const el = $(id);
      if (el && !el.__cwBound) { Shared.wireSecretInput(el, { onInput: updateMyAnimeListButtonState }); el.__cwBound = true; }
    }
    const wire = (id, fn) => { const el = $(id); if (el && !el.__wired) { el.addEventListener("click", fn); el.__wired = true; } };
    wire("btn-copy-myanimelist-redirect", () => Shared.copyText(computeRedirect(), $("btn-copy-myanimelist-redirect"), { successMessage: "Redirect URL copied" }));
    wire("btn-connect-myanimelist", startMyAnimeList);
    wire("btn-delete-myanimelist", myanimelistDeleteToken);
    updateMyAnimeListButtonState();
  }

  let initDone = false;
  function initMyAnimeListAuthLoader() {
    try { initMyAnimeListAuthUI(); } catch (_) {}
    if (initDone) return;
    initDone = true;
    try { hydrateFromConfig(true); } catch (_) {}
  }

  if (d.readyState === "loading") d.addEventListener("DOMContentLoaded", initMyAnimeListAuthLoader, { once: true });
  else initMyAnimeListAuthLoader();

  w.cwAuth = w.cwAuth || {};
  w.cwAuth.myanimelist = { init: initMyAnimeListAuthLoader };
  Object.assign(w, { setMyAnimeListSuccess, updateMyAnimeListButtonState, initMyAnimeListAuthUI, startMyAnimeList, myanimelistDeleteToken });
})(window, document);
