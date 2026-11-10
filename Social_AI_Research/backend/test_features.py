"""Comprehensive test suite for Participant Blocking and Hourly Data Export features."""

import csv
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

import auth
import db
import main
import pangram
from admin import queries
from admin.router import _parse_identifiers_from_file_bytes, _parse_identifiers_from_text
from hourly_export import export_database_to_zip, json_serializer


def run_coro(coro):
    """Step a coroutine to completion without opening loopback sockets (sandbox-safe)."""
    try:
        while True:
            res = coro.send(None)
            if hasattr(res, "send"):
                run_coro(res)
    except StopIteration as e:
        return e.value


# ────────────────────── 1. Identifier Parser Tests ──────────────────────

def test_parse_identifiers_from_text():
    raw_text = """
    5f4d81234abcde56789f0123
    672a912ef0134b791240a5bc, u-89ab12cd34ef5678; "custom_pid_99"
       another_id_123   
    """
    res = _parse_identifiers_from_text(raw_text)
    assert "5f4d81234abcde56789f0123" in res
    assert "672a912ef0134b791240a5bc" in res
    assert "u-89ab12cd34ef5678" in res
    assert "custom_pid_99" in res
    assert "another_id_123" in res
    assert len(res) == 5


def test_parse_identifiers_from_csv_with_header():
    csv_content = b"""Participant id,Submission id,Status
5f4d81234abcde56789f0123,sub_1,APPROVED
672a912ef0134b791240a5bc,sub_2,REJECTED
"""
    res = _parse_identifiers_from_file_bytes(csv_content)
    assert res == ["5f4d81234abcde56789f0123", "672a912ef0134b791240a5bc"]


def test_parse_identifiers_from_csv_with_bom():
    csv_content = "\ufeffProlific ID\npid_101\npid_102\n".encode("utf-8")
    res = _parse_identifiers_from_file_bytes(csv_content)
    assert res == ["pid_101", "pid_102"]


def test_parse_identifiers_from_plain_list_file():
    txt_content = b"id_1\nid_2\nid_3\n"
    res = _parse_identifiers_from_file_bytes(txt_content)
    assert res == ["id_1", "id_2", "id_3"]


# ────────────────────── 2. Auth Blocking Verification ──────────────────────

def test_auth_blocks_blocked_participant():
    async def _test():
        mock_db = MagicMock()
        mock_db.participants.find_one = AsyncMock(return_value={
            "_id": ObjectId(),
            "participantId": "u-test123",
            "externalId": "prolific_test",
            "token": "tok_123",
            "active": True,
            "blocked": True,
            "blockedReason": "Attention check failure",
        })

        with patch.object(db.state, "db", mock_db):
            creds = MagicMock(credentials="tok_123")
            with pytest.raises(HTTPException) as exc_info:
                await auth.current_participant(creds)
            assert exc_info.value.status_code == 403
            assert "Participant is blocked" in exc_info.value.detail

    run_coro(_test())


def test_auth_blocks_via_blocked_participants_collection():
    async def _test():
        mock_db = MagicMock()
        part_id = ObjectId()
        mock_db.participants.find_one = AsyncMock(return_value={
            "_id": part_id,
            "participantId": "u-test456",
            "externalId": "prolific_test2",
            "token": "tok_456",
            "active": True,
            "blocked": False,
        })
        mock_db.blocked_participants.find_one = AsyncMock(return_value={
            "identifier": "prolific_test2",
            "reason": "Batch blacklist",
        })
        mock_db.participants.update_one = AsyncMock()

        with patch.object(db.state, "db", mock_db):
            creds = MagicMock(credentials="tok_456")
            with pytest.raises(HTTPException) as exc_info:
                await auth.current_participant(creds)
            assert exc_info.value.status_code == 403
            assert "Participant is blocked" in exc_info.value.detail
            mock_db.participants.update_one.assert_called_once()

    run_coro(_test())


