# /providers/sync/myanimelist/_history.py
# MyAnimeList history sync (num_episodes_watched)
# Copyright (c) 2025-2026 CrossWatch / Cenodude (https://github.com/cenodude/CrossWatch)
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from cw_platform.history_events import history_sync_key
from cw_platform.id_map import minimal as id_minimal

from ..anilist._progress import resolve_target
from ._common import make_logger, mapping_ready, node_title, node_year, resolve_mal_id, to_int, unresolved_item

FEATURE = "history"
_log = make_logger(FEATURE)

# ponytail: MAL keeps one date per title (finish_date / updated_at), so every episode of a
# show shares it and rewatches aren't represented. Per-episode dates would need another source.


def _iso(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rewatches(adapter: Any) -> bool:
    cfg = getattr(adapter, "raw_cfg", None)
    return bool(isinstance(cfg, Mapping) and cfg.get("_cw_history_rewatches"))


def _key(adapter: Any, item: Mapping[str, Any]) -> str:
    return history_sync_key(item, event_mode=_rewatches(adapter))


def build_index(adapter: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in adapter.client.animelist():
        node = row.get("node") or {}
        ls = row.get("list_status") or {}
        mal_id = to_int(node.get("id"))
        watched = to_int(ls.get("num_episodes_watched")) or 0
        status = str(ls.get("status") or "")
        if not mal_id or not (watched or status == "completed"):
            continue
        when = _iso(ls.get("finish_date") if status == "completed" else None) or _iso(ls.get("updated_at"))
        if not when:
            continue
        title, year = node_title(node), node_year(node)
        if str(node.get("media_type") or "").lower() == "movie":
            items = [{"type": "movie", "title": title, "year": year, "ids": {"mal": mal_id}, "watched_at": when}]
        else:
            items = [
                {
                    "type": "episode",
                    "show_ids": {"mal": mal_id},
                    "season": 1,
                    "episode": n,
                    "_simkl_episode_number": n,  # native absolute number, read by anime history_coords
                    "series_title": title,
                    "watched_at": when,
                }
                for n in range(1, watched + 1)
            ]
        for item in items:
            key = _key(adapter, item)
            if key:
                out[key] = item
    _log("index_done", "info", count=len(out))
    return out


def _target(adapter: Any, item: Mapping[str, Any]) -> tuple[int | None, int, str]:
    """(mal_id, episode, reason). Movies use episode 1."""
    if str(item.get("type") or "").lower() == "movie":
        mal_id, reason = resolve_mal_id(adapter, item, FEATURE)
        return mal_id, 1, reason
    sids = item.get("show_ids") if isinstance(item.get("show_ids"), Mapping) else {}
    native = to_int(item.get("_simkl_episode_number"))
    if to_int(sids.get("mal")) and native:
        return int(sids["mal"]), native, ""
    if not mapping_ready(adapter, FEATURE):
        return None, 0, "anime_mapping_required"
    hit = resolve_target(getattr(adapter, "raw_cfg", None), item, target="mal")
    return (hit[0], hit[1], "") if hit else (None, 0, "no_match")


def _group(adapter: Any, items: Iterable[Mapping[str, Any]]) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[int, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for it in items:
        m = id_minimal(it)
        mal_id, ep, reason = _target(adapter, m)
        if not mal_id:
            unresolved.append(unresolved_item(m, reason))
            continue
        g = groups.setdefault(mal_id, {"eps": [], "keys": [], "movie": m.get("type") == "movie"})
        g["eps"].append(ep)
        g["keys"].append(_key(adapter, m))
    return groups, unresolved


def _entry(adapter: Any, mal_id: int) -> tuple[dict[str, Any], int | None]:
    row = adapter.client.list_entry(mal_id)
    total = to_int(((row or {}).get("node") or {}).get("num_episodes"))
    if row is None or total is None:
        total = to_int(adapter.client.request("GET", f"/anime/{mal_id}", params={"fields": "num_episodes"}).get("num_episodes"))
    return dict((row or {}).get("list_status") or {}), (total or None)


def add(adapter: Any, items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    groups, unresolved = _group(adapter, items)
    confirmed: list[str] = []
    for mal_id, g in groups.items():
        try:
            ls, total = _entry(adapter, mal_id)
            current = to_int(ls.get("num_episodes_watched")) or 0
            target = 1 if g["movie"] else max(g["eps"])
            if total:
                target = min(target, total)
            if ls.get("status") != "completed" and target > current:
                done = bool(total and target >= total)
                adapter.client.save(mal_id, num_watched_episodes=target, status="completed" if done else "watching")
            confirmed.extend(k for k in g["keys"] if k)
        except Exception as e:
            unresolved.extend({"key": k, "_cw_unresolved_reason": f"add_failed:{e.__class__.__name__}"} for k in g["keys"])
    _log("write_done", "info", op="add", applied=len(confirmed), unresolved=len(unresolved))
    return {"confirmed_keys": confirmed, "unresolved": unresolved}


def remove(adapter: Any, items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    groups, unresolved = _group(adapter, items)
    confirmed: list[str] = []
    for mal_id, g in groups.items():
        try:
            row = adapter.client.list_entry(mal_id)
            current = to_int(((row or {}).get("list_status") or {}).get("num_episodes_watched")) or 0
            keep = 0 if g["movie"] else min(current, min(g["eps"]) - 1)
            if row is not None and (keep < current or g["movie"]):
                # Never delete the entry: unwatching just lowers the count (to plan_to_watch at zero).
                adapter.client.save(mal_id, num_watched_episodes=keep, status="watching" if keep else "plan_to_watch")
            confirmed.extend(k for k in g["keys"] if k)
        except Exception as e:
            unresolved.extend({"key": k, "_cw_unresolved_reason": f"remove_failed:{e.__class__.__name__}"} for k in g["keys"])
    _log("write_done", "info", op="remove", applied=len(confirmed), unresolved=len(unresolved))
    return {"confirmed_keys": confirmed, "unresolved": unresolved}
