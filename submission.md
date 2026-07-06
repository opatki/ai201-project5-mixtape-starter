# Codebase Map — Mixtape

## Main files

**`app.py`** — Flask application factory (`create_app()`). Creates a single `db = SQLAlchemy()` instance at module scope (imported by `models.py` and every service to avoid circular imports), reads `DATABASE_URL`/`SECRET_KEY` from env with sqlite fallback, registers four blueprints (`songs`, `playlists`, `users`, `feed`) under matching URL prefixes, and calls `db.create_all()` inside the app context. No models are defined here — this file is purely wiring.

**`models.py`** — All 7 SQLAlchemy models plus 3 association tables:
- `User` — has `listening_streak` and `last_listened_at` columns baked directly onto the row (not a separate streak table), plus a self-referential many-to-many `friends` relationship via the `friendships` table.
- `Song` — owns `shared_by` (FK to User) and `share_note`; tags come through the `song_tags` join table.
- `ListeningEvent` — one row per (user, song, timestamp) play. This is what both the streak and feed features are computed from — there's no separate "streak" table, streaks are derived state on `User`.
- `Rating` — one row per (user, song), enforced by a `UniqueConstraint`. Rating and re-rating go through the same code path (upsert).
- `Playlist` — songs come through `playlist_entries`, not a plain many-to-many. That table carries extra columns (`position`, `added_by`, `added_at`) that a bare association table wouldn't need — ordering and provenance for playlist membership live in the join row itself, not on `Song` or `Playlist`.
- `Tag` / `song_tags` — plain many-to-many tagging for songs.
- `Notification` — flat table: `user_id` (recipient), `notification_type` string, `body` text, `read` boolean. No polymorphic "subject" reference back to the song/rating/playlist that caused it — the triggering context is baked into the `body` string at creation time, not reconstructable from the row alone.

Every model has a hand-written `to_dict()` — there's no serialization library; routes/services build JSON responses by calling `.to_dict()` directly.

**`routes/`** — one blueprint per resource (`songs.py`, `playlists.py`, `users.py`, `feed.py`). Every route follows the same shape: parse `request.get_json()`/`request.args`, validate required fields inline (return 400 if missing), call exactly one service function, catch `ValueError` and turn it into a 404 or 400 with `{"error": str(e)}`. Routes contain zero query logic and zero business rules.

**`services/`** — where all the logic actually lives, one file per feature area:
- `streak_service.py` — `record_listening_event()` + `update_listening_streak()`. Streak math (consecutive-day increment vs. reset) happens here, not in the model.
- `feed_service.py` — `get_friends_listening_now()` (last 24h, deduped to one song per friend) and `get_activity_feed()` (last N events, no recency filter). Both walk `user.friends` to get the friend ID list, then query `ListeningEvent`.
- `search_service.py` — `search_songs()` (title/artist `ILIKE` match) and `get_song()`.
- `notification_service.py` — `create_notification()` is the single low-level constructor; `add_to_playlist()` and `rate_song()` are the two call sites that trigger notification-worthy events, plus `get_notifications()`/`mark_as_read()` for reading them back.
- `playlist_service.py` — `create_playlist()`, `get_playlist()`, `get_playlist_songs()` (ordered by the `position` column on `playlist_entries`), `get_user_playlists()`.

**`seed_data.py`** — drops and recreates all tables, then inserts 5 users with friendships, 25 songs (deliberately split into 0-tag / 1-tag / 3+-tag groups), 3 playlists, a mix of recent (<30 min) and old (1–14 day) listening events, and one pre-existing notification. The recent-vs-old event split and the tag-count split are clearly there to exercise edge cases in the feed and search services.

**`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py`, one per service under scrutiny. No `test_notifications.py` or `test_feed.py`, even though those services exist — coverage isn't uniform across `services/`.

## Data flow — user rates a song

1. `POST /songs/<song_id>/rate` with `{user_id, score}` hits `rate()` in [routes/songs.py](routes/songs.py#L29).
2. The route does presence validation only, then calls `notification_service.rate_song(user_id, song_id, int(score))`.
3. `rate_song()` in [services/notification_service.py](services/notification_service.py#L73) validates the score range (1–5), loads the `Song` and `User` to confirm they exist, checks for an existing `Rating` row for that `(user_id, song_id)` pair (enforced at the DB level by a `UniqueConstraint`), and either updates the existing row's `score` or inserts a new `Rating`. It commits and returns the `Rating`.
4. The route serializes the returned `Rating.to_dict()` and responds `201`.

The notable thing tracing this end-to-end: `rate_song()` lives in `notification_service.py` — a strong signal that rating a song is *supposed* to notify the original sharer, the same way `add_to_playlist()` (right above it in the same file) calls `create_notification()` when a friend adds your shared song to a playlist. But `rate_song()` never calls `create_notification()`. Compare the two functions side by side: `add_to_playlist()` ends with an `if song.shared_by != added_by_user_id: create_notification(...)` block; `rate_song()` has no equivalent block at all. So today, rating a song updates the `Rating` table but produces no `Notification` row and no entry in the recipient's `/users/<id>/notifications` feed — the sharer never finds out their song was rated, only that it was added to a playlist.

## Patterns noticed

- **Routes are thin, services own logic.** Every route file does input parsing + `try/except ValueError` → HTTP status translation, and delegates the actual query/mutation to a same-named or clearly-named service function. No route touches `db.session` directly except the trivial `GET /users/<id>` lookup in `routes/users.py`.
- **`ValueError` is the universal "not found / invalid" signal** between services and routes — services never return `None` or raise custom exception types; routes uniformly catch `ValueError` and map it to 400 or 404 depending on the route.
- **String UUIDs everywhere**, generated via a shared `generate_uuid()` default rather than integer autoincrement PKs — makes IDs opaque and stable across `db.drop_all()`/reseed cycles.
- **Derived state stored on the row, not computed on read.** `User.listening_streak` and `User.last_listened_at` are mutated in place by `streak_service`, rather than recalculated from `ListeningEvent` history on every request. Same idea shows up with `Notification.read` as a mutable flag rather than a separate read-receipts table.
- **Association tables double as data, not just links.** `playlist_entries` carries `position`/`added_by`/`added_at` — ordering and attribution live in the join table, which is why `playlist_service.get_playlist_songs()` has to explicitly `.order_by(asc(playlist_entries.c.position))` rather than relying on relationship default ordering.
- **Notifications are fire-and-forget, one-directional strings.** `create_notification()` takes a free-text `body` built by the caller (e.g. an f-string embedding `adder.username`, `song.title`, `playlist.name`) rather than storing structured references — the notification can't be traced back to "which rating/which playlist-entry caused this," only read as a rendered message.
- **Feature coverage in `tests/` mirrors the assignment's bug list, not the full service surface** — `streak_service`, `search_service`, and `playlist_service` all have dedicated test files; `notification_service` and `feed_service` don't, despite being just as central to the app's behavior.
