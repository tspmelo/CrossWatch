# tests/test_myanimelist_sync.py
# CrossWatch test scripts
from __future__ import annotations

from typing import Any

import pytest

import providers.sync._mod_MYANIMELIST as mal_mod
from providers.sync.myanimelist import _common, _history, _ratings, _watchlist


def _row(mal_id: int, status: str, *, score: int = 0, watched: int = 0, total: int = 12, media_type: str = "tv") -> dict[str, Any]:
    return {
        "node": {"id": mal_id, "title": f"Show {mal_id}", "media_type": media_type, "num_episodes": total, "start_season": {"year": 2020}},
        "list_status": {"status": status, "score": score, "num_episodes_watched": watched, "updated_at": "2024-05-01T10:00:00+00:00"},
    }


class FakeClient(mal_mod.MALClient):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._list_cache = rows
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, path: str, **kw: Any) -> Any:
        self.calls.append((method, path, dict(kw.get("data") or {})))
        if method == "PATCH":
            return dict(kw.get("data") or {})
        if method == "GET" and path.startswith("/anime/"):
            return {"num_episodes": 24}
        return {}


class Adapter:
    raw_cfg: dict[str, Any] = {}
    key_of = staticmethod(mal_mod.MYANIMELISTModule.key_of)

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.client = FakeClient(rows)

    def writes(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [c for c in self.client.calls if c[0] != "GET"]


@pytest.fixture(autouse=True)
def _no_shadow_io(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(_common, "read_json", lambda p: dict(store.get(str(p)) or {}))
    monkeypatch.setattr(_common, "write_json", lambda p, d: store.__setitem__(str(p), dict(d)))


def test_watchlist_index_only_plan_to_watch() -> None:
    a = Adapter([_row(1, "plan_to_watch"), _row(2, "watching")])
    idx = _watchlist.build_index(a)
    assert [v["ids"]["mal"] for v in idx.values()] == [1]
    assert all(v["type"] == "anime" for v in idx.values())


def test_watchlist_add_never_demotes_and_remove_only_deletes_ptw() -> None:
    a = Adapter([_row(2, "watching"), _row(3, "plan_to_watch")])
    res = _watchlist.add(a, [{"type": "anime", "title": "x", "ids": {"mal": 2}}, {"type": "anime", "title": "y", "ids": {"mal": 5}}])
    assert len(res["confirmed_keys"]) == 2
    assert a.writes() == [("PATCH", "/anime/5/my_list_status", {"status": "plan_to_watch"})]

    a.client.calls.clear()
    _watchlist.remove(a, [{"type": "anime", "ids": {"mal": 2}}, {"type": "anime", "ids": {"mal": 3}}])
    assert a.writes() == [("DELETE", "/anime/3/my_list_status", {})]


def test_watchlist_unmatched_item_is_ignored_and_reported_present() -> None:
    a = Adapter([])
    a.client.search = lambda q: []  # type: ignore[method-assign]
    item = {"type": "movie", "title": "Not Anime", "year": 2001, "ids": {"tmdb": 99}}
    res = _watchlist.add(a, [item])
    assert res["skipped_keys"] == ["tmdb:99"]
    assert "tmdb:99" in _watchlist.build_index(a)


def test_ratings_index_add_and_unrate() -> None:
    a = Adapter([_row(1, "completed", score=8), _row(2, "completed", score=0)])
    idx = _ratings.build_index(a)
    assert [(v["ids"]["mal"], v["rating"]) for v in idx.values()] == [(1, 8)]

    _ratings.add(a, [{"type": "show", "ids": {"mal": 1}, "rating": 8}, {"type": "show", "ids": {"mal": 2}, "rating": 7}])
    assert a.writes() == [("PATCH", "/anime/2/my_list_status", {"score": 7})]  # unchanged score not rewritten

    a.client.calls.clear()
    _ratings.remove(a, [{"type": "show", "ids": {"mal": 1}}, {"type": "show", "ids": {"mal": 9}}])
    assert a.writes() == [("PATCH", "/anime/1/my_list_status", {"score": 0})]  # unlisted title not created


def test_history_index_expands_watched_episodes() -> None:
    a = Adapter([_row(1, "watching", watched=3), _row(2, "plan_to_watch"), _row(3, "completed", media_type="movie", total=1)])
    idx = _history.build_index(a)
    eps = sorted(v["episode"] for v in idx.values() if v["type"] == "episode")
    assert eps == [1, 2, 3]
    ep = next(v for v in idx.values() if v["type"] == "episode")
    assert ep["show_ids"] == {"mal": 1} and ep["_simkl_episode_number"] == ep["episode"]
    assert ep["watched_at"] == "2024-05-01T10:00:00Z"
    assert [v["ids"]["mal"] for v in idx.values() if v["type"] == "movie"] == [3]


def _ep(mal_id: int, n: int) -> dict[str, Any]:
    return {"type": "episode", "show_ids": {"mal": mal_id}, "season": 1, "episode": n, "_simkl_episode_number": n, "watched_at": "2024-05-01T10:00:00Z"}


def test_history_add_takes_max_and_completes_at_end() -> None:
    a = Adapter([_row(1, "watching", watched=3, total=12), _row(2, "watching", watched=10, total=12)])
    res = _history.add(a, [_ep(1, 4), _ep(1, 6), _ep(1, 2), _ep(2, 12), _ep(2, 5)])
    assert len(res["confirmed_keys"]) == 5
    assert a.writes() == [
        ("PATCH", "/anime/1/my_list_status", {"num_watched_episodes": 6, "status": "watching"}),
        ("PATCH", "/anime/2/my_list_status", {"num_watched_episodes": 12, "status": "completed"}),
    ]


def test_history_add_skips_when_not_newer() -> None:
    a = Adapter([_row(1, "watching", watched=8)])
    _history.add(a, [_ep(1, 5)])
    assert a.writes() == []


def test_history_remove_lowers_count_and_never_deletes() -> None:
    a = Adapter([_row(1, "watching", watched=8), _row(2, "watching", watched=1)])
    _history.remove(a, [_ep(1, 6), _ep(1, 7), _ep(2, 1)])
    assert a.writes() == [
        ("PATCH", "/anime/1/my_list_status", {"num_watched_episodes": 5, "status": "watching"}),
        ("PATCH", "/anime/2/my_list_status", {"num_watched_episodes": 0, "status": "plan_to_watch"}),
    ]


def test_history_episode_without_native_number_needs_mapping() -> None:
    a = Adapter([])
    res = _history.add(a, [{"type": "episode", "show_ids": {"tvdb": 1}, "season": 2, "episode": 3}])
    assert res["confirmed_keys"] == []
    assert res["unresolved"][0]["_cw_unresolved_reason"] == "anime_mapping_required"


def test_list_fetch_pages_once_and_is_shared() -> None:
    pages = [
        {"data": [_row(1, "plan_to_watch")], "paging": {"next": "https://api.myanimelist.net/v2/users/@me/animelist?offset=1"}},
        {"data": [_row(2, "completed", score=7)], "paging": {}},
    ]
    client = FakeClient([])
    client._list_cache = None
    client.request = lambda method, path, **kw: pages.pop(0)  # type: ignore[method-assign]
    assert [r["node"]["id"] for r in client.animelist()] == [1, 2]
    assert [r["node"]["id"] for r in client.animelist()] == [1, 2]  # cached, no third request
    assert pages == []
