"""Configuration and Elasticsearch client helpers for the mock logger."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import ssl
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.request import urlopen

from dotenv import dotenv_values
from elasticsearch9 import Elasticsearch


class ConfigurationError(ValueError):
    """Raised when the runtime configuration is incomplete or invalid."""


class KibanaRequestError(RuntimeError):
    """Raised when Kibana rejects a request or cannot be reached."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class LogEntry:
    """One independently scheduled kind of generated event."""

    name: str
    events_per_minute: float
    message: str | None
    random_message: bool


@dataclass(frozen=True)
class Configuration:
    """Validated application configuration."""

    elastic_hosts: tuple[str, ...]
    es_port: int
    elastic_username: str
    elastic_password: str
    ca_certs: tuple[str, ...]
    kibana_host: str
    kibana_port: int
    destination_type: str
    destination_name: str
    data_stream_namespace: str | None
    flush_interval: float
    random_words_count: int
    log_entries: tuple[LogEntry, ...]
    ilm_policy: Mapping[str, Any] | None

    @property
    def elasticsearch_urls(self) -> tuple[str, ...]:
        """Return every configured Elasticsearch endpoint."""
        return tuple(f"{host}:{self.es_port}" for host in self.elastic_hosts)

    @property
    def kibana_url(self) -> str:
        """Return the configured Kibana endpoint."""
        return f"{self.kibana_host}:{self.kibana_port}"

    @property
    def data_stream_name(self) -> str | None:
        """Return the namespace-qualified data-stream name when configured."""
        if self.destination_type != "data_stream":
            return None
        if self.data_stream_namespace is None:
            raise ConfigurationError(
                "DATA_STREAM_NAMESPACE is required for a data-stream destination."
            )
        return f"{self.destination_name}-{self.data_stream_namespace}"

    @property
    def write_destination(self) -> str:
        """Return the alias or namespace-qualified data stream used by Bulk API."""
        return self.data_stream_name or self.destination_name

    @property
    def event_data_stream(self) -> Mapping[str, str] | None:
        """Return the ECS data-stream fields required on data-stream events."""
        if self.destination_type != "data_stream":
            return None
        if self.data_stream_namespace is None:
            raise ConfigurationError(
                "DATA_STREAM_NAMESPACE is required for a data-stream destination."
            )
        return {
            "type": "logs",
            "namespace": self.data_stream_namespace,
            "dataset": self.destination_name,
        }


def _default_dotenv_path() -> Path | None:
    """Find the project's local .env file without selecting a hidden fallback."""
    working_directory_file = Path.cwd() / ".env"
    if working_directory_file.is_file():
        return working_directory_file
    return None


def _load_environment(dotenv_path: str | Path | None) -> tuple[dict[str, str], Path | None]:
    """Load dotenv values, giving real environment variables precedence."""
    path = Path(dotenv_path) if dotenv_path is not None else _default_dotenv_path()
    dotenv_values_map: dict[str, str] = {}
    if path is not None:
        if not path.is_file():
            raise ConfigurationError(f"Configuration file does not exist: {path}")
        dotenv_values_map = {
            key: value
            for key, value in dotenv_values(path).items()
            if value is not None
        }

    # Container orchestrators and command-line environments must be able to
    # override an adjacent .env file without rewriting it.
    return dotenv_values_map | dict(os.environ), path


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} must be configured.")
    return value


def _parse_positive_number(values: Mapping[str, str], name: str) -> float:
    raw_value = _required(values, name)
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a number greater than zero.") from error
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero.")
    return value


def _parse_positive_integer(values: Mapping[str, str], name: str) -> int:
    raw_value = _required(values, name)
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a whole number greater than zero.") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero.")
    return value


def _parse_http_host(host: str, host_name: str) -> str:
    """Validate one HTTP(S) endpoint host without its separately configured port."""
    host = host.strip().rstrip("/")
    if not host:
        raise ConfigurationError(f"{host_name} must not contain an empty host.")
    parsed_host = urlsplit(host)
    if parsed_host.scheme not in {"http", "https"} or not parsed_host.netloc:
        raise ConfigurationError(f"{host_name} must be an http:// or https:// URL.")
    if parsed_host.path or parsed_host.query or parsed_host.fragment:
        raise ConfigurationError(
            f"{host_name} may not include a path, query, or fragment."
        )
    try:
        has_port = parsed_host.port is not None
    except ValueError as error:
        raise ConfigurationError(f"{host_name} contains an invalid port.") from error
    if has_port:
        raise ConfigurationError(
            f"{host_name} may not include a port; use its matching port setting instead."
        )
    return host


