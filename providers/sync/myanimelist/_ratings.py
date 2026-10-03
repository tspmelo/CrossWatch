# /providers/sync/myanimelist/_ratings.py
# MyAnimeList ratings sync (list score 1-10)
# Copyright (c) 2025-2026 CrossWatch / Cenodude (https://github.com/cenodude/CrossWatch)
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from cw_platform.id_map import minimal as id_minimal

from ._common import (
    enrich,
    make_logger,
    node_item,
    resolve_mal_id,
    shadow_apply,
    shadow_entry,
    shadow_load,
    shadow_save,
    to_int,
    unresolved_item,
)

FEATURE = "ratings"
_log = make_logger(FEATURE)


def rating_1_10(v: Any) -> int | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    n = int(f + 0.5)
    return n if 1 <= n <= 10 else None


def build_index(adapter: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    live: set[int] = set()
    for row in adapter.client.animelist():
        node = row.get("node") or {}
        ls = row.get("list_status") or {}
        score = rating_1_10(ls.get("score"))
        if not score or not node.get("id"):
            continue
        live.add(int(node["id"]))
        item = enrich(adapter, node_item(node), FEATURE)
        item["rating"] = score
        if ls.get("updated_at"):
            item["rated_at"] = str(ls["updated_at"])
        out[adapter.key_of(item)] = item
    shadow = shadow_load(FEATURE)
    if shadow_apply(shadow, out, live):
        shadow_save(FEATURE, shadow)
    out = {k: v for k, v in out.items() if k}
    _log("index_done", "info", count=len(out))
    return out


def _write(adapter: Any, items: Iterable[Mapping[str, Any]], *, unrate: bool) -> dict[str, Any]:
    shadow = shadow_load(FEATURE)
    confirmed: list[str] = []
    skipped: list[str] = []
    unresolved: list[dict[str, Any]] = []
    for it in items:
        m = id_minimal(it)
        key = adapter.key_of(m)
        score = 0 if unrate else rating_1_10(m.get("rating"))
        if score is None:
            unresolved.append(unresolved_item(m, "missing_or_invalid_rating"))
            continue
        ent = shadow.get(key)
        mal_id, reason = (int(ent["mal"]), "") if ent and to_int(ent.get("mal")) else resolve_mal_id(adapter, m, FEATURE)
        if not mal_id:
            if reason == "anime_only_unmapped":
                skipped.append(key)
            else:
                unresolved.append(unresolved_item(m, reason or "no_match"))
            continue
        row = adapter.client.list_entry(mal_id)
        current = to_int(((row or {}).get("list_status") or {}).get("score")) or 0
        try:
            if unrate and row is None:
                pass  # not on the list: nothing to unrate
            elif current != score:
                adapter.client.save(mal_id, score=score)
            if not unrate:
                shadow[key] = shadow_entry(m, mal_id)
            confirmed.append(key)
        except Exception as e:
            unresolved.append(unresolved_item(m, f"write_failed:{e.__class__.__name__}"))
    shadow_save(FEATURE, shadow)
    _log("write_done", "info", op="remove" if unrate else "add", applied=len(confirmed), skipped=len(skipped), unresolved=len(unresolved))
    return {"confirmed_keys": confirmed, "skipped_keys": skipped, "unresolved": unresolved}


def add(adapter: Any, items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return _write(adapter, items, unrate=False)


def remove(adapter: Any, items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return _write(adapter, items, unrate=True)
