"""Generate configured mock log events and write them through Elasticsearch Bulk API."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import random
import time
from typing import Any

from common import Configuration
from common import ConfigurationError
from common import LogEntry
from common import create_elasticsearch_client
from common import create_kibana_client
from common import load_configuration
from setup import prepare_setup


LOGGER = logging.getLogger(__name__)

WORDS = (
    "amber", "api", "batch", "bridge", "cache", "cluster", "code", "event",
    "field", "gateway", "health", "index", "ingest", "latency", "log", "metric",
    "node", "pipeline", "query", "queue", "request", "response", "retry", "route",
    "service", "shard", "signal", "stream", "template", "trace", "worker", "zone",
)


class BulkWriteError(RuntimeError):
    """Raised when Elasticsearch reports one or more failed bulk items."""


@dataclass
class _ScheduledEntry:
    entry: LogEntry
    emitted_events: int


def utc_timestamp(now: datetime | None = None) -> str:
    """Return a UTC timestamp with milliseconds in Elasticsearch's ISO-8601 form."""
    instant = now or datetime.now(timezone.utc)
    return instant.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def random_sentence(word_count: int, random_choice: Callable[[Sequence[str]], str]) -> str:
    """Create one deterministic-size, human-readable random message."""
    return " ".join(random_choice(WORDS) for _ in range(word_count))


def create_document(
    entry: LogEntry,
    random_words_count: int,
    random_choice: Callable[[Sequence[str]], str],
    now: Callable[[], datetime],
) -> dict[str, Any]:
    """Create an ECS-compatible document at the moment it is generated."""
    message = (
        random_sentence(random_words_count, random_choice)
        if entry.random_message
        else entry.message
    )
    return {
        "@timestamp": utc_timestamp(now()),
        "message": message,
        "event": {"dataset": entry.name},
    }


class EventScheduler:
    """Schedule each log-entry rate independently without losing fractional rates."""

    def __init__(
        self,
        entries: Iterable[LogEntry],
        random_words_count: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        random_choice: Callable[[Sequence[str]], str] = random.choice,
    ) -> None:
        self._monotonic = monotonic
        self._now = now
        self._random_choice = random_choice
        self._random_words_count = random_words_count
        self._started_at = monotonic()
        self._scheduled_entries = [
            _ScheduledEntry(
                entry=entry,
                emitted_events=0,
            )
            for entry in entries
        ]

    def generate_due_documents(self) -> list[dict[str, Any]]:
        """Generate every event due at the current real time."""
        elapsed_seconds = max(0.0, self._monotonic() - self._started_at)
        documents: list[dict[str, Any]] = []
        for scheduled_entry in self._scheduled_entries:
            expected_events = int(
                elapsed_seconds * scheduled_entry.entry.events_per_minute / 60
            )
            while scheduled_entry.emitted_events < expected_events:
                documents.append(
                    create_document(
                        scheduled_entry.entry,
                        self._random_words_count,
                        self._random_choice,
                        self._now,
                    )
                )
                scheduled_entry.emitted_events += 1
        return documents

    def seconds_until_next_event(self) -> float:
        """Return the delay to the earliest scheduled event."""
        elapsed_seconds = max(0.0, self._monotonic() - self._started_at)
        return max(
            0.0,
            min(
                (entry.emitted_events + 1) * 60 / entry.entry.events_per_minute
                - elapsed_seconds
                for entry in self._scheduled_entries
            ),
        )


def bulk_operations(destination: str, documents: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Create NDJSON-equivalent bulk operations for either destination type."""
    operations: list[dict[str, Any]] = []
    for document in documents:
        # `create` is valid for a data stream and also routes through an alias
        # marked as the write index.
        operations.extend(({"create": {"_index": destination}}, dict(document)))
    return operations


def _response_body(response: object) -> Mapping[str, Any]:
    body = getattr(response, "body", response)
    if not isinstance(body, Mapping):
        raise BulkWriteError("Elasticsearch bulk API returned an unexpected response.")
    return body


def write_bulk(client: object, destination: str, documents: Sequence[Mapping[str, Any]]) -> None:
    """Write a batch and report individual Elasticsearch failures clearly."""
    if not documents:
        return
    response = _response_body(
        client.bulk(operations=bulk_operations(destination, documents), refresh=False)
    )
    if not response.get("errors"):
        return

    failures = []
    for item in response.get("items", []):
        action = item.get("create", {})
        if "error" in action:
            failures.append(str(action["error"]))
    details = "; ".join(failures[:3]) or "Elasticsearch did not provide item details"
    raise BulkWriteError(f"Bulk write to {destination!r} failed: {details}")


def run_generator(
    client: object,
    configuration: Configuration,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    random_choice: Callable[[Sequence[str]], str] = random.choice,
) -> None:
    """Generate events continuously and flush accumulated documents on schedule."""
    scheduler = EventScheduler(
        configuration.log_entries,
        configuration.random_words_count,
        monotonic=monotonic,
        now=now,
        random_choice=random_choice,
    )
    buffered_documents: list[dict[str, Any]] = []
    next_flush_at = monotonic() + configuration.flush_interval
    entry_rates = ", ".join(
        f"{entry.name}={entry.events_per_minute:g}/min"
        for entry in configuration.log_entries
    )
    LOGGER.info(
        "Starting mock-log generation for %s with rates: %s",
        configuration.destination_name,
        entry_rates,
    )

    try:
        while True:
            buffered_documents.extend(scheduler.generate_due_documents())
            current_time = monotonic()
            if current_time >= next_flush_at:
                if buffered_documents:
                    write_bulk(client, configuration.destination_name, buffered_documents)
                    LOGGER.info(
                        "Wrote %d generated events to %s",
                        len(buffered_documents),
                        configuration.destination_name,
                    )
                    buffered_documents.clear()
                while next_flush_at <= current_time:
                    next_flush_at += configuration.flush_interval

            delay = min(
                scheduler.seconds_until_next_event(),
                max(0.0, next_flush_at - monotonic()),
            )
            # A small lower bound prevents a tight loop from floating-point rounding.
            sleep(max(0.001, delay))
    except KeyboardInterrupt:
        if buffered_documents:
            write_bulk(client, configuration.destination_name, buffered_documents)
            LOGGER.info(
                "Wrote %d buffered events before stopping", len(buffered_documents)
            )
        LOGGER.info("Mock logger stopped")


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="Path to a dotenv file.")
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="Create the configured Elasticsearch destination and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Prepare Elasticsearch first, then run the configured event generator."""
    arguments = _parse_arguments(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        configuration = load_configuration(arguments.env_file)
    except ConfigurationError as error:
        LOGGER.error("Invalid configuration: %s", error)
        return 2
    client = create_elasticsearch_client(configuration)
    kibana_client = create_kibana_client(configuration)
    prepare_setup(client, kibana_client, configuration)
    if not arguments.setup_only:
        run_generator(client, configuration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
