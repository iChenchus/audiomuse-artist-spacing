"""Tests for the artist_spacing plugin against a stand-in for the AudioMuse-AI core.

The stand-in reproduces the three things the plugin depends on: the bearer-token
guard that runs before every request, a before/teardown pair that must stay
balanced, and the response shapes of the similarity endpoints.
"""

import importlib.util
import json
import random
import sys
import types
from pathlib import Path

import pytest
from flask import Flask, g, jsonify, request

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "secret-token"

SETTINGS = {}
SEED_AUTHORS = {"seed-a": "Artist A", "seed-z": "Artist Z"}


def _install_stubs():
    api = types.ModuleType("plugin.api")
    api.get_setting = lambda key, default=None: SETTINGS.get(key, default)
    api.set_setting = lambda key, value: SETTINGS.__setitem__(key, value)
    api.render_page = lambda body, title=None: f"<html><h1>{title}</h1>{body}</html>"
    api.manage_plugins_url = lambda: "/plugins"
    api.get_score_data_by_ids = lambda ids: [
        {"item_id": i, "author": SEED_AUTHORS[i]} for i in ids if i in SEED_AUTHORS
    ]
    pkg = types.ModuleType("plugin")
    pkg.api = api
    sys.modules["plugin"] = pkg
    sys.modules["plugin.api"] = api

    ctx = types.ModuleType("app_server_context")
    ctx.resolve_input_item_id = lambda raw, data=None: raw
    sys.modules["app_server_context"] = ctx


