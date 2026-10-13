"""Tests for dotenv parsing and LOG_ENTRIES validation."""

from __future__ import annotations

import pytest

from common import ConfigurationError
from common import load_configuration


def write_environment(path, log_entries: str) -> None:
    path.write_text(
        "\n".join(
            (
                "ELASTIC_HOST=https://elasticsearch.example.test",
                "ES_PORT=9200",
                "ELASTIC_USERNAME=elastic",
                "ELASTIC_PASSWORD=password",
                "KIBANA_HOST=https://kibana.example.test",
                "KIBANA_PORT=5601",
                "DESTINATION_TYPE=index",
                "DESTINATION_NAME=logs-test",
                "FLUSH_INTERVAL=3",
                "RANDOM_WORDS_COUNT=4",
                "ILM_POLICY='{\"phases\":{\"hot\":{\"actions\":{\"rollover\":{\"max_age\":\"30d\"}}}}}'",
                f"LOG_ENTRIES='{log_entries}'",
            )
        )
    )


def test_load_configuration_parses_fixed_and_random_messages(tmp_path, monkeypatch):
    for name in (
        "ELASTIC_HOST", "ES_PORT", "ELASTIC_USERNAME", "ELASTIC_PASSWORD",
        "KIBANA_HOST", "KIBANA_PORT", "DESTINATION_TYPE", "DESTINATION_NAME", "FLUSH_INTERVAL",
        "RANDOM_WORDS_COUNT", "LOG_ENTRIES",
    ):
        monkeypatch.delenv(name, raising=False)
    environment_file = tmp_path / ".env"
    write_environment(
        environment_file,
        '[{"name":"application","events_per_minute":60,"random_message":true},'
        '{"name":"audit","events_per_minute":2.5,"message":"signed in"}]',
    )

    configuration = load_configuration(environment_file)

    assert configuration.elasticsearch_urls == ("https://elasticsearch.example.test:9200",)
    assert configuration.kibana_url == "https://kibana.example.test:5601"
    assert configuration.ilm_policy == {
        "phases": {"hot": {"actions": {"rollover": {"max_age": "30d"}}}}
    }
    assert configuration.log_entries[0].random_message is True
    assert configuration.log_entries[0].message is None
    assert configuration.log_entries[1].events_per_minute == 2.5
    assert configuration.log_entries[1].message == "signed in"
    assert configuration.data_stream_namespace is None


def test_load_configuration_parses_comma_separated_elasticsearch_hosts(
    tmp_path, monkeypatch
):
    for name in ("ELASTIC_HOST", "ES_PORT"):
        monkeypatch.delenv(name, raising=False)
    environment_file = tmp_path / ".env"
    write_environment(
        environment_file, '[{"name":"application","events_per_minute":1,"message":"x"}]'
    )
    environment_file.write_text(
        environment_file.read_text().replace(
            "ELASTIC_HOST=https://elasticsearch.example.test",
            "ELASTIC_HOST=https://es01.example.test, https://es02.example.test",
        )
    )

    configuration = load_configuration(environment_file)

    assert configuration.elasticsearch_urls == (
        "https://es01.example.test:9200",
        "https://es02.example.test:9200",
    )


@pytest.mark.parametrize(
    "entries, expected_message",
    (
        ('[{"name":"missing","events_per_minute":1}]', "exactly one"),
        ('[{"name":"both","events_per_minute":1,"message":"x","random_message":true}]', "exactly one"),
        ('[{"name":"zero","events_per_minute":0,"message":"x"}]', "greater than zero"),
        ('[{"name":"not-a-number","events_per_minute":NaN,"message":"x"}]', "greater than zero"),
    ),
)
def test_load_configuration_rejects_invalid_log_entries(tmp_path, entries, expected_message):
    environment_file = tmp_path / ".env"
    write_environment(environment_file, entries)

    with pytest.raises(ConfigurationError, match=expected_message):
        load_configuration(environment_file)


def test_index_destination_requires_an_ilm_policy(tmp_path):
    environment_file = tmp_path / ".env"
    write_environment(environment_file, '[{"name":"application","events_per_minute":1,"message":"x"}]')
    environment_file.write_text(
        environment_file.read_text().replace(
            "ILM_POLICY='{\"phases\":{\"hot\":{\"actions\":{\"rollover\":{\"max_age\":\"30d\"}}}}}'\n",
            "",
        )
    )

    with pytest.raises(ConfigurationError, match="ILM_POLICY"):
        load_configuration(environment_file)


def test_data_stream_destination_requires_a_namespace_and_uses_it_in_its_name(tmp_path):
    environment_file = tmp_path / ".env"
    write_environment(
        environment_file, '[{"name":"application","events_per_minute":1,"message":"x"}]'
    )
    environment_file.write_text(
        environment_file.read_text()
        .replace("DESTINATION_TYPE=index", "DESTINATION_TYPE=data_stream")
        .replace("DESTINATION_NAME=logs-test", "DESTINATION_NAME=logs-test\nDATA_STREAM_NAMESPACE=default")
    )

    configuration = load_configuration(environment_file)

    assert configuration.data_stream_name == "logs-test-default"
    assert configuration.write_destination == "logs-test-default"
    assert configuration.event_data_stream == {
        "type": "logs",
        "namespace": "default",
        "dataset": "logs-test",
    }


def test_data_stream_destination_rejects_a_missing_namespace(tmp_path):
    environment_file = tmp_path / ".env"
    write_environment(
        environment_file, '[{"name":"application","events_per_minute":1,"message":"x"}]'
    )
    environment_file.write_text(
        environment_file.read_text().replace("DESTINATION_TYPE=index", "DESTINATION_TYPE=data_stream")
    )

    with pytest.raises(ConfigurationError, match="DATA_STREAM_NAMESPACE"):
        load_configuration(environment_file)


def test_data_stream_destination_requires_an_ilm_policy(tmp_path):
    environment_file = tmp_path / ".env"
    write_environment(
        environment_file, '[{"name":"application","events_per_minute":1,"message":"x"}]'
    )
    environment_file.write_text(
        environment_file.read_text()
        .replace("DESTINATION_TYPE=index", "DESTINATION_TYPE=data_stream")
        .replace("DESTINATION_NAME=logs-test", "DESTINATION_NAME=logs-test\nDATA_STREAM_NAMESPACE=default")
        .replace(
            "ILM_POLICY='{\"phases\":{\"hot\":{\"actions\":{\"rollover\":{\"max_age\":\"30d\"}}}}}'\n",
            "",
        )
    )

    with pytest.raises(ConfigurationError, match="ILM_POLICY"):
        load_configuration(environment_file)