def _parse_http_endpoint(
    values: Mapping[str, str], host_name: str, port_name: str
) -> tuple[str, int]:
    """Parse one HTTP(S) endpoint that uses a separately configured port."""
    host = _parse_http_host(_required(values, host_name), host_name)

    port = _parse_positive_integer(values, port_name)
    if port > 65535:
        raise ConfigurationError(f"{port_name} must be between 1 and 65535.")
    return host, port


def _parse_elasticsearch_hosts(values: Mapping[str, str]) -> tuple[tuple[str, ...], int]:
    """Parse the comma-separated Elasticsearch host list and its shared port."""
    raw_hosts = _required(values, "ELASTIC_HOST")
    hosts = tuple(
        _parse_http_host(host, "ELASTIC_HOST") for host in raw_hosts.split(",")
    )
    if len(set(hosts)) != len(hosts):
        raise ConfigurationError("ELASTIC_HOST must not contain duplicate hosts.")
    port = _parse_positive_integer(values, "ES_PORT")
    if port > 65535:
        raise ConfigurationError("ES_PORT must be between 1 and 65535.")
    return hosts, port


def _parse_destination_name(values: Mapping[str, str]) -> str:
    name = _required(values, "DESTINATION_NAME")
    forbidden_characters = set('\\/*?"<>|,#:')
    if (
        name in {".", ".."}
        or name[0] in "-_+"
        or name.lower() != name
        or any(character in forbidden_characters or character.isspace() for character in name)
    ):
        raise ConfigurationError(
            "DESTINATION_NAME must be a lowercase Elasticsearch-compatible name."
        )
    if len(name.encode("utf-8")) > 255:
        raise ConfigurationError("DESTINATION_NAME may contain at most 255 bytes.")
    return name


def _parse_data_stream_namespace(
    values: Mapping[str, str], destination_type: str
) -> str | None:
    """Read the data-stream namespace, which is required only for data streams."""
    namespace = values.get("DATA_STREAM_NAMESPACE", "").strip()
    if destination_type == "data_stream" and not namespace:
        raise ConfigurationError(
            "DATA_STREAM_NAMESPACE must be configured for DESTINATION_TYPE=data_stream."
        )
    return namespace or None


def _is_true(value: object) -> bool:
    """Accept JSON true and the historic string value \"true\"."""
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _parse_log_entries(values: Mapping[str, str]) -> tuple[LogEntry, ...]:
    raw_entries = _required(values, "LOG_ENTRIES")
    try:
        entries = json.loads(raw_entries)
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"LOG_ENTRIES must be valid JSON: {error.msg}.") from error

    if not isinstance(entries, list) or not entries:
        raise ConfigurationError("LOG_ENTRIES must be a non-empty JSON array.")

    parsed_entries: list[LogEntry] = []
    names: set[str] = set()
    allowed_fields = {"name", "events_per_minute", "message", "random_message"}
    for position, entry in enumerate(entries, start=1):
        prefix = f"LOG_ENTRIES item {position}"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{prefix} must be a JSON object.")
        unknown_fields = set(entry) - allowed_fields
        if unknown_fields:
            fields = ", ".join(sorted(unknown_fields))
            raise ConfigurationError(f"{prefix} contains unknown field(s): {fields}.")

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError(f"{prefix}.name must be a non-empty string.")
        if name in names:
            raise ConfigurationError(f"{prefix}.name {name!r} is duplicated.")
        names.add(name)

        event_rate = entry.get("events_per_minute")
        if isinstance(event_rate, bool) or not isinstance(event_rate, (int, float)):
            raise ConfigurationError(f"{prefix}.events_per_minute must be a number.")
        if not math.isfinite(event_rate) or event_rate <= 0:
            raise ConfigurationError(f"{prefix}.events_per_minute must be greater than zero.")

        message = entry.get("message")
        random_message = _is_true(entry.get("random_message"))
        if message is not None and not isinstance(message, str):
            raise ConfigurationError(f"{prefix}.message must be a string.")
        if (message is None) == (not random_message):
            raise ConfigurationError(
                f"{prefix} must define exactly one of message or random_message: true."
            )

        parsed_entries.append(
            LogEntry(
                name=name,
                events_per_minute=float(event_rate),
                message=message,
                random_message=random_message,
            )
        )
    return tuple(parsed_entries)


def _parse_ca_certs(values: Mapping[str, str], dotenv_path: Path | None) -> tuple[str, ...]:
    raw_certificates = values.get("CA_CERTS", "").strip()
    if not raw_certificates:
        return ()

    base_directory = dotenv_path.parent if dotenv_path is not None else Path.cwd()
    paths = []
    for certificate in raw_certificates.split(","):
        certificate_path = Path(certificate.strip())
        if not certificate_path.is_absolute():
            certificate_path = base_directory / certificate_path
        paths.append(str(certificate_path))
    return tuple(paths)


