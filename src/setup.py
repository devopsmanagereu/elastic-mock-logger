"""Prepare the configured Elasticsearch destination for mock log events."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import logging
import re
from typing import Any

from common import Configuration
from common import ConfigurationError
from common import create_elasticsearch_client
from common import create_kibana_client
from common import KibanaRequestError
from common import load_configuration
from urllib.parse import quote


LOGGER = logging.getLogger(__name__)

EVENT_MAPPINGS = {
    "properties": {
        "@timestamp": {"type": "date"},
        "message": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "event": {"properties": {"dataset": {"type": "keyword"}}},
    }
}


def _response_exists(response: object) -> bool:
    """Normalize Elasticsearch's boolean and test-double existence responses."""
    body = getattr(response, "body", response)
    if isinstance(body, Mapping):
        return bool(body.get("exists", True))
    return bool(body)


def ilm_policy(configuration: Configuration) -> dict[str, Any]:
    """Return the configured policy used to manage the destination."""
    if configuration.ilm_policy is None:
        raise ConfigurationError("ILM_POLICY is required for every destination type.")
    return dict(configuration.ilm_policy)


def _response_body(response: object) -> Mapping[str, Any]:
    """Return the mapping body from an Elasticsearch API response."""
    body = getattr(response, "body", response)
    if not isinstance(body, Mapping):
        raise ConfigurationError("Elasticsearch returned an unexpected alias response.")
    return body


def _alias_has_write_index(alias_indices: Mapping[str, Any], alias: str) -> bool:
    """Return whether any index in the alias explicitly accepts writes."""
    return any(
        isinstance(index_definition, Mapping)
        and isinstance(index_definition.get("aliases"), Mapping)
        and isinstance(index_definition["aliases"].get(alias), Mapping)
        and index_definition["aliases"][alias].get("is_write_index") is True
        for index_definition in alias_indices.values()
    )


def _next_backing_index(alias_indices: Mapping[str, Any], alias: str) -> str:
    """Return the next zero-padded index name after the alias's highest sequence."""
    pattern = re.compile(rf"^{re.escape(alias)}-(\d+)$")
    series_numbers = [
        int(match.group(1))
        for index_name in alias_indices
        if (match := pattern.fullmatch(index_name))
    ]
    next_number = max(series_numbers, default=0) + 1
    return f"{alias}-{next_number:06d}"


def prepare_index_destination(client: object, configuration: Configuration) -> None:
    """Create the policy and template, then ensure the alias has a write index."""
    destination = configuration.destination_name
    client.ilm.put_lifecycle(name=destination, policy=ilm_policy(configuration))
    client.indices.put_index_template(
        name=destination,
        index_patterns=[f"{destination}-*"],
        template={
            "settings": {
                "index.lifecycle.name": destination,
                "index.lifecycle.rollover_alias": destination,
            },
            "mappings": EVENT_MAPPINGS,
        },
    )

    alias_indices: Mapping[str, Any] = {}
    if _response_exists(client.indices.exists_alias(name=destination)):
        alias_indices = _response_body(client.indices.get_alias(name=destination))
        if _alias_has_write_index(alias_indices, destination):
            LOGGER.info("Alias %s already has a write index", destination)
            return

    backing_index = _next_backing_index(alias_indices, destination)
    client.indices.create(
        index=backing_index,
        aliases={destination: {"is_write_index": True}},
    )
    LOGGER.info("Created write index %s for alias %s", backing_index, destination)


def prepare_data_stream_destination(client: object, configuration: Configuration) -> None:
    """Create an ILM-managed data stream and update it safely on repeated setup."""
    destination = configuration.destination_name
    data_stream_name = configuration.write_destination
    client.ilm.put_lifecycle(name=destination, policy=ilm_policy(configuration))
    client.indices.put_index_template(
        name=destination,
        index_patterns=[f"{destination}-*"],
        priority=501,
        data_stream={},
        template={
            "settings": {"index.lifecycle.name": destination},
            "mappings": EVENT_MAPPINGS,
        },
    )
    if _response_exists(client.indices.exists(index=data_stream_name)):
        LOGGER.info("Data stream %s already exists", data_stream_name)
    else:
        client.indices.create_data_stream(name=data_stream_name)
        LOGGER.info("Created data stream destination %s", data_stream_name)

    client.indices.put_data_stream_settings(
        name=data_stream_name,
        settings={"index.lifecycle.name": destination},
    )
    LOGGER.info("Applied ILM policy %s to data stream %s", destination, data_stream_name)


def prepare_destination(client: object, configuration: Configuration) -> None:
    """Set up the selected destination type. This operation is safe to repeat."""
    if configuration.destination_type == "index":
        prepare_index_destination(client, configuration)
    else:
        prepare_data_stream_destination(client, configuration)


def data_view_definition(
    configuration: Configuration, *, include_id: bool = False
) -> dict[str, Any]:
    """Build the Kibana data view that resolves the destination alias or stream."""
    destination = configuration.destination_name
    data_view = {
        "name": destination,
        "title": configuration.write_destination,
        "timeFieldName": "@timestamp",
    }
    if include_id:
        data_view["id"] = destination
    return {
        "data_view": data_view,
        "refresh_fields": True,
    }


def prepare_kibana_data_view(client: object, configuration: Configuration) -> None:
    """Create or update an idempotent Kibana data view for the destination."""
    destination = configuration.destination_name
    data_view_path = f"/api/data_views/data_view/{quote(destination, safe='')}"
    try:
        client.request_json("POST", data_view_path, data_view_definition(configuration))
        LOGGER.info("Updated Kibana data view %s", destination)
    except KibanaRequestError as error:
        if error.status_code != 404:
            raise
        client.request_json(
            "POST",
            "/api/data_views/data_view",
            data_view_definition(configuration, include_id=True),
        )
        LOGGER.info("Created Kibana data view %s", destination)


def prepare_setup(
    elasticsearch_client: object, kibana_client: object, configuration: Configuration
) -> None:
    """Prepare Elasticsearch first, then create the matching Kibana data view."""
    prepare_destination(elasticsearch_client, configuration)
    prepare_kibana_data_view(kibana_client, configuration)


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="Path to a dotenv file.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run only destination setup, primarily for an explicit deployment step."""
    arguments = _parse_arguments(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        configuration = load_configuration(arguments.env_file)
    except ConfigurationError as error:
        LOGGER.error("Invalid configuration: %s", error)
        return 2
    elasticsearch_client = create_elasticsearch_client(configuration)
    kibana_client = create_kibana_client(configuration)
    prepare_setup(elasticsearch_client, kibana_client, configuration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
