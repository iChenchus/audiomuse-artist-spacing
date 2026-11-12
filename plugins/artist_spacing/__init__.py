# Artist Spacing - https://github.com/iChenchus/audiomuse-artist-spacing
# SPDX-License-Identifier: AGPL-3.0-only

"""Artist Spacing - an AudioMuse-AI plugin.

AudioMuse-AI caps how many songs of one artist a similarity search returns
(MAX_SONGS_PER_ARTIST), but it does not care where they land in the list, so an
Instant Mix often plays the same artist two or three times in a row.

This plugin mirrors the AudioMuse-AI API under /plugins/artist_spacing/api/...
Every call is handed to the real endpoint untouched. For the endpoints that
answer with a list of similar tracks, the list is reordered on the way back so
that two songs of the same artist are separated by at least N songs of other
artists (N = 3 by default, the seed song counts). Nothing is added or removed.

Point the Navidrome plugin (or any other client) at
    http://<audiomuse-host>:8000/plugins/artist_spacing
instead of
    http://<audiomuse-host>:8000
and keep the same API token.
"""

import html
import json
import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from flask import Blueprint, Response, current_app, redirect, request

from plugin.api import get_setting, manage_plugins_url, render_page, set_setting

PLUGIN_ID = "artist_spacing"
DEFAULT_GAP = 3
MAX_GAP = 20
CORE_TIMEOUT = 280

logger = logging.getLogger(__name__)
bp = Blueprint(PLUGIN_ID, __name__)

# Endpoints whose answer is a list of similar tracks, and where that list
# lives: None means the JSON body is the list itself, a string is the key.
TRACK_ENDPOINTS = {
    "similar_tracks": None,
    "sem_grove/search": "results",
    "hyperbolic/similar": "results",
    "clap/search_by_song": "results",
    "alchemy": "results",
}

FORWARDED_HEADERS = ("Authorization", "Cookie", "Content-Type", "Accept")

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="artist-spacing")


# --------------------------------------------------------------------------
# The reordering itself (pure functions, no Flask)
# --------------------------------------------------------------------------

def artist_key(track):
    """Comparison key for a track's artist, or None when it has none."""
    if not isinstance(track, dict):
        return None
    author = track.get("author")
    if not isinstance(author, str):
        return None
    author = " ".join(author.split()).casefold()
    return author or None


def _fits(out, index, key, gap, seed_key):
    """Can a track of artist `key` be inserted at `index` of `out`?"""
    if seed_key == key and index < gap:
        return False
    lo = max(0, index - gap)
    hi = min(len(out), index + gap)
    return all(artist_key(out[i]) != key for i in range(lo, hi))


def space_artists(tracks, gap=DEFAULT_GAP, seed_artist=None):
    """Reorder `tracks` so the same artist is at least `gap` songs apart.

    The incoming order (most similar first) is kept as far as possible: each
    slot takes the first remaining track whose artist has not played in the
    last `gap` slots. Tracks that do not fit anywhere at the end are slotted
    back into the latest earlier position that respects the rule. If the list
    simply lacks the variety to honour it, the leftovers go last, as far from
    their own artist as they can be. Tracks without an artist are never moved
    out of the way and never block anything. The seed song is treated as
    having just played, so its artist cannot open the list.
    """
    tracks = list(tracks)
    if gap <= 0 or len(tracks) < 2:
        return tracks

    seed_key = None
    if seed_artist:
        seed_key = artist_key({"author": seed_artist})

    recent = deque(maxlen=gap)
    if seed_key:
        recent.append(seed_key)

    pending = tracks
    out = []
    while pending:
        pick = None
        for i, track in enumerate(pending):
            key = artist_key(track)
            if key is None or key not in recent:
                pick = i
                break
        if pick is None:
            break
        track = pending.pop(pick)
        out.append(track)
        recent.append(artist_key(track))

    for track in pending:
        key = artist_key(track)
        spot = None
        for index in range(len(out), -1, -1):
            if _fits(out, index, key, gap, seed_key):
                spot = index
                break
        if spot is None:
            out.append(track)
        else:
            out.insert(spot, track)
    return out


def count_violations(tracks, gap=DEFAULT_GAP, seed_artist=None):
    """How many tracks sit closer than `gap` songs to one of the same artist."""
    recent = deque(maxlen=max(gap, 1))
    if seed_artist:
        recent.append(artist_key({"author": seed_artist}))
    bad = 0
    for track in tracks:
        key = artist_key(track)
        if gap > 0 and key is not None and key in recent:
            bad += 1
        recent.append(key)
    return bad


# --------------------------------------------------------------------------
# Talking to the core
# --------------------------------------------------------------------------

def _call_core(app, method, path, query, body, headers, remote_addr):
    """Run one request through the app itself, in its own context.

    It goes through the normal WSGI entry point, so the core authenticates and
    answers it exactly as if the caller had hit the real endpoint. It runs on
    another thread so it gets a fresh application context instead of sharing
    (and tearing down) the one of the request we are serving.
    """
    client = app.test_client(use_cookies=False)
    resp = client.open(
        path,
        method=method,
        query_string=query,
        data=body,
        headers=headers,
        environ_base={"REMOTE_ADDR": remote_addr or "127.0.0.1"},
    )
    return resp.status_code, resp.get_data(), resp.headers.get("Content-Type")


