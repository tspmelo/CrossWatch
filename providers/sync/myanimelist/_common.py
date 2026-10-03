# /providers/sync/myanimelist/_common.py
# MyAnimeList Module shared helpers
# Copyright (c) 2025-2026 CrossWatch / Cenodude (https://github.com/cenodude/CrossWatch)
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Callable

from cw_platform.anime_mapping import AnimeMappingService
from cw_platform.anime_mapping.service import (
    PAIR_FEATURE_OPTIONS_KEY,
    mapped_or_default_media_type,
    mapping_enabled_for_feature,
    runtime_pair_feature_options,
)
from cw_platform.id_map import minimal as id_minimal

from .._log import log as cw_log
from ..anilist._common import read_json, state_file, write_json  # generic pair-scoped state helpers

__all__ = ["read_json", "state_file", "write_json"]

_WS = re.compile(r"\s+")


def make_logger(feature: str) -> Callable[..., None]:
    def _log(msg: str, level: str = "debug", **fields: Any) -> None:
        cw_log("MYANIMELIST", str(feature), str(level), str(msg), **fields)
    return _log


def to_int(v: Any) -> int | None:
    try:
        if v is None or isinstance(v, bool):
            return None
        s = str(v).strip()
        return int(float(s)) if s else None
    except Exception:
        return None


def node_title(node: Mapping[str, Any]) -> str:
    alt = node.get("alternative_titles") if isinstance(node.get("alternative_titles"), Mapping) else {}
    return str((alt or {}).get("en") or node.get("title") or "").strip()


def node_year(node: Mapping[str, Any]) -> int:
    season = node.get("start_season") if isinstance(node.get("start_season"), Mapping) else {}
    return to_int((season or {}).get("year")) or 0


def node_type(node: Mapping[str, Any]) -> str:
    return "movie" if str(node.get("media_type") or "").lower() == "movie" else "show"


def node_item(node: Mapping[str, Any], *, kind: str | None = None) -> dict[str, Any]:
    return {"type": kind or node_type(node), "title": node_title(node), "year": node_year(node), "ids": {"mal": int(node["id"])}}


def unresolved_item(item: Mapping[str, Any], reason: str) -> dict[str, Any]:
    out = dict(id_minimal(item))
    out["_cw_unresolved_reason"] = str(reason or "unknown")
    return out


def _mapping_enabled(adapter: Any, feature: str) -> bool:
    cfg = getattr(adapter, "raw_cfg", None)
    if not isinstance(cfg, Mapping):
        return False
    if PAIR_FEATURE_OPTIONS_KEY in cfg:
        return bool(runtime_pair_feature_options(cfg, feature).get("use_anime_mapping", False))
    return mapping_enabled_for_feature(cfg, feature)


def mapping_ready(adapter: Any, feature: str) -> bool:
    if not _mapping_enabled(adapter, feature):
        return False
    try:
        return bool(AnimeMappingService(getattr(adapter, "raw_cfg", None)).ready())
    except Exception:
        return False


def anime_only(adapter: Any, feature: str) -> bool:
    cfg = getattr(adapter, "raw_cfg", None)
    if not (isinstance(cfg, Mapping) and PAIR_FEATURE_OPTIONS_KEY in cfg):
        return False
    return bool(runtime_pair_feature_options(cfg, feature).get("anime_only_sync", False) and mapping_ready(adapter, feature))


def enrich(adapter: Any, item: Mapping[str, Any], feature: str) -> dict[str, Any]:
    out = dict(item or {})
    if not mapping_ready(adapter, feature):
        return out
    try:
        svc = AnimeMappingService(getattr(adapter, "raw_cfg", None))
        got = svc.enrich_ids(out.get("ids") if isinstance(out.get("ids"), Mapping) else {}, media_type=mapped_or_default_media_type(out))
        if isinstance(got, Mapping) and got.get("ids"):
            out["ids"] = {**dict(out.get("ids") or {}), **dict(got["ids"])}
    except Exception as e:
        cw_log("MYANIMELIST", feature, "debug", "mapping_enrich_failed", error_type=e.__class__.__name__)
    return out


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def _score(want_title: str, want_year: int | None, want_kind: str, node: Mapping[str, Any]) -> int:
    wt = _norm(want_title)
    titles = {_norm(node_title(node)), _norm(str(node.get("title") or ""))} - {""}
    score = 0
    if wt in titles:
        score += 70
    elif any(wt and (wt in t or t in wt) for t in titles):
        score += 20
    year = node_year(node)
    if want_year and year:
        score += 30 if want_year == year else -50
    if (want_kind == "movie") == (node_type(node) == "movie"):
        score += 5
    return score


def resolve_mal_id(adapter: Any, item: Mapping[str, Any], feature: str) -> tuple[int | None, str]:
    """(mal_id, reason). Reason is set when no id: anime_only_unmapped / no_match."""
    enriched = enrich(adapter, item, feature)
    mal = to_int((enriched.get("ids") or {}).get("mal"))
    if mal:
        return mal, ""
    if anime_only(adapter, feature):
        return None, "anime_only_unmapped"
    title = str(item.get("title") or "").strip()
    if not title:
        return None, "no_match"
    year = to_int(item.get("year"))
    kind = str(item.get("type") or "").strip().lower()
    try:
        nodes = adapter.client.search(title)
    except Exception:
        return None, "no_match"
    best = max(nodes, key=lambda n: _score(title, year, kind, n), default=None)
    # Same bar as AniList: an exact title plus a matching year.
    if best is None or _score(title, year, kind, best) < 85:
        return None, "no_match"
    return int(best["id"]), ""


# Shadow: remembers which source item each MAL write came from, so later indexes carry the
# source ids (the planner matches on shared ids) and unmatched items aren't retried every run.
def shadow_load(feature: str) -> dict[str, dict[str, Any]]:
    data = read_json(state_file(f"myanimelist_{feature}_shadow.json"))
    return {str(k): dict(v) for k, v in (data or {}).items() if isinstance(v, Mapping)}


def shadow_save(feature: str, shadow: Mapping[str, Any]) -> None:
    write_json(state_file(f"myanimelist_{feature}_shadow.json"), shadow)


def shadow_entry(item: Mapping[str, Any], mal_id: int | None) -> dict[str, Any]:
    ids = item.get("ids") if isinstance(item.get("ids"), Mapping) else {}
    return {
        "mal": int(mal_id or 0),
        "ids": {str(k): str(v) for k, v in ids.items() if k and v not in (None, "")},
        "type": str(item.get("type") or ""),
        "title": str(item.get("title") or ""),
        "year": to_int(item.get("year")) or 0,
    }


def shadow_apply(shadow: dict[str, dict[str, Any]], out: dict[str, dict[str, Any]], live: set[int]) -> bool:
    """Merge source ids into indexed items; drop shadow rows whose MAL entry is gone. Returns changed."""
    by_mal = {int(e["mal"]): e for e in shadow.values() if to_int(e.get("mal"))}
    for item in out.values():
        ent = by_mal.get(to_int((item.get("ids") or {}).get("mal")) or 0)
        if ent:
            item["ids"] = {**ent.get("ids", {}), **item["ids"]}
    dead = [k for k, e in shadow.items() if to_int(e.get("mal")) and int(e["mal"]) not in live]
    for k in dead:
        shadow.pop(k, None)
    return bool(dead)