_install_stubs()
_spec = importlib.util.spec_from_file_location(
    "audiomuse_plugins.artist_spacing", ROOT / "plugins" / "artist_spacing" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


def T(item_id, author):
    return {"item_id": item_id, "title": f"song {item_id}", "author": author, "album": "x", "distance": 0.1}


# The shape the user described: seed by Artist A, then three more of Artist A up front.
CLUMPED = [
    T("1", "Artist A"), T("2", "Artist A"), T("3", "Artist B"), T("4", "Artist A"),
    T("5", "Artist C"), T("6", "Artist B"), T("7", "Artist D"), T("8", "Artist E"),
    T("9", "Artist B"), T("10", "Artist F"), T("11", "Artist G"), T("12", "Artist C"),
]


@pytest.fixture
def core():
    SETTINGS.clear()
    app = Flask(__name__)
    app.calls = []
    app.balance = {"started": 0, "finished": 0}

    @app.before_request
    def guard():
        app.balance["started"] += 1
        g.counted = True
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return "login", 302, {"Location": "/login"}

    @app.teardown_request
    def done(exc=None):
        if g.pop("counted", False):
            app.balance["finished"] += 1

    @app.route("/api/similar_tracks")
    def similar_tracks():
        app.calls.append(("similar_tracks", request.query_string.decode()))
        return jsonify(CLUMPED[: request.args.get("n", 50, type=int)])

    @app.route("/api/hyperbolic/similar", methods=["POST"])
    def hyperbolic():
        app.calls.append(("hyperbolic", request.get_json()))
        return jsonify({"results": CLUMPED, "count": len(CLUMPED)})

    @app.route("/api/sem_grove/search", methods=["POST"])
    def sem_grove():
        seed = dict(T("seed-a", "Artist A"), is_seed=True)
        return jsonify({"results": [seed] + CLUMPED, "count": len(CLUMPED) + 1})

    @app.route("/api/find_path")
    def find_path():
        return jsonify({"path": CLUMPED})

    @app.route("/api/broken")
    def broken():
        return jsonify({"error": "nope"}), 404

    app.register_blueprint(plugin.bp, url_prefix="/plugins/artist_spacing")
    return app


AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/plugins/artist_spacing"


# ---------------------------------------------------------------- algorithm

def test_already_spaced_list_is_untouched():
    tracks = [T(str(i), f"Artist {i}") for i in range(10)]
    assert plugin.space_artists(tracks, 3) == tracks


def test_clumped_list_is_spaced_and_complete():
    out = plugin.space_artists(CLUMPED, 3, seed_artist="Artist A")
    assert sorted(t["item_id"] for t in out) == sorted(t["item_id"] for t in CLUMPED)
    assert plugin.count_violations(out, 3, "Artist A") == 0
    # the seed artist cannot be among the first three
    assert all(t["author"] != "Artist A" for t in out[:3])


def test_artist_matching_ignores_case_and_spacing():
    tracks = [T("1", "Barricada"), T("2", " barricada "), T("3", "X"), T("4", "Y"), T("5", "Z")]
    out = plugin.space_artists(tracks, 3)
    assert [t["item_id"] for t in out] == ["1", "3", "4", "5", "2"]


def test_tracks_without_artist_are_kept_and_never_block():
    tracks = [T("1", "A"), T("2", None), T("3", ""), T("4", "A"), T("5", "B"), T("6", "C"), T("7", "D")]
    out = plugin.space_artists(tracks, 3)
    assert len(out) == len(tracks)
    assert plugin.count_violations(out, 3) == 0


def test_tail_clump_is_slotted_back():
    tracks = [T(str(i), f"Artist {i}") for i in range(12)] + [T("a1", "Z"), T("a2", "Z"), T("a3", "Z")]
    out = plugin.space_artists(tracks, 3)
    assert plugin.count_violations(out, 3) == 0


def test_impossible_list_keeps_every_track():
    tracks = [T(str(i), "Only One") for i in range(6)]
    out = plugin.space_artists(tracks, 3)
    assert sorted(t["item_id"] for t in out) == sorted(t["item_id"] for t in tracks)


def test_random_lists_shaped_like_the_core_output():
    rng = random.Random(1234)
    for _ in range(500):
        artists = [f"Artist {i}" for i in range(rng.randint(8, 30))]
        tracks = []
        for artist in artists:
            for _ in range(rng.randint(1, 3)):  # the core caps at 3 per artist
                tracks.append(T(str(len(tracks)), artist))
        rng.shuffle(tracks)
        seed = rng.choice(artists + [None])
        gap = rng.randint(1, 4)
        out = plugin.space_artists(tracks, gap, seed)
        assert sorted(t["item_id"] for t in out) == sorted(t["item_id"] for t in tracks)
        assert plugin.count_violations(out, gap, seed) == 0


# ---------------------------------------------------------------- mirroring

def test_similar_tracks_is_reordered_with_the_seed_artist(core):
    client = core.test_client()
    resp = client.get(f"{BASE}/api/similar_tracks?item_id=seed-a&n=12&eliminate_duplicates=true", headers=AUTH)
    assert resp.status_code == 200
    out = resp.get_json()
    assert sorted(t["item_id"] for t in out) == sorted(t["item_id"] for t in CLUMPED)
    assert plugin.count_violations(out, 3, "Artist A") == 0
    assert all(t["author"] != "Artist A" for t in out[:3])
    # the core saw the caller's own query, untouched
    assert core.calls == [("similar_tracks", "item_id=seed-a&n=12&eliminate_duplicates=true")]


def test_unknown_seed_still_spaces_the_list(core):
    out = core.test_client().get(f"{BASE}/api/similar_tracks?item_id=nobody", headers=AUTH).get_json()
    assert plugin.count_violations(out, 3) == 0


def test_post_endpoint_keeps_its_envelope(core):
    body = {"item_id": "seed-z", "limit": 12}
    resp = core.test_client().post(f"{BASE}/api/hyperbolic/similar", json=body, headers=AUTH)
    data = resp.get_json()
    assert data["count"] == len(CLUMPED)
    assert plugin.count_violations(data["results"], 3, "Artist Z") == 0
    assert core.calls == [("hyperbolic", body)]


def test_seed_row_stays_first(core):
    data = core.test_client().post(f"{BASE}/api/sem_grove/search", json={"item_id": "seed-a"}, headers=AUTH).get_json()
    assert data["results"][0]["is_seed"] is True
    rest = data["results"][1:]
    assert plugin.count_violations(rest, 3, "Artist A") == 0


def test_other_endpoints_pass_through_byte_for_byte(core):
    client = core.test_client()
    direct = client.get("/api/find_path", headers=AUTH)
    mirrored = client.get(f"{BASE}/api/find_path", headers=AUTH)
    assert mirrored.get_data() == direct.get_data()
    missing = client.get(f"{BASE}/api/broken", headers=AUTH)
    assert missing.status_code == 404 and missing.get_json() == {"error": "nope"}


def test_auth_is_enforced_by_the_core(core):
    client = core.test_client()
    assert client.get(f"{BASE}/api/similar_tracks?item_id=seed-a").status_code == 302  # plugin page, no token
    wrong = client.get(f"{BASE}/api/similar_tracks?item_id=seed-a", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 302


def test_gap_zero_turns_it_off(core):
    SETTINGS["gap"] = 0
    out = core.test_client().get(f"{BASE}/api/similar_tracks?item_id=seed-a", headers=AUTH).get_json()
    assert out == CLUMPED


def test_request_hooks_stay_balanced(core):
    client = core.test_client()
    for _ in range(5):
        client.get(f"{BASE}/api/similar_tracks?item_id=seed-a", headers=AUTH)
    assert core.balance["started"] == core.balance["finished"] == 10


def test_settings_page_saves_the_gap(core):
    client = core.test_client()
    page = client.get(f"{BASE}/settings", headers=AUTH)
    assert page.status_code == 200 and b"/plugins/artist_spacing" in page.data
    saved = client.post(f"{BASE}/settings", data={"gap": "5"}, headers=AUTH)
    assert saved.status_code == 302 and SETTINGS["gap"] == 5
    client.post(f"{BASE}/settings", data={"gap": "999"}, headers=AUTH)
    assert SETTINGS["gap"] == plugin.MAX_GAP