def _parse_ilm_policy(values: Mapping[str, str]) -> Mapping[str, Any] | None:
    """Parse the required lifecycle policy from its JSON configuration."""
    raw_policy = values.get("ILM_POLICY", "").strip()
    if not raw_policy:
        raise ConfigurationError(
            "ILM_POLICY must be configured for every destination type."
        )
    try:
        policy = json.loads(raw_policy)
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"ILM_POLICY must be valid JSON: {error.msg}.") from error
    if isinstance(policy, dict) and "policy" in policy:
        policy = policy["policy"]
    if not isinstance(policy, dict) or not isinstance(policy.get("phases"), dict):
        raise ConfigurationError("ILM_POLICY must be a JSON object containing phases.")
    return policy


def load_configuration(dotenv_path: str | Path | None = None) -> Configuration:
    """Read and validate application settings from dotenv and environment values."""
    values, resolved_dotenv_path = _load_environment(dotenv_path)
    elastic_hosts, es_port = _parse_elasticsearch_hosts(values)
    kibana_host, kibana_port = _parse_http_endpoint(values, "KIBANA_HOST", "KIBANA_PORT")
    destination_type = _required(values, "DESTINATION_TYPE")
    if destination_type not in {"index", "data_stream"}:
        raise ConfigurationError("DESTINATION_TYPE must be index or data_stream.")

    return Configuration(
        elastic_hosts=elastic_hosts,
        es_port=es_port,
        elastic_username=_required(values, "ELASTIC_USERNAME"),
        elastic_password=_required(values, "ELASTIC_PASSWORD"),
        ca_certs=_parse_ca_certs(values, resolved_dotenv_path),
        kibana_host=kibana_host,
        kibana_port=kibana_port,
        destination_type=destination_type,
        destination_name=_parse_destination_name(values),
        data_stream_namespace=_parse_data_stream_namespace(values, destination_type),
        flush_interval=_parse_positive_number(values, "FLUSH_INTERVAL"),
        random_words_count=_parse_positive_integer(values, "RANDOM_WORDS_COUNT"),
        log_entries=_parse_log_entries(values),
        ilm_policy=_parse_ilm_policy(values),
    )


def create_elasticsearch_client(configuration: Configuration) -> Elasticsearch:
    """Create an Elasticsearch client with the selected authentication and TLS settings."""
    options: dict[str, Any] = {
        "hosts": list(configuration.elasticsearch_urls),
        "basic_auth": (configuration.elastic_username, configuration.elastic_password),
        # elastic-transport marks failed nodes as dead and selects another node
        # for every retry. This covers each configured host once per request.
        "max_retries": len(configuration.elastic_hosts) - 1,
        "retry_on_timeout": True,
    }
    if configuration.ca_certs:
        options["ssl_context"] = create_ssl_context(configuration.ca_certs)
    return Elasticsearch(**options)


def create_ssl_context(ca_certs: tuple[str, ...]) -> ssl.SSLContext:
    """Create a TLS context that trusts system roots and every configured CA."""
    ssl_context = ssl.create_default_context()
    for certificate_path in ca_certs:
        ssl_context.load_verify_locations(cafile=certificate_path)
    return ssl_context


class KibanaClient:
    """Make authenticated JSON requests to Kibana's public REST API."""

    def __init__(self, configuration: Configuration) -> None:
        self._url = configuration.kibana_url
        encoded_credentials = b64encode(
            f"{configuration.elastic_username}:{configuration.elastic_password}".encode("utf-8")
        ).decode("ascii")
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {encoded_credentials}",
            "kbn-xsrf": "elastic-mock-logger",
        }
        self._ssl_context = (
            create_ssl_context(configuration.ca_certs)
            if configuration.kibana_host.startswith("https://")
            else None
        )

    def request_json(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a Kibana API request and return its JSON response body."""
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = dict(self._headers)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._url}{path}", data=payload, headers=headers, method=method
        )
        try:
            with urlopen(request, context=self._ssl_context, timeout=30) as response:
                response_body = response.read()
        except HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace")
            raise KibanaRequestError(
                f"Kibana request {method} {path} failed with HTTP {error.code}: "
                f"{response_body}",
                status_code=error.code,
            ) from error
        except URLError as error:
            raise KibanaRequestError(
                f"Kibana request {method} {path} could not be completed: {error.reason}"
            ) from error

        if not response_body:
            return {}
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as error:
            raise KibanaRequestError(
                f"Kibana request {method} {path} returned invalid JSON."
            ) from error


def create_kibana_client(configuration: Configuration) -> KibanaClient:
    """Create a Kibana client that shares the Elasticsearch credentials."""
    return KibanaClient(configuration)