# ────────────────────── 3. Score Endpoint Never Calls Pangram When Blocked ──────────────────────

def test_score_endpoint_blocks_and_never_calls_pangram():
    async def _test():
        mock_participant = {
            "participantId": "u-blocked1",
            "blocked": True,
        }
        req = main.ScoreRequest(
            platform="twitter",
            postId="12345",
            text="A sufficiently long text for scoring to pass the minimum character count requirement..." * 5,
        )

        with patch.object(pangram, "score_text", AsyncMock()) as mock_pangram:
            with pytest.raises(HTTPException) as exc_info:
                await main.score(req, participant=mock_participant)
            assert exc_info.value.status_code == 403
            assert "Participant is blocked" in exc_info.value.detail
            mock_pangram.assert_not_called()

    run_coro(_test())


# ────────────────────── 4. Block Identifiers Logic ──────────────────────

class FakeListCursor:
    def __init__(self, items):
        self.items = items

    async def to_list(self, length):
        return self.items


def test_block_identifiers_marks_and_saves():
    async def _test():
        mock_db = MagicMock()
        # Suppose we block two IDs: one existing participant by externalId, one new Prolific ID
        existing_part = {
            "_id": ObjectId("672a912ef0134b791240a5bc"),
            "participantId": "u-existing01",
            "externalId": "prolific_known",
            "blocked": False,
        }

        def mock_find(query):
            clauses = query.get("$or", [])
            for c in clauses:
                if c.get("externalId") == "prolific_known":
                    return FakeListCursor([existing_part])
            return FakeListCursor([])

        mock_db.participants.find.side_effect = mock_find
        mock_db.participants.update_one = AsyncMock()
        mock_db.blocked_participants.update_one = AsyncMock()

        summary = await queries.block_identifiers(
            mock_db,
            ["prolific_known", "prolific_unknown_future"],
            reason="Quality check failed"
        )

        assert summary["total_parsed"] == 2
        assert summary["unique_identifiers"] == 2
        assert summary["matched_participants_count"] == 1
        assert summary["new_blocklist_entries"] == 1

        # Check that participant was marked blocked
        mock_db.participants.update_one.assert_called()
        # Check that both were saved to blocked_participants
        assert mock_db.blocked_participants.update_one.call_count >= 2

    run_coro(_test())


# ────────────────────── 5. Hourly Export Atomically Replaces exports.zip ──────────────────────

class FakeAsyncCursor:
    def __init__(self, docs):
        self.docs = docs

    def __aiter__(self):
        self._iter = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def test_hourly_export_replaces_single_zip(tmp_path):
    async def _test():
        target_zip = tmp_path / "exports.zip"

        # Pre-create an old exports.zip
        with zipfile.ZipFile(target_zip, "w") as zf:
            zf.writestr("old.txt", "old export content")
        assert target_zip.exists()

        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_client.__getitem__.return_value = mock_db

        mock_db.participants.find.return_value = FakeAsyncCursor([
            {
                "_id": ObjectId("672a912ef0134b791240a5bc"),
                "participantId": "u-001",
                "externalId": "prolific_001",
                "group": "treatment",
                "cohort": 1,
                "blocked": True,
                "blockedReason": "Attention check",
                "createdAt": datetime.now(timezone.utc),
            }
        ])
        mock_db.observations.find.return_value = FakeAsyncCursor([])
        mock_db.interactions.find.return_value = FakeAsyncCursor([])
        mock_db.dwell_logs.find.return_value = FakeAsyncCursor([])
        mock_db.scored_posts.find.return_value = FakeAsyncCursor([])
        mock_db.blocked_participants.find.return_value = FakeAsyncCursor([
            {
                "identifier": "prolific_001",
                "identifierType": "prolific_id",
                "reason": "Attention check",
                "blockedAt": datetime.now(timezone.utc),
            }
        ])

        with patch("hourly_export.AsyncIOMotorClient", return_value=mock_client):
            result_path = await export_database_to_zip("mongodb://fake:27017", "sair_test", target_zip)

        assert result_path == target_zip
        assert target_zip.exists()

        # Verify no multiple files like exports_1.zip, exports.zip.tmp exist
        files_in_dir = list(tmp_path.iterdir())
        assert len(files_in_dir) == 1
        assert files_in_dir[0].name == "exports.zip"

        # Verify zip content
        with zipfile.ZipFile(target_zip, "r") as zf:
            namelist = zf.namelist()
            assert "participants.csv" in namelist
            assert "participants.json" in namelist
            assert "blocked_participants.csv" in namelist
            assert "manifest.json" in namelist
            assert "old.txt" not in namelist  # Fully replaced old file

            manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
            assert manifest_data["database"] == "sair_test"
            assert manifest_data["collections"]["participants"] == 1
            assert manifest_data["collections"]["blocked_participants"] == 1

    run_coro(_test())