def _seed_id(rest, payload):
    raw = request.args.get("item_id")
    if raw:
        return raw
    if not isinstance(payload, dict):
        return None
    if payload.get("item_id"):
        return str(payload["item_id"])
    if rest == "alchemy":
        items = payload.get("items")
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
            if str(items[0].get("op", "ADD")).upper() == "ADD" and items[0].get("id"):
                return str(items[0]["id"])
    return None


def _seed_artist(rest, payload):
    """Artist of the seed song, or None when it cannot be worked out."""
    raw = _seed_id(rest, payload)
    if not raw:
        return None
    try:
        import app_server_context
        from plugin.api import get_score_data_by_ids

        canonical = app_server_context.resolve_input_item_id(
            raw, payload if isinstance(payload, dict) else None
        )
        for row in get_score_data_by_ids([canonical]) or []:
            author = row["author"]
            if author:
                return author
    except Exception:
        logger.debug("artist_spacing: seed artist lookup failed", exc_info=True)
    return None


def _gap():
    try:
        return max(0, min(MAX_GAP, int(get_setting("gap", DEFAULT_GAP))))
    except (TypeError, ValueError):
        return DEFAULT_GAP


def _respace(rest, raw_body, payload, gap):
    """Return the reordered JSON body, or None to pass the original through."""
    data = json.loads(raw_body)
    list_key = TRACK_ENDPOINTS[rest]
    rows = data if list_key is None else (data.get(list_key) if isinstance(data, dict) else None)
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    if not all(isinstance(row, dict) for row in rows):
        return None

    seeds = [row for row in rows if row.get("is_seed")]
    others = [row for row in rows if not row.get("is_seed")]
    seed_artist = None
    for row in seeds:
        seed_artist = row.get("author") or seed_artist
    if not seed_artist:
        seed_artist = _seed_artist(rest, payload)

    before = count_violations(others, gap, seed_artist)
    if before == 0:
        return None
    spaced = seeds + space_artists(others, gap, seed_artist)
    after = count_violations(spaced[len(seeds):], gap, seed_artist)
    logger.info(
        "artist_spacing: %s - %d tracks, gap %d, seed artist %s, clashes %d -> %d",
        rest, len(others), gap, "known" if seed_artist else "unknown", before, after,
    )

    if list_key is None:
        data = spaced
    else:
        data[list_key] = spaced
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


@bp.route("/api/<path:rest>", methods=["GET", "POST"])
def mirror(rest):
    app = current_app._get_current_object()
    body = request.get_data()
    headers = {name: request.headers[name] for name in FORWARDED_HEADERS if name in request.headers}
    future = _pool.submit(
        _call_core,
        app,
        request.method,
        "/api/" + quote(rest, safe="/"),
        request.query_string.decode("utf-8", "replace"),
        body,
        headers,
        request.remote_addr,
    )
    status, raw_body, content_type = future.result(timeout=CORE_TIMEOUT)

    gap = _gap()
    if status == 200 and gap > 0 and rest.strip("/") in TRACK_ENDPOINTS:
        try:
            payload = json.loads(body) if body else None
            spaced = _respace(rest.strip("/"), raw_body, payload, gap)
            if spaced is not None:
                raw_body = spaced
        except Exception:
            # Whatever goes wrong here, the caller still gets the core's answer.
            logger.exception("artist_spacing: could not reorder %s, passing it through", rest)

    return Response(raw_body, status=status, content_type=content_type or "application/json")


# --------------------------------------------------------------------------
# Settings page (admin only, opened from Manage Plugins)
# --------------------------------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        try:
            gap = max(0, min(MAX_GAP, int(request.form.get("gap", DEFAULT_GAP))))
        except (TypeError, ValueError):
            gap = DEFAULT_GAP
        set_setting("gap", gap)
        return redirect(manage_plugins_url())

    base = request.host_url.rstrip("/") + request.script_root + "/plugins/" + PLUGIN_ID
    body = (
        '<form method="post">'
        "<p><label>Songs of other artists between two songs of the same artist "
        f'<input type="number" name="gap" min="0" max="{MAX_GAP}" value="{_gap()}" '
        'style="width:5em"></label> (0 turns the reordering off)</p>'
        '<button type="submit">Save</button>'
        "</form>"
        "<p>To use it, set the AudioMuse-AI API URL of your client "
        "(for example the Navidrome plugin) to the address AudioMuse-AI is "
        f"reached at, followed by <code>/plugins/{PLUGIN_ID}</code>. From this "
        f"browser that would be <code>{html.escape(base)}</code>. Keep the same API token.</p>"
        "<p>The number of songs per artist is still decided by the "
        "<code>MAX_SONGS_PER_ARTIST</code> setting of AudioMuse-AI; this plugin "
        "only changes where they land.</p>"
    )
    return render_page(body, title="Artist Spacing")


def register(ctx):
    ctx.add_blueprint(bp)
