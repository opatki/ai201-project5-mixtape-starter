"""
tests/test_feed.py — Mixtape

Tests for the "Friends Listening Now" feed logic.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from app import create_app, db
from models import User, Song, ListeningEvent, friendships
import services.feed_service as feed_service

FIXED_NOW = datetime(2024, 6, 11, 1, 0, 0, tzinfo=timezone.utc)  # 1:00 AM


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def me_and_friends(app):
    """One user with three friends, each with a listening event at a different age."""
    with app.app_context():
        me = User(username="me", email="me@example.com")
        yesterday_friend = User(username="yesterday_friend", email="yf@example.com")
        current_friend = User(username="current_friend", email="cf@example.com")
        old_friend = User(username="old_friend", email="of@example.com")
        db.session.add_all([me, yesterday_friend, current_friend, old_friend])
        db.session.flush()

        for f in (yesterday_friend, current_friend, old_friend):
            db.session.execute(friendships.insert().values(user_id=me.id, friend_id=f.id))
            db.session.execute(friendships.insert().values(user_id=f.id, friend_id=me.id))

        song = Song(title="Some Track", artist="Some Artist", shared_by=me.id)
        db.session.add(song)
        db.session.flush()

        db.session.add_all([
            ListeningEvent(user_id=yesterday_friend.id, song_id=song.id,
                            listened_at=FIXED_NOW - timedelta(hours=23)),
            ListeningEvent(user_id=current_friend.id, song_id=song.id,
                            listened_at=FIXED_NOW - timedelta(minutes=5)),
            ListeningEvent(user_id=old_friend.id, song_id=song.id,
                            listened_at=FIXED_NOW - timedelta(days=3)),
        ])
        db.session.commit()

        yield {
            "me": me,
            "yesterday_friend": yesterday_friend,
            "current_friend": current_friend,
            "old_friend": old_friend,
        }


def _get_listening_now(user_id):
    with patch("services.feed_service.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        return feed_service.get_friends_listening_now(user_id)


def test_excludes_friend_from_23_hours_ago(app, me_and_friends):
    """
    A friend who listened 23 hours ago is from a different calendar day
    ("yesterday") and should not show up in "listening now".
    """
    with app.app_context():
        result = _get_listening_now(me_and_friends["me"].id)
        usernames = [entry["friend"]["username"] for entry in result]
        assert "yesterday_friend" not in usernames


def test_includes_genuinely_recent_friend(app, me_and_friends):
    """A friend who listened 5 minutes ago should show up in "listening now"."""
    with app.app_context():
        result = _get_listening_now(me_and_friends["me"].id)
        usernames = [entry["friend"]["username"] for entry in result]
        assert "current_friend" in usernames


def test_excludes_friend_from_days_ago(app, me_and_friends):
    """A friend who listened 3 days ago should not show up in "listening now"."""
    with app.app_context():
        result = _get_listening_now(me_and_friends["me"].id)
        usernames = [entry["friend"]["username"] for entry in result]
        assert "old_friend" not in usernames
