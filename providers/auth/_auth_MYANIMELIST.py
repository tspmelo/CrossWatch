# providers/auth/_auth_MYANIMELIST.py
# CrossWatch - MyAnimeList Auth Provider
# Copyright (c) 2025-2026 CrossWatch / Cenodude (https://github.com/cenodude/CrossWatch)
from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Mapping, MutableMapping
from typing import Any
from urllib.parse import urlencode

import requests

from ._auth_base import AuthManifest, AuthProvider, AuthStatus

from cw_platform.app_version import user_agent as http_user_agent
from cw_platform.provider_instances import ensure_instance_block, ensure_provider_block, normalize_instance_id, resolve_provider_block

try:
    from _logging import log as _real_log
except ImportError:
    _real_log = None

__VERSION__ = "0.1"

UA = http_user_agent(override_env="CW_MYANIMELIST_UA")
AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
API_BASE = "https://api.myanimelist.net/v2"
ME_URL = f"{API_BASE}/users/@me"
HTTP_TIMEOUT = 15
REFRESH_SKEW_SEC = 24 * 3600
FORCED_REFRESH_MIN_INTERVAL = 60.0

_REFRESH_LOCK = threading.Lock()
_LAST_FORCED_REFRESH: dict[str, float] = {}


class MALAuthError(RuntimeError):
    pass


def log(msg: str, *, level: str = "INFO", module: str = "AUTH", extra: dict[str, Any] | None = None) -> None:
    try:
        if callable(_real_log):
            _real_log(msg, level=level, module=module, extra=extra or {})
    except Exception:
        pass


