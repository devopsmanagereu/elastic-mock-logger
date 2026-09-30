"""Tests for index and data-stream provisioning."""

from __future__ import annotations

from common import Configuration
from common import KibanaRequestError
from common import LogEntry
from setup import prepare_kibana_data_view
from setup import prepare_destination


class RecordingIlm:
    def __init__(self):
        self.calls = []

    def put_lifecycle(self, **kwargs):
        self.calls.append(kwargs)


class RecordingIndices:
    def __init__(self, exists: bool = False, alias_exists: bool = False, aliases=None):
        self.exists_result = exists
        self.alias_exists = alias_exists
        self.aliases = aliases or {}
        self.template_calls = []
        self.create_calls = []
        self.data_stream_calls = []
        self.exists_alias_calls = []
        self.get_alias_calls = []

    def exists(self, **_kwargs):
        return self.exists_result

    def put_index_template(self, **kwargs):
        self.template_calls.append(kwargs)

    def create(self, **kwargs):
        self.create_calls.append(kwargs)

    def create_data_stream(self, **kwargs):
        self.data_stream_calls.append(kwargs)

    def exists_alias(self, **kwargs):
        self.exists_alias_calls.append(kwargs)
        return self.alias_exists

    def get_alias(self, **kwargs):
        self.get_alias_calls.append(kwargs)
        return self.aliases


class RecordingClient:
    def __init__(self, exists: bool = False, alias_exists: bool = False, aliases=None):
        self.ilm = RecordingIlm()
        self.indices = RecordingIndices(exists, alias_exists, aliases)


def configuration(destination_type: str) -> Configuration:
    return Configuration(
        elastic_hosts=("https://elasticsearch.example.test",),
        es_port=9200,
        elastic_username="elastic",
        elastic_password="password",
        ca_certs=(),
        kibana_host="https://kibana.example.test",
        kibana_port=5601,
        destination_type=destination_type,
        destination_name="logs-test",
        flush_interval=3,
        random_words_count=4,
        log_entries=(LogEntry("application", 60, "message", False),),
        ilm_policy={
            "phases": {"hot": {"actions": {"rollover": {"max_age": "30d"}}}}
        },
    )


def test_index_setup_creates_rollover_policy_template_and_initial_write_index():
    client = RecordingClient()

    prepare_destination(client, configuration("index"))

    assert client.ilm.calls == [{
        "name": "logs-test",
        "policy": {"phases": {"hot": {"actions": {"rollover": {"max_age": "30d"}}}}},
    }]
    assert client.indices.template_calls[0]["index_patterns"] == ["logs-test-*"]
    assert client.indices.create_calls == [{
        "index": "logs-test-000001",
        "aliases": {"logs-test": {"is_write_index": True}},
    }]


def test_index_setup_does_not_create_an_index_when_the_alias_has_a_write_index():
    client = RecordingClient(
        alias_exists=True,
        aliases={
            "logs-test-000003": {
                "aliases": {"logs-test": {"is_write_index": True}}
            }
        },
    )

    prepare_destination(client, configuration("index"))

    assert client.indices.get_alias_calls == [{"name": "logs-test"}]
    assert client.indices.create_calls == []


def test_index_setup_creates_the_next_series_index_when_the_alias_has_no_write_index():
    client = RecordingClient(
        alias_exists=True,
        aliases={
            "logs-test-000001": {"aliases": {"logs-test": {}}},
            "logs-test-000007": {"aliases": {"logs-test": {}}},
        },
    )

    prepare_destination(client, configuration("index"))

    assert client.indices.create_calls == [{
        "index": "logs-test-000008",
        "aliases": {"logs-test": {"is_write_index": True}},
    }]


def test_data_stream_setup_uses_a_matching_data_stream_template():
    client = RecordingClient()

    prepare_destination(client, configuration("data_stream"))

    assert client.ilm.calls == []
    assert client.indices.template_calls[0]["index_patterns"] == ["logs-test"]
    assert client.indices.template_calls[0]["data_stream"] == {}
    assert client.indices.data_stream_calls == [{"name": "logs-test"}]


class RecordingKibana:
    def __init__(self):
        self.calls = []

    def request_json(self, method, path, body):
        self.calls.append((method, path, body))
        if path.endswith("logs-test"):
            raise KibanaRequestError("not found", status_code=404)
        return {"data_view": {"id": "logs-test"}}


def test_kibana_setup_creates_a_named_data_view_that_targets_the_alias():
    client = RecordingKibana()

    prepare_kibana_data_view(client, configuration("index"))

    assert client.calls == [
        (
            "POST",
            "/api/data_views/data_view/logs-test",
            {
                "data_view": {
                    "name": "logs-test",
                    "title": "logs-test",
                    "timeFieldName": "@timestamp",
                },
                "refresh_fields": True,
            },
        ),
        (
            "POST",
            "/api/data_views/data_view",
            {
                "data_view": {
                    "id": "logs-test",
                    "name": "logs-test",
                    "title": "logs-test",
                    "timeFieldName": "@timestamp",
                },
                "refresh_fields": True,
            },
        ),
    ]
