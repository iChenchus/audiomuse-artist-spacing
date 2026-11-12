# Artist Spacing

A plugin for [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) that
keeps songs of the same artist apart in Instant Mix.

AudioMuse-AI already limits how many songs of one artist a similarity search
returns (`MAX_SONGS_PER_ARTIST`, 3 by default), but not where they land. The
songs closest to the seed are usually by the seed's own artist, so a mix tends
to open with the same band several times in a row.

With this plugin, two songs of the same artist are separated by at least 3
songs of other artists. The seed song counts, so its artist cannot come back
before the fourth slot. The number is configurable. The list keeps the same
songs and, as far as the rule allows, the same order.

## How it works

The plugin mirrors the AudioMuse-AI API under `/plugins/artist_spacing/api/`.
Every request is handed to the real endpoint with the caller's own
credentials, and the answer comes back unchanged, except for these endpoints,
whose track list is reordered:

- `GET /api/similar_tracks`
- `POST /api/sem_grove/search`
- `POST /api/hyperbolic/similar`
- `POST /api/clap/search_by_song`
- `POST /api/alchemy`

Those are the five sources the Navidrome plugin can use for Instant Mix.
Sonic paths, artist similarity and everything else pass through untouched.
If reordering fails for any reason, the original answer is returned.

## Install

1. In AudioMuse-AI open Plugins > Repositories and add this repository's
   `manifest.json` (the raw URL).
2. Install Artist Spacing from the Catalog tab and apply the restart.
3. In the client, add `/plugins/artist_spacing` to the AudioMuse-AI API URL.
   For the Navidrome plugin: Settings > Plugins > AudioMuse-AI > API URL, for
   example `http://audiomuse:8000/plugins/artist_spacing`. Keep the API token.

To go back, restore the original API URL. To change the distance, open the
plugin's Settings from Manage Plugins (0 turns the reordering off).

## Notes

- Artists are compared by the track's `author`, ignoring case and extra
  spaces. "Band" and "Band feat. Someone" count as different artists.
- Tracks without an artist are left where they are.
- When a list does not have enough different artists to honour the distance,
  the songs that do not fit go last, as far apart as possible.
- Requires AudioMuse-AI 3.6.4 or newer. Runs on the Flask container only.

## Development

    python -m pytest tests
    python build.py OWNER/REPO

`build.py` zips the code into `dist/` and writes `manifest.json` plus the
download URL and checksum in `plugin.json`. Published versions are never
rebuilt: add a new entry at the top of `versions` in `plugin.json`.

## License

AGPL-3.0, the same as AudioMuse-AI. See [LICENSE](LICENSE).