def _blocks(cfg: Any, instance_id: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    inst = normalize_instance_id(instance_id)
    if not isinstance(cfg, dict):
        return inst, {}, {}
    base = ensure_provider_block(cfg, "myanimelist")
    a = ensure_instance_block(cfg, "myanimelist", inst)
    return inst, base, a


def _read(cfg: Mapping[str, Any], instance_id: Any) -> dict[str, Any]:
    return resolve_provider_block(cfg, "myanimelist", instance_id)


def new_verifier() -> str:
    # PKCE "plain": MAL only supports plain, so verifier == challenge (43-128 chars).
    return secrets.token_urlsafe(64)[:128]


def build_authorize_url(client_id: str, redirect_uri: str, state: str, verifier: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": verifier,
        "code_challenge_method": "plain",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _token_post(data: dict[str, str], client_secret: str) -> dict[str, Any]:
    if client_secret:
        data["client_secret"] = client_secret
    r = requests.post(TOKEN_URL, data=data, headers={"Accept": "application/json", "User-Agent": UA}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json() or {}
    if not str(j.get("access_token") or "").strip():
        raise MALAuthError("MyAnimeList token response has no access_token")
    return j


def exchange_code(code: str, *, client_id: str, client_secret: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
    return _token_post(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        client_secret,
    )


def apply_token(block: MutableMapping[str, Any], tok: Mapping[str, Any]) -> None:
    block["access_token"] = str(tok.get("access_token") or "").strip()
    block["refresh_token"] = str(tok.get("refresh_token") or block.get("refresh_token") or "").strip()
    try:
        expires_in = int(tok.get("expires_in") or 0)
    except Exception:
        expires_in = 0
    block["expires_at"] = int(time.time()) + expires_in if expires_in > 0 else 0


def fetch_user(access_token: str) -> dict[str, Any] | None:
    r = requests.get(ME_URL, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": UA}, timeout=HTTP_TIMEOUT)
    if not r.ok:
        return None
    j = r.json() or {}
    if not j.get("id"):
        return None
    return {"id": j.get("id"), "name": j.get("name")}


def about_to_expire(block: Mapping[str, Any], skew_sec: int = REFRESH_SKEW_SEC) -> bool:
    try:
        exp = int(block.get("expires_at") or 0)
    except Exception:
        exp = 0
    return bool(exp and exp - int(time.time()) <= skew_sec)


def _load_full_cfg() -> dict[str, Any]:
    from cw_platform.config_base import load_config

    cfg = load_config() or {}
    return cfg if isinstance(cfg, dict) else dict(cfg)


def refresh_token(cfg: Mapping[str, Any] | None = None, *, instance_id: Any = None, force: bool = False) -> dict[str, Any]:
    from cw_platform.config_base import save_config

    inst = normalize_instance_id(instance_id)
    # ponytail: one global lock, per-instance locks if many MAL accounts refresh at once
    with _REFRESH_LOCK:
        full = _load_full_cfg()
        block = ensure_instance_block(full, "myanimelist", inst)
        if not force and str(block.get("access_token") or "").strip() and not about_to_expire(block):
            return {"ok": True, "status": "fresh", "instance": inst}
        rt = str(block.get("refresh_token") or "").strip()
        cid = str(block.get("client_id") or "").strip()
        if not rt or not cid:
            return {"ok": False, "status": "missing_refresh", "instance": inst}
        try:
            tok = _token_post({"grant_type": "refresh_token", "client_id": cid, "refresh_token": rt}, str(block.get("client_secret") or "").strip())
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", 0)
            if code in (400, 401):
                block["access_token"] = ""
                block["refresh_token"] = ""
                save_config(full)
                log(f"MYANIMELIST: refresh rejected; reconnect required (instance={inst})", level="ERROR")
                return {"ok": False, "status": "invalid_grant", "instance": inst, "reconnect_required": True}
            return {"ok": False, "status": f"refresh_failed:{code}", "instance": inst}
        except Exception as e:
            return {"ok": False, "status": "network_error", "error": str(e), "instance": inst}
        apply_token(block, tok)
        save_config(full)
        if isinstance(cfg, dict) and isinstance(cfg.get("myanimelist"), dict):
            for k in ("access_token", "refresh_token", "expires_at"):
                cfg["myanimelist"][k] = block.get(k)
        log(f"MYANIMELIST: token refreshed (instance={inst})")
        return {"ok": True, "status": "ok", "instance": inst}


def _allow_forced_refresh(inst: str) -> bool:
    now = time.monotonic()
    with _REFRESH_LOCK:
        if now - _LAST_FORCED_REFRESH.get(inst, 0.0) < FORCED_REFRESH_MIN_INTERVAL:
            return False
        _LAST_FORCED_REFRESH[inst] = now
        return True


def _token_of(block: Mapping[str, Any]) -> str:
    return str(block.get("access_token") or "").strip()


def request_with_auth(
    session: requests.Session,
    method: str,
    url: str,
    *,
    block: MutableMapping[str, Any],
    instance_id: Any = None,
    timeout: float = HTTP_TIMEOUT,
    max_retries: int = 3,
    **kwargs: Any,
) -> requests.Response:
    """Send with the instance token; refresh when near expiry or once after a 401. `block` is updated in place."""
    from providers.sync._mod_common import request_with_retries

    inst = normalize_instance_id(instance_id)
    base_headers = dict(kwargs.pop("headers", None) or {})

    def _reload() -> None:
        fresh = ensure_instance_block(_load_full_cfg(), "myanimelist", inst)
        for k in ("access_token", "refresh_token", "expires_at"):
            block[k] = fresh.get(k)

    def _send() -> requests.Response:
        headers = dict(base_headers)
        headers["Authorization"] = f"Bearer {_token_of(block)}"
        return request_with_retries(session, method, url, timeout=timeout, max_retries=max_retries, headers=headers, **kwargs)

    if about_to_expire(block) and refresh_token(instance_id=inst).get("ok"):
        _reload()
    resp = _send()
    if resp.status_code != 401 or not _allow_forced_refresh(inst):
        return resp
    if not refresh_token(instance_id=inst, force=True).get("ok"):
        return resp
    _reload()
    return _send()


class MyAnimeListAuth(AuthProvider):
    name = "MYANIMELIST"

    def manifest(self) -> AuthManifest:
        return AuthManifest(
            name="MYANIMELIST",
            label="MyAnimeList",
            flow="oauth2",
            fields=[
                {"key": "myanimelist.client_id", "label": "Client ID", "type": "text", "required": True},
                {"key": "myanimelist.client_secret", "label": "Client Secret", "type": "password", "required": False},
            ],
            actions={"start": True, "finish": False, "refresh": True, "disconnect": True},
            notes="Authorize with MyAnimeList; you'll be redirected back to the app.",
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "features": {
                "watchlist": {"read": True, "write": True},
                "ratings": {"read": True, "write": True},
                "history": {"read": True, "write": True},
            }
        }

    def get_status(self, cfg: Mapping[str, Any], *, instance_id: Any = None) -> AuthStatus:
        s = _read(cfg, instance_id)
        tok = _token_of(s)
        user = s.get("user") or {}
        uname = user.get("name") if isinstance(user, Mapping) else None
        return AuthStatus(
            connected=bool(tok),
            label="MyAnimeList",
            user=str(uname) if uname else None,
            expires_at=int(s.get("expires_at") or 0) or None,
        )

    def start(self, cfg: MutableMapping[str, Any], redirect_uri: str, *, instance_id: Any = None, state: str = "") -> dict[str, Any]:
        _, _, s = _blocks(cfg, instance_id)
        verifier = new_verifier()
        s["_pkce_verifier"] = verifier
        url = build_authorize_url(str(s.get("client_id") or "").strip(), redirect_uri, state or secrets.token_urlsafe(16), verifier)
        log("MYANIMELIST: start OAuth", extra={"redirect_uri": redirect_uri})
        return {"url": url, "verifier": verifier}

    def finish(self, cfg: MutableMapping[str, Any], *, instance_id: Any = None, **payload: Any) -> AuthStatus:
        inst = instance_id if instance_id is not None else payload.get("instance_id")
        _, _, s = _blocks(cfg, inst)
        verifier = str(payload.get("verifier") or s.get("_pkce_verifier") or "").strip()
        tok = exchange_code(
            str(payload.get("code") or "").strip(),
            client_id=str(s.get("client_id") or "").strip(),
            client_secret=str(s.get("client_secret") or "").strip(),
            redirect_uri=str(payload.get("redirect_uri") or "").strip(),
            verifier=verifier,
        )
        apply_token(s, tok)
        s.pop("_pkce_verifier", None)
        try:
            user = fetch_user(s["access_token"])
        except Exception:
            user = None
        if user:
            s["user"] = user
        return self.get_status(cfg, instance_id=inst)

    def refresh(self, cfg: MutableMapping[str, Any], *, instance_id: Any = None) -> AuthStatus:
        try:
            refresh_token(cfg, instance_id=instance_id)
        except Exception:
            pass
        return self.get_status(_load_full_cfg(), instance_id=instance_id)

    def disconnect(self, cfg: MutableMapping[str, Any], *, instance_id: Any = None) -> AuthStatus:
        _, _, s = _blocks(cfg, instance_id)
        s["access_token"] = ""
        s["refresh_token"] = ""
        s["expires_at"] = 0
        s.pop("user", None)
        return self.get_status(cfg, instance_id=instance_id)


PROVIDER = MyAnimeListAuth()


def html() -> str:
    return r'''<div class="section" id="sec-myanimelist">
  <style>
    #sec-myanimelist .inline{display:flex;gap:8px;align-items:center}
    #sec-myanimelist .inline .msg{margin-left:auto;padding:8px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.04);color:#ddd;font-weight:600}
    #sec-myanimelist .inline .msg.ok{border-color:rgba(0,255,170,.18);background:rgba(0,255,170,.08);color:#b9ffd7}
    #sec-myanimelist .inline .msg.warn{border-color:rgba(255,210,0,.18);background:rgba(255,210,0,.08);color:#ffe9a6}
    #sec-myanimelist .inline .msg.hidden{display:none}
    #sec-myanimelist .btn.danger{background:#a8182e;border-color:rgba(255,107,107,.4)}
    #sec-myanimelist .btn.danger:hover{filter:brightness(1.08)}
    #sec-myanimelist #btn-connect-myanimelist{
      background: linear-gradient(135deg,#2e51a2,#1d3a7c);
      border-color: rgba(46,81,162,.55);
      box-shadow: 0 0 14px rgba(46,81,162,.4);
      color: #fff;
    }
    #sec-myanimelist #btn-connect-myanimelist:hover{filter:brightness(1.08);box-shadow: 0 0 18px rgba(46,81,162,.55)}
    #sec-myanimelist .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  </style>

  <div class="head" data-toggle-section="sec-myanimelist">
    <span class="chev"></span><strong>MyAnimeList</strong>
  </div>

  <div class="body">
    <div class="cw-panel">
      <div class="cw-meta-provider-panel active" data-provider="myanimelist">
        <div class="cw-panel-head">
          <div>
            <div class="cw-panel-title">MyAnimeList</div>
            <div class="muted">Connect your account and set API keys.</div>
          </div>
        </div>

        <div class="cw-subtiles" style="margin-top:2px">
          <button type="button" class="cw-subtile active" data-sub="auth">Authentication</button>
        </div>

        <div class="cw-subpanels">
          <div class="cw-subpanel active" data-sub="auth">
            <div class="cw-auth-journey" style="--cw-auth-c1:46,81,162;--cw-auth-c2:46,81,162;--cw-auth-logo:url('/assets/img/MYANIMELIST.svg')">
              <div class="cw-auth-journey-text">
                <div class="cw-auth-journey-title">Connect to MyAnimeList</div>
                <div class="cw-auth-journey-copy">Add your MyAnimeList Client ID (and Client Secret for "web" apps), then click Connect MyAnimeList and approve the request in the browser window.</div>
              </div>
            </div>

            <div class="grid2">
              <div>
                <label for="myanimelist_client_id">Client ID</label>
                <input id="myanimelist_client_id" name="myanimelist_client_id" placeholder="Your MyAnimeList client_id" autocomplete="off" spellcheck="false" autocapitalize="off" />
              </div>
              <div>
                <label for="myanimelist_client_secret">Client Secret (optional)</label>
                <input id="myanimelist_client_secret" name="myanimelist_client_secret" placeholder="Only for 'web' app type" type="password" autocomplete="off" spellcheck="false" autocapitalize="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" />
              </div>
            </div>

            <div id="myanimelist_hint" class="msg warn hidden">
              You need a MyAnimeList API client. Create one at
              <a href="https://myanimelist.net/apiconfig" target="_blank" rel="noopener">MyAnimeList API</a>.
              Set the App Redirect URL to <code id="redirect_uri_preview_myanimelist"></code>.
              <button id="btn-copy-myanimelist-redirect" class="btn" type="button" style="margin-left:8px">Copy Redirect URL</button>
            </div>

            <div class="inline" style="margin-top:10px">
              <button class="btn" id="btn-connect-myanimelist" type="button">Connect MyAnimeList</button>
              <button class="btn danger" id="btn-delete-myanimelist" type="button">Disconnect</button>
              <span class="msg ok hidden" id="myanimelist_msg" role="status" aria-live="polite"></span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
'''
