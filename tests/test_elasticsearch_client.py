"""Tests for Elasticsearch host-pool configuration."""

from __future__ import annotations

from common import Configuration
from common import LogEntry
from common import create_elasticsearch_client


def configuration(hosts: tuple[str, ...]) -> Configuration:
    return Configuration(
        elastic_hosts=hosts,
        es_port=9200,
        elastic_username="elastic",
        elastic_password="password",
        ca_certs=(),
        kibana_host="https://kibana.example.test",
        kibana_port=5601,
        destination_type="data_stream",
        destination_name="logs-test",
        data_stream_namespace="default",
        flush_interval=3,
        random_words_count=4,
        log_entries=(LogEntry("application", 60, "message", False),),
        ilm_policy=None,
    )


def test_client_uses_the_native_host_pool_and_retries_every_configured_host(monkeypatch):
    captured_options = {}

    class FakeElasticsearch:
        def __init__(self, **options):
            captured_options.update(options)

    monkeypatch.setattr("common.Elasticsearch", FakeElasticsearch)

    create_elasticsearch_client(
        configuration(("https://es01.example.test", "https://es02.example.test"))
    )

    assert captured_options["hosts"] == [
        "https://es01.example.test:9200",
        "https://es02.example.test:9200",
    ]
    assert captured_options["max_retries"] == 1
    assert captured_options["retry_on_timeout"] is True
