# /providers/sync/_mod_MYANIMELIST.py
# CrossWatch MyAnimeList module
# Copyright (c) 2025-2026 CrossWatch / Cenodude (https://github.com/cenodude/CrossWatch)

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from ._mod_common import SimpleRateLimiter, build_session, make_snapshot_progress, safe_json
from ._log import log as cw_log
from cw_platform.app_version import user_agent as http_user_agent
from cw_platform.id_map import canonical_key, minimal as id_minimal
from cw_platform.provider_instances import normalize_instance_id
from providers.auth._auth_MYANIMELIST import API_BASE, MALAuthError, request_with_auth

__VERSION__ = "0.1"
__all__ = ["get_manifest", "MYANIMELISTModule", "OPS"]

UA = os.environ.get("CW_MYANIMELIST_UA") or http_user_agent("MyAnimeList", override_env="CW_MYANIMELIST_UA")
LIST_FIELDS = "list_status{start_date,finish_date},media_type,start_season,num_episodes,alternative_titles"
DEFAULT_RATES = {"GET": 2.0, "PATCH": 1.0, "DELETE": 1.0}


if "ctx" not in globals():
    class _NullCtx:
        def emit(self, *args: Any, **kwargs: Any) -> None:
            pass

    ctx = _NullCtx()  # type: ignore[assignment]


def _feature_import(name: str) -> Any:
    try:
        return __import__(f"providers.sync.myanimelist._{name}", fromlist=["_"])
    except Exception as e:
        cw_log("MYANIMELIST", "module", "warn", "feature_import_failed", import_feature=name, error_type=e.__class__.__name__, error=str(e))
        return None


FEATURES: dict[str, Any] = {name: _feature_import(name) for name in ("watchlist", "ratings", "history")}


class MALError(RuntimeError):
    pass


def label_mal(method: str, url: str, kw: Mapping[str, Any]) -> str:
    path = urlparse(url).path
    if path.endswith("/users/@me"):
        return "viewer"
    if path.endswith("/users/@me/animelist"):
        return "list:index"
    if path.endswith("/my_list_status"):
        return "list:remove" if method == "DELETE" else "list:save"
    if path.rstrip("/").endswith("/anime"):
        return "search"
    return "rest"


def _instance_id(cfg: Mapping[str, Any]) -> str:
    if cfg.get("_cw_provider_instance") is not None:
        return normalize_instance_id(cfg.get("_cw_provider_instance"))
    for prefix in ("CW_PROBE", "CW_PAIR_SRC", "CW_PAIR_DST"):
        name = "CW_PROBE_PROVIDER" if prefix == "CW_PROBE" else prefix
        if str(os.getenv(name) or "").upper().strip() == "MYANIMELIST":
            return normalize_instance_id(os.getenv(f"{prefix}_INSTANCE"))
    return "default"


def _token_of(block: Mapping[str, Any]) -> str:
    return str(block.get("access_token") or "").strip()


@dataclass
class MALConfig:
    timeout: float = 15.0
    max_retries: int = 3


