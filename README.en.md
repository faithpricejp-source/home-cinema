[中文](README.md)

# Home Cinema · Local Media Library

A local, Infuse-style media library app for macOS: scans your movie / TV folders, fetches posters and overviews from TMDB, shows a dark poster wall, and plays everything in-app via libmpv — with resume, "continue watching", auto next episode, and automatic intro / end-credits skipping for TV shows.

- **Home Cinema.app**: a Swift shell (WKWebView renders the poster wall + embedded libmpv playback) that starts the local Python service on launch.
- Backend in Python (FastAPI + SQLite), frontend in plain HTML/CSS/JS (no build step, no CDN), and the service only listens on `127.0.0.1:8770`.
- You can also just open `http://127.0.0.1:8770` in a browser; playback is then handed over to IINA.

## Dependencies

- macOS 13+, Apple Silicon (Intel untested)
- Homebrew: `brew install python mpv ffmpeg chromaprint` (libmpv comes from mpv; chromaprint provides `fpcalc`, used to identify intros / end credits)
- Xcode Command Line Tools (`swiftc`, to build the app)
- Optional: IINA (playback in browser mode, or as a fallback when the embedded player is unavailable)
- Optional: a TMDB API key (free to request at: https://www.themoviedb.org/settings/api ). It works without one, you just get no posters and no overviews

## Installation

```bash
git clone https://github.com/faithpricejp-source/home-cinema.git
cd home-cinema
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx pytest numpy
cp config.example.toml config.toml   # then edit it as described in the next section
macapp/build.sh                      # build Home Cinema.app and copy it to /Applications
```

Store the TMDB key as a plain text file containing nothing but the key (default `~/.config/tmdb/api-key.txt`); do not write it into the repo.

## Configuration

```bash
cp config.example.toml config.toml
```

Edit `config.toml`:

```toml
movie_roots = ["/path/to/Movie"]                 # movie root directories
tv_roots = ["/path/to/TV/已完结", "/path/to/TV/未完结"]  # TV root directories
db_path = "~/Library/Application Support/HomeCinema/library.db"
cache_dir = "~/Library/Caches/HomeCinema"        # poster / still-image cache
tmdb_key_file = "~/.config/tmdb/api-key.txt"     # the TMDB v3 API key is read only from this file
tmdb_language = "zh-CN"
port = 8770
iina_cli = "/Applications/IINA.app/Contents/MacOS/iina-cli"
```

In the example above the two TV folders are Chinese placeholder names: `已完结` means "completed series" and `未完结` means "ongoing series"; they are only sample paths, the directory names carry no meaning for the scanner.

- `config.toml` is already listed in `.gitignore`, do not commit it.
- Paths support `~`; the config file defaults to `config.toml` in the project root and can be
  overridden with the environment variable `HOMECINEMA_CONFIG=/path/to/config.toml`.
- It also works without a TMDB key: scanning and playback behave normally, entries are shown as
  「未匹配」 (Unmatched) with a placeholder poster.

## Directory Layout

- Movies: `<movie_root>/<title> (<year>)/<title> (<year>).<ext>`, one folder per movie.
- TV shows: `<tv_root>/<series name>[ (<year>)]/Season NN/<series name> SxxExx.<ext>`;
  when there is no `Season NN` level, the videos sit directly under `<series name>/` (the season number is taken from SxxExx in the file name).
- Video extensions: `mp4 mkv avi rmvb m4v mov ts wmv flv webm` (case-insensitive).
- A movie folder may contain `.nfo` (if it includes `<tmdbid>` that id is used directly and no search is done),
  `poster.jpg` / `folder.jpg` / a `.jpg`/`.png` named the same as the video (a local poster takes priority over TMDB), and `.srt`/`.ass` subtitles (loaded automatically by IINA).

## Running

```bash
.venv/bin/python -m homecinema scan             # scan the library into the database (incremental; vanished files are marked missing, progress is kept)
.venv/bin/python -m homecinema fetch-metadata   # fetch TMDB metadata, download posters / stills into the cache
.venv/bin/python -m homecinema serve            # start the web service, open http://127.0.0.1:8770 in a browser
.venv/bin/python -m homecinema recommend        # optional: build the Recommendations page from your library and watch history (needs a TMDB key)
```

You can also click 「重新扫描」 (Rescan) in the top-right corner of the web page (runs scan + fills in missing metadata in the background, the button shows progress).

## Usage

- Home page: continue watching (wide cards with a progress bar), recently added, movie / TV poster walls.
- Click 「播放/继续播放」 (Play / Resume) → `iina-cli` is called to open the file; in the background progress is recorded over the mpv IPC every 5 seconds.
- Watched 90% (or fewer than 3 minutes left, only for titles longer than 10 minutes) is marked as watched automatically; next time it plays from the beginning.
- If you didn't finish, clicking Play again resumes from the last position; a position under 30 seconds counts as not watched.
- The TV show detail page lets you switch seasons and click any episode to play; 「继续：SxxExx」 (Continue: SxxExx) at the top jumps straight to the next episode.

## Embedded Player (Playback Inside the App)

Clicking Play inside Home Cinema.app renders the picture in the same window (the Swift shell
renders through libmpv, the on-screen controller is mpv's own OSC), no separate IINA window is
opened; when playback finishes you return to the poster wall, 「继续观看」 (Continue Watching) and the progress bars
refresh automatically, and after an episode finishes the next one starts on its own. Behaviour over
browser access is unchanged (it is still handed to IINA). Writing progress to the database, the
watched rule (90% or fewer than 3 minutes left) and the resume start point share one implementation
across both paths.

### Keyboard Shortcuts (in-app, mpv default bindings)

- Space: pause / resume
- ← / →: seek backward / forward 5 seconds; ↑ / ↓: seek backward / forward 1 minute
- m: mute; j: cycle subtitle tracks; #: cycle audio tracks
- `[` / `]`: slower / faster playback; scroll wheel: seek backward / forward
- f: window fullscreen (the app toggles the window; mpv's own fullscreen is not used)
- ESC: when fullscreen, exit fullscreen first; press it again to end playback and return to the poster wall
- Moving the mouse brings up the OSC control bar, where you can press buttons and drag the progress bar

When a file has embedded subtitles but no default track, one is picked by language (all Chinese
variants first, then English).

### Fallback

If libmpv initialization fails (a dylib renamed/corrupted, render context creation failing, and so
on), an alert says 「内嵌播放器不可用，改用 IINA」 (embedded player unavailable, switching to IINA)
and that playback falls back to the v1 IINA path.

## Intro / End-Credits Skipping

Applies only to **TV shows** (movies are unaffected). When playing in the embedded player, the intro
and the end credits of each episode are skipped by default; skipping the end credits reuses the
"playback finished" logic, so the episode is still marked as watched and the next episode still starts
on its own.

Intro / end-credit positions are computed ahead of time by the detection command and stored in the
database (the `segments` table):

```bash
.venv/bin/python -m homecinema detect-segments                    # process every season not detected yet
.venv/bin/python -m homecinema detect-segments --show 12          # process only show ID=12
.venv/bin/python -m homecinema detect-segments --limit-seasons 3  # process at most 3 seasons
```

- Chapters first: for each episode `ffprobe` is used to read the chapters, and if they determine an intro / end credit, the chapter result is used (`source='chapters'`).
- Every episode of the season is then compared by audio fingerprint to fill in whatever the chapters did not provide (fingerprint only: `source='fingerprint'`; chapters plus fingerprint: `source='chapters+fingerprint'`; chapter values are never overwritten): the intro window is the first `min(600, duration×0.35)` seconds,
  the end-credit window is the last 300 seconds; a season with only 1 episode skips fingerprinting. Episodes where neither method yields anything are recorded as `source='none'`.
- Each season is written to the database as soon as it is done: if the run is interrupted, re-running it skips the seasons already finished.
- `GET /api/segments?episode_id=N` shows the detection result for one episode (for debugging).

The app menu 「显示 → 跳过片头片尾」 (View → Skip Intro/Outro) is the master switch (on by default, stored in UserDefaults as `skipIntroOutro`).
When an intro is skipped the overlay shows 「已跳过片头 · 按 ← 回看」 (Intro skipped · press ← to rewind); dragging back into the intro
manually will not skip again, and turning the switch off stops skipping immediately for the episode currently playing.

## Tests

```bash
.venv/bin/pytest -q
```

All tests use invented titles and fake HTTP clients / fake mpv sockets; they make no network access and never start a player.

## Notes

This product uses the TMDB API but is not endorsed or certified by TMDB; metadata and images come from
[themoviedb.org](https://www.themoviedb.org).

Entries that match badly can be pinned manually in `~/Library/Application Support/HomeCinema/overrides.toml`:

```toml
[movie]
"Some Movie (1999)" = 12345   # folder name = TMDB id
[tv]
"Some Show" = 67890
```

`tools/audit_matches.py` looks up the English title and year behind every match and lists the suspicious ones.

The approach to intro / end-credit detection was inspired by Jellyfin's [Intro Skipper](https://github.com/intro-skipper/intro-skipper) plugin (Chromaprint audio-fingerprint comparison across a season).

## License

GPL-3.0 (the app links against a GPL build of libmpv).
