"""Tests for document creation, scheduling, and bulk payloads."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from common import LogEntry
from elastic_mock_logger import BulkWriteError
from elastic_mock_logger import EventScheduler
from elastic_mock_logger import bulk_operations
from elastic_mock_logger import create_document
from elastic_mock_logger import write_bulk


class Clock:
    def __init__(self):
        self.seconds = 0.0

    def monotonic(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc) + timedelta(
            seconds=self.seconds
        )


def test_scheduler_creates_events_at_the_requested_rate_with_utc_timestamp():
    clock = Clock()
    scheduler = EventScheduler(
        [LogEntry("application", 2, None, True)],
        3,
        monotonic=clock.monotonic,
        now=clock.now,
        random_choice=lambda _words: "word",
    )

    clock.seconds = 29
    assert scheduler.generate_due_documents() == []
    clock.seconds = 30
    documents = scheduler.generate_due_documents()

    assert documents == [{
        "@timestamp": "2026-09-30T12:00:30.000Z",
        "message": "word word word",
        "event": {"dataset": "application"},
    }]


def test_scheduler_emits_exactly_180_events_in_the_first_complete_minute():
    clock = Clock()
    scheduler = EventScheduler(
        [LogEntry("high-rate", 180, "message", False)],
        1,
        monotonic=clock.monotonic,
        now=clock.now,
        random_choice=lambda _words: "unused",
    )

    clock.seconds = 60
    documents = scheduler.generate_due_documents()

    assert len(documents) == 180
    assert scheduler.generate_due_documents() == []


def test_fixed_message_is_preserved_in_document():
    document = create_document(
        LogEntry("audit", 1, "line one\nline two", False),
        2,
        lambda _words: "unused",
        lambda: datetime(2026, 9, 30, tzinfo=timezone.utc),
    )

    assert document["message"] == "line one\nline two"
    assert document["event"]["dataset"] == "audit"


def test_data_stream_documents_include_the_configured_ecs_fields():
    clock = Clock()
    scheduler = EventScheduler(
        [LogEntry("application", 60, "message", False)],
        1,
        monotonic=clock.monotonic,
        now=clock.now,
        random_choice=lambda _words: "unused",
        data_stream={
            "type": "logs",
            "namespace": "default",
            "dataset": "logs-test",
        },
    )

    clock.seconds = 1
    document = scheduler.generate_due_documents()[0]

    assert document["data_stream"] == {
        "type": "logs",
        "namespace": "default",
        "dataset": "logs-test",
    }


class BulkClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def bulk(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def test_bulk_writer_uses_create_operations_for_an_alias_or_data_stream():
    documents = [{"@timestamp": "2026-09-30T12:00:00.000Z", "message": "hello"}]
    client = BulkClient({"errors": False, "items": []})

    write_bulk(client, "logs-test", documents)

    assert client.calls[0]["operations"] == bulk_operations("logs-test", documents)
    assert client.calls[0]["operations"][0] == {"create": {"_index": "logs-test"}}


def test_bulk_writer_raises_when_an_item_fails():
    client = BulkClient({"errors": True, "items": [{"create": {"error": "rejected"}}]})

    with pytest.raises(BulkWriteError, match="rejected"):
        write_bulk(client, "logs-test", [{"message": "hello"}])