class MALClient:
    def __init__(self, cfg: MALConfig, block: dict[str, Any], instance_id: str):
        self.cfg = cfg
        self.block = block
        self.instance_id = instance_id
        self.session = build_session("MYANIMELIST", ctx, feature_label=label_mal)
        rates = dict(DEFAULT_RATES)
        rl = block.get("rate_limit")
        if isinstance(rl, Mapping):
            for k, v in rl.items():
                if str(k).upper() in rates:
                    rates[str(k).upper()] = v
        try:
            self.session._rate_limiter = SimpleRateLimiter(rates_per_sec=rates)
        except Exception:
            pass
        self.session.headers.update({"Accept": "application/json", "User-Agent": UA})
        self._list_cache: list[dict[str, Any]] | None = None

    def request(self, method: str, path: str, **kw: Any) -> Any:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        r = request_with_auth(
            self.session, method, url,
            block=self.block, instance_id=self.instance_id,
            timeout=self.cfg.timeout, max_retries=self.cfg.max_retries, **kw,
        )
        if r.status_code == 401:
            raise MALAuthError("MyAnimeList unauthorized")
        if r.status_code == 404 and method == "DELETE":
            return {}
        if r.status_code >= 400:
            raise MALError(f"MyAnimeList http:{r.status_code}")
        return safe_json(r) or {}

    def animelist(self) -> list[dict[str, Any]]:
        """Full user list, fetched once per adapter (all features read the same endpoint)."""
        if self._list_cache is not None:
            return self._list_cache
        rows: list[dict[str, Any]] = []
        url: str | None = "/users/@me/animelist"
        params: dict[str, Any] | None = {"fields": LIST_FIELDS, "limit": 1000, "nsfw": "true"}
        while url:
            data = self.request("GET", url, params=params)
            rows.extend(x for x in data.get("data") or [] if isinstance(x, Mapping))
            nxt = (data.get("paging") or {}).get("next")
            url, params = (str(nxt), None) if nxt else (None, None)
        self._list_cache = rows
        return rows

    def list_entry(self, mal_id: int) -> dict[str, Any] | None:
        for row in self.animelist():
            node = row.get("node") or {}
            if node.get("id") == mal_id:
                return row
        return None

    def save(self, mal_id: int, **fields: Any) -> dict[str, Any]:
        res = self.request("PATCH", f"/anime/{int(mal_id)}/my_list_status", data={k: v for k, v in fields.items() if v is not None})
        row = self.list_entry(mal_id)
        if row is not None:
            row["list_status"] = {**(row.get("list_status") or {}), **res}
        elif self._list_cache is not None:
            self._list_cache.append({"node": {"id": int(mal_id)}, "list_status": dict(res)})
        return res

    def delete(self, mal_id: int) -> None:
        self.request("DELETE", f"/anime/{int(mal_id)}/my_list_status")
        if self._list_cache is not None:
            self._list_cache = [r for r in self._list_cache if (r.get("node") or {}).get("id") != mal_id]

    def search(self, q: str) -> list[dict[str, Any]]:
        q = q.strip()[:64]
        if len(q) < 3:
            return []
        data = self.request("GET", "/anime", params={"q": q, "limit": 10, "fields": "media_type,start_season,alternative_titles", "nsfw": "true"})
        return [x.get("node") for x in data.get("data") or [] if isinstance(x, Mapping) and isinstance(x.get("node"), Mapping)]


def supported_features() -> dict[str, bool]:
    return {"watchlist": bool(FEATURES["watchlist"]), "ratings": bool(FEATURES["ratings"]), "history": bool(FEATURES["history"]), "playlists": False}


def _capabilities() -> dict[str, Any]:
    return {
        "bidirectional": True,
        "provides_ids": True,
        "index_semantics": "present",
        "observed_deletes": False,
        "ratings": {
            "types": {"movies": True, "shows": True, "seasons": False, "episodes": False},
            "upsert": True,
            "unrate": True,
            "from_date": False,
        },
    }


def get_manifest() -> Mapping[str, Any]:
    return {
        "name": "MYANIMELIST",
        "label": "MyAnimeList",
        "version": __VERSION__,
        "type": "sync",
        "bidirectional": True,
        "features": supported_features(),
        "requires": [],
        "capabilities": _capabilities(),
    }


