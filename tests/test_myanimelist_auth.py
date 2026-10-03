# tests/test_myanimelist_auth.py
# CrossWatch test scripts
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

import providers.auth._auth_MYANIMELIST as mal


def test_authorize_url_uses_plain_pkce() -> None:
    q = parse_qs(urlparse(mal.build_authorize_url("cid", "http://h/callback/myanimelist", "st", "v" * 50)).query)
    assert q["code_challenge"] == ["v" * 50] and q["code_challenge_method"] == ["plain"] and q["state"] == ["st"]


@responses.activate
def test_finish_stores_tokens_expiry_and_user() -> None:
    responses.post(mal.TOKEN_URL, json={"access_token": "at", "refresh_token": "rt", "expires_in": 2678400})
    responses.get(mal.ME_URL, json={"id": 7, "name": "neo"})
    cfg: dict[str, Any] = {"myanimelist": {"client_id": "cid"}}
    status = mal.PROVIDER.finish(cfg, code="c", redirect_uri="http://h/callback/myanimelist", verifier="v" * 50)
    blk = cfg["myanimelist"]
    assert status.connected and status.user == "neo"
    assert blk["access_token"] == "at" and blk["refresh_token"] == "rt" and blk["expires_at"] > 0
    body = parse_qs(responses.calls[0].request.body)
    assert body["code_verifier"] == ["v" * 50] and "client_secret" not in body


@responses.activate
def test_request_refreshes_once_after_401(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, Any] = {"myanimelist": {"client_id": "cid", "access_token": "old", "refresh_token": "rt"}}
    monkeypatch.setattr(mal, "_load_full_cfg", lambda: store)
    monkeypatch.setattr("cw_platform.config_base.save_config", lambda cfg: None)
    mal._LAST_FORCED_REFRESH.clear()

    url = f"{mal.API_BASE}/users/@me"
    responses.get(url, status=401)
    responses.get(url, json={"id": 1})
    responses.post(mal.TOKEN_URL, json={"access_token": "new", "refresh_token": "rt2", "expires_in": 3600})

    block = dict(store["myanimelist"])
    r = mal.request_with_auth(requests.Session(), "GET", url, block=block, max_retries=1)
    assert r.status_code == 200
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer new"
    assert block["access_token"] == "new" and store["myanimelist"]["refresh_token"] == "rt2"
