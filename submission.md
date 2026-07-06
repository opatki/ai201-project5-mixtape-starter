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

## Bug fixes

### Issue 1 — My listening streak keeps resetting

**Location:** `services/streak_service.py`, `update_listening_streak()`.

**Root cause:** The consecutive-day branch was gated by an extra condition that has nothing to do with streak correctness:
```python
elif days_since_last == 1 and today.weekday() != 6:
    user.listening_streak += 1
else:
    user.listening_streak = 1
```
`days_since_last == 1` already means "listened yesterday, listening again today" — that's a complete, correct definition of a consecutive day. The `and today.weekday() != 6` clause additionally requires that today not be a Sunday. Whenever today *is* Sunday, the condition is false regardless of `days_since_last`, so execution falls through to `else` and the streak resets to 1 — even though the user listened yesterday too. This contradicts the function's own docstring, which states the increment rule with no day-of-week exception.

**How I reproduced it:** Checked out the pre-fix version of `services/streak_service.py` from git history (`git show HEAD~1:services/streak_service.py`) and called the real `update_listening_streak(user, now)` function directly (same function `record_listening_event()` calls on every `POST /songs/<song_id>/listen`) for a single user, once per day, for 14 consecutive calendar days with zero gaps — Monday, June 10, 2024 through Sunday, June 23, 2024. Printed the resulting `user.listening_streak` after each call. Actual output:
```
2024-06-10 (Monday   ) -> streak = 1
2024-06-11 (Tuesday  ) -> streak = 2
2024-06-12 (Wednesday) -> streak = 3
2024-06-13 (Thursday ) -> streak = 4
2024-06-14 (Friday   ) -> streak = 5
2024-06-15 (Saturday ) -> streak = 6
2024-06-16 (Sunday   ) -> streak = 1   <- resets despite no gap
2024-06-17 (Monday   ) -> streak = 2
2024-06-18 (Tuesday  ) -> streak = 3
2024-06-19 (Wednesday) -> streak = 4
2024-06-20 (Thursday ) -> streak = 5
2024-06-21 (Friday   ) -> streak = 6
2024-06-22 (Saturday ) -> streak = 7
2024-06-23 (Sunday   ) -> streak = 1   <- resets again, same pattern
```
The streak resets to 1 every single Sunday, regardless of an unbroken daily listening history — a weekly, deterministic reset. This is also the exact scenario the pre-existing (but previously failing) test `test_streak_increments_on_sunday` in `tests/test_streaks.py` was written to catch.

**Fix:** Removed the `and today.weekday() != 6` clause so the increment applies uniformly on every consecutive day:
```python
elif days_since_last == 1:
    user.listening_streak += 1
```

**Verification:** `pytest tests/test_streaks.py` — all 5 tests pass, including `test_streak_increments_on_sunday`, which failed (`assert 1 == 2`) before the fix.

---

### Issue 5 — The last song in a playlist never shows up

**Location:** `services/playlist_service.py`, `get_playlist_songs()`.

**Root cause:** The SQL query itself is correct — it joins `Song` through the `playlist_entries` association table, filters by `playlist_id`, and orders by the `position` column ascending, producing the full, correctly-ordered list of songs. The bug is a stray list slice applied to that already-correct result, right before serialization:
```python
return [song.to_dict() for song in songs[:-1]]
```
`songs[:-1]` drops the last element of any list. Since `songs` is already the complete, ordered result set, this unconditionally discards the final song in the playlist — not an off-by-one in the query or the `position` values, just a trailing item dropped in Python after the database already returned the right rows.

**How I reproduced it:** Stashed the working-tree fix (`git stash push -- services/playlist_service.py`) to expose the pre-fix version, then created one user, five songs ("Track 1"–"Track 5"), and one playlist with all five songs inserted into `playlist_entries` at positions 1–5 — the same shape `GET /playlists/<id>/songs` → `get_playlist_songs()` reads. Called the real `get_playlist_songs(playlist.id)` function and compared what was inserted vs. what it returned. Actual output:
```
Inserted 5 songs: ['Track 1', 'Track 2', 'Track 3', 'Track 4', 'Track 5']
get_playlist_songs() returned 4 songs: ['Track 1', 'Track 2', 'Track 3', 'Track 4']
```
"Track 5" — the song at the highest `position` — is silently missing from the response for a playlist of any size greater than zero. This matches the pre-existing (but previously failing) tests `test_playlist_returns_all_songs` (expected 5, got 4) and `test_playlist_returns_songs_in_order` (expected `Track 5` at the end, missing entirely) in `tests/test_playlists.py`.

**Fix:** Removed the slice so the full ordered result set is returned:
```python
return [song.to_dict() for song in songs]
```

**Verification:** `pytest tests/test_playlists.py` — all 3 tests pass, including the two that failed before the fix.
