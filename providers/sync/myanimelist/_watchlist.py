# /providers/sync/myanimelist/_watchlist.py
# MyAnimeList watchlist sync (plan_to_watch)
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

FEATURE = "watchlist"
PTW = "plan_to_watch"
_log = make_logger(FEATURE)


def build_index(adapter: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    live: set[int] = set()
    for row in adapter.client.animelist():
        node = row.get("node") or {}
        if (row.get("list_status") or {}).get("status") != PTW or not node.get("id"):
            continue
        live.add(int(node["id"]))
        item = enrich(adapter, node_item(node, kind="anime"), FEATURE)
        item["myanimelist"] = {"status": PTW}
        out[adapter.key_of(item)] = item

    shadow = shadow_load(FEATURE)
    changed = shadow_apply(shadow, out, live)
    for src_key, ent in shadow.items():
        if not to_int(ent.get("mal")) and src_key not in out:
            # Ignored (no MAL match): report as present so the planner stops re-adding it.
            out[src_key] = {"type": ent.get("type") or "anime", "title": ent.get("title"), "year": ent.get("year"), "ids": dict(ent.get("ids") or {}), "myanimelist": {"ignored": True}}
    if changed:
        shadow_save(FEATURE, shadow)
    out = {k: v for k, v in out.items() if k}
    _log("index_done", "info", count=len(out))
    return out


def add(adapter: Any, items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    shadow = shadow_load(FEATURE)
    confirmed: list[str] = []
    skipped: list[str] = []
    unresolved: list[dict[str, Any]] = []
    for it in items:
        m = id_minimal(it)
        key = adapter.key_of(m)
        ent = shadow.get(key)
        if ent and not to_int(ent.get("mal")):
            skipped.append(key)
            continue
        mal_id, reason = (int(ent["mal"]), "") if ent else resolve_mal_id(adapter, m, FEATURE)
        if not mal_id:
            shadow[key] = shadow_entry(m, None)
            skipped.append(key)
            _log("write_item_skipped", op="add", title=str(m.get("title") or ""), reason=reason)
            continue
        current = ((adapter.client.list_entry(mal_id) or {}).get("list_status") or {}).get("status")
        try:
            # Never demote an entry the user is already watching or has finished.
            if not current:
                adapter.client.save(mal_id, status=PTW)
            shadow[key] = shadow_entry(m, mal_id)
            confirmed.append(key)
        except Exception as e:
            unresolved.append(unresolved_item(m, f"add_failed:{e.__class__.__name__}"))
    shadow_save(FEATURE, shadow)
    _log("write_done", "info", op="add", applied=len(confirmed), skipped=len(skipped), unresolved=len(unresolved))
    return {"confirmed_keys": confirmed, "skipped_keys": skipped, "unresolved": unresolved}


def remove(adapter: Any, items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    shadow = shadow_load(FEATURE)
    confirmed: list[str] = []
    unresolved: list[dict[str, Any]] = []
    for it in items:
        m = id_minimal(it)
        key = adapter.key_of(m)
        ent = shadow.pop(key, None)
        mal_id = to_int((m.get("ids") or {}).get("mal")) or (to_int(ent.get("mal")) if ent else None)
        if ent and not mal_id:
            confirmed.append(key)  # ignored placeholder: nothing on MAL to remove
            continue
        if not mal_id:
            mal_id, _ = resolve_mal_id(adapter, m, FEATURE)
        if not mal_id:
            unresolved.append(unresolved_item(m, "no_match"))
            continue
        current = ((adapter.client.list_entry(mal_id) or {}).get("list_status") or {}).get("status")
        try:
            if current == PTW:
                adapter.client.delete(mal_id)
            confirmed.append(key)
        except Exception as e:
            unresolved.append(unresolved_item(m, f"remove_failed:{e.__class__.__name__}"))
    shadow_save(FEATURE, shadow)
    _log("write_done", "info", op="remove", applied=len(confirmed), unresolved=len(unresolved))
    return {"confirmed_keys": confirmed, "unresolved": unresolved}