def test_backend_automatic_export_uses_db_directly(tmp_path):
    """Test the direct database export method used by the backend automatic daemon."""
    from hourly_export import export_database

    async def _test():
        target_zip = tmp_path / "backend_auto_exports.zip"
        mock_db = MagicMock()
        mock_db.name = "sair_auto"
        mock_db.participants.find.return_value = FakeAsyncCursor([
            {
                "_id": ObjectId("672a912ef0134b791240a5bc"),
                "participantId": "u-auto1",
                "externalId": "prolific_auto1",
                "group": "control",
                "cohort": 1,
                "blocked": False,
                "createdAt": datetime.now(timezone.utc),
            }
        ])
        mock_db.observations.find.return_value = FakeAsyncCursor([])
        mock_db.interactions.find.return_value = FakeAsyncCursor([])
        mock_db.dwell_logs.find.return_value = FakeAsyncCursor([])
        mock_db.scored_posts.find.return_value = FakeAsyncCursor([])
        mock_db.blocked_participants.find.return_value = FakeAsyncCursor([])

        res = await export_database(mock_db, target_zip)
        assert res == target_zip
        assert target_zip.exists()

        with zipfile.ZipFile(target_zip, "r") as zf:
            assert "participants.csv" in zf.namelist()
            assert "manifest.json" in zf.namelist()
            manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
            assert manifest_data["database"] == "sair_auto"
            assert manifest_data["collections"]["participants"] == 1

    run_coro(_test())


# ────────────────────── 4. MongoDB Disconnection Resilience Tests ──────────────────────

def test_db_is_connected_when_client_is_none():
    db.state.client = None
    db.state.db = None
    async def _test():
        connected = await db.is_connected()
        assert connected is False
    run_coro(_test())


def test_db_connect_handles_unreachable_server_gracefully():
    with patch("db.AsyncIOMotorClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        with patch("db._ensure_indexes", side_effect=Exception("Connection refused")):
            async def _test():
                await db.connect()
                assert db.state.client is not None
            run_coro(_test())


# ────────────────────── 5. Phone Alert (ntfy.sh) Tests ──────────────────────

def test_ntfy_disabled_when_topic_empty():
    import alerts
    with patch.dict(os.environ, {"NTFY_TOPIC": ""}):
        async def _test():
            sent = await alerts.send_ntfy_notification("Test Title", "Test Message")
            assert sent is False
        run_coro(_test())


def test_ntfy_sends_post_request():
    import alerts
    with patch.dict(os.environ, {"NTFY_TOPIC": "test-sair-topic"}):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post.return_value = mock_resp
        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_client
            async def _test():
                sent = await alerts.send_ntfy_notification("Alert Title", "Mongo Down", priority="urgent")
                assert sent is True
                assert mock_client.post.called
                args, kwargs = mock_client.post.call_args
                assert "test-sair-topic" in args[0]
                assert kwargs["headers"]["Title"] == "Alert Title"
                assert kwargs["headers"]["Priority"] == "urgent"
            run_coro(_test())