class MYANIMELISTModule:
    def __init__(self, cfg: Mapping[str, Any]):
        block = dict(cfg.get("myanimelist") or {})
        if not _token_of(block):
            raise MALError("MYANIMELIST requires access_token")
        self.cfg = MALConfig(
            timeout=float(block.get("timeout", cfg.get("timeout", 15.0))),
            max_retries=int(block.get("max_retries", cfg.get("max_retries", 3))),
        )
        self.client = MALClient(self.cfg, block, _instance_id(cfg))
        self.raw_cfg = cfg
        self.progress_factory = (
            lambda feature, total=None, throttle_ms=300: make_snapshot_progress(
                ctx, dst="MYANIMELIST", feature=str(feature), total=total, throttle_ms=int(throttle_ms)
            )
        )

    def manifest(self) -> Mapping[str, Any]:
        return get_manifest()

    def health(self) -> Mapping[str, Any]:
        start = time.perf_counter()
        status, reason = "down", None
        try:
            self.client.request("GET", "/users/@me")
            status = "ok"
        except MALAuthError:
            status, reason = "auth_failed", "unauthorized"
        except Exception as e:
            reason = f"exception:{e.__class__.__name__}"
        ok = status == "ok"
        latency_ms = int((time.perf_counter() - start) * 1000)
        cw_log("MYANIMELIST", "health", "info", "health", latency_ms=latency_ms, ok=ok, status=status)
        return {
            "ok": ok,
            "status": status,
            "latency_ms": latency_ms,
            "features": {k: bool(v and ok) for k, v in supported_features().items()},
            "details": {"reason": reason} if reason else None,
        }

    @staticmethod
    def normalize(obj: Any) -> dict[str, Any]:
        return id_minimal(obj)

    @staticmethod
    def key_of(obj: Any) -> str:
        return canonical_key(id_minimal(obj)) or ""

    def feature_names(self) -> tuple[str, ...]:
        return tuple(k for k, v in supported_features().items() if v)

    def _feature(self, feature: str) -> Any:
        return FEATURES.get(str(feature or "").strip().lower())

    def build_index(self, feature: str, **kwargs: Any) -> dict[str, dict[str, Any]]:
        mod = self._feature(feature)
        if not mod:
            return {}
        return mod.build_index(self)

    def _write(self, op: str, feature: str, items: Iterable[Mapping[str, Any]], dry_run: bool) -> dict[str, Any]:
        lst = list(items or [])
        if not lst:
            return {"ok": True, "count": 0}
        if dry_run:
            return {"ok": True, "count": len(lst), "dry_run": True}
        mod = self._feature(feature)
        if not mod:
            return {"ok": True, "count": 0, "unresolved": []}
        res = getattr(mod, op)(self, lst)
        confirmed = list(res.get("confirmed_keys") or [])
        return {
            "ok": True,
            "count": len(confirmed),
            "confirmed": len(confirmed),
            "confirmed_keys": confirmed,
            "skipped_keys": list(res.get("skipped_keys") or []),
            "unresolved": list(res.get("unresolved") or []),
        }

    def add(self, feature: str, items: Iterable[Mapping[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
        return self._write("add", feature, items, dry_run)

    def remove(self, feature: str, items: Iterable[Mapping[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
        return self._write("remove", feature, items, dry_run)


class _MYANIMELISTOPS:
    def name(self) -> str:
        return "MYANIMELIST"

    def label(self) -> str:
        return "MyAnimeList"

    def features(self) -> Mapping[str, bool]:
        return supported_features()

    def capabilities(self) -> Mapping[str, Any]:
        return _capabilities()

    def is_configured(self, cfg: Mapping[str, Any]) -> bool:
        return bool(_token_of((cfg or {}).get("myanimelist") or {}))

    def _adapter(self, cfg: Mapping[str, Any]) -> MYANIMELISTModule:
        return MYANIMELISTModule(cfg)

    def build_index(self, cfg: Mapping[str, Any], *, feature: str) -> Mapping[str, dict[str, Any]]:
        return self._adapter(cfg).build_index(feature)

    def add(self, cfg: Mapping[str, Any], items: Iterable[Mapping[str, Any]], *, feature: str, dry_run: bool = False) -> dict[str, Any]:
        return self._adapter(cfg).add(feature, items, dry_run=dry_run)

    def remove(self, cfg: Mapping[str, Any], items: Iterable[Mapping[str, Any]], *, feature: str, dry_run: bool = False) -> dict[str, Any]:
        return self._adapter(cfg).remove(feature, items, dry_run=dry_run)

    def health(self, cfg: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._adapter(cfg).health()


OPS = _MYANIMELISTOPS()
