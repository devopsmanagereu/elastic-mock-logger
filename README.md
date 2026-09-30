# Elastic mock logger

This generator continuously creates test logs and writes them to Elasticsearch
through the Bulk API. Before its first write, it prepares the target index or
data stream.

## Configuration

Copy [`.env.example`](.env.example) to `.env`, then adjust the connection and
`LOG_ENTRIES` contents:

```dotenv
# Every host shares ES_PORT. The client retries another host after a failure.
ELASTIC_HOST=https://es01.example.test,https://es02.example.test
ES_PORT=9200
ELASTIC_USERNAME=elastic
ELASTIC_PASSWORD=change-me
# CA_CERTS=certificates/ca.crt
KIBANA_HOST=https://kibana.example.test
KIBANA_PORT=5601

DESTINATION_TYPE=index
DESTINATION_NAME=logs-random
FLUSH_INTERVAL=3
RANDOM_WORDS_COUNT=8

ILM_POLICY={"policy":{"phases":{"hot":{"min_age":"0ms","actions":{"rollover":{"max_age":"3m","max_primary_shard_size":"1mb"}}},"delete":{"min_age":"10m","actions":{"delete":{"delete_searchable_snapshot":true}}}}}}
LOG_ENTRIES=[{"name":"single_line_logs","events_per_minute":180,"random_message":"true"},{"name":"multi_line_logs","events_per_minute":10,"message":"Firstrow\n\tsecondrow\n\tthirdrow"}]

```

`LOG_ENTRIES` is a JSON array. Every entry must define:

- `name`: the message type; stored in `event.dataset`.
- `events_per_minute`: a positive number; fractional values are supported.
- Exactly one of `message` or `random_message: true`.

For a random message, the application generates exactly `RANDOM_WORDS_COUNT`
words. Encode multi-line messages as `\n` in JSON. Every event contains an
`@timestamp` in UTC, generated at the time that event is created.

`FLUSH_INTERVAL` is the positive number of seconds for which documents are
collected before they are sent in a single `_bulk` request.

`CA_CERTS` is an optional comma-separated list of PEM files. Relative paths are
resolved from the selected `.env` file.

`ELASTIC_HOST` accepts one host or a comma-separated list of HTTP(S) hosts. All
hosts use `ES_PORT`. The native `elasticsearch9` transport maintains the host
pool, marks a failed host as unavailable, and retries the request on another
configured host, including after a timeout.

`KIBANA_HOST` and `KIBANA_PORT` select the Kibana instance where setup creates
the data view. The Elasticsearch username and password are also used for
Kibana's Basic authentication.

## Elasticsearch destination

`DESTINATION_TYPE=index` creates (or updates) an ILM policy and composable
index template named after `DESTINATION_NAME`. Its first index is
`<DESTINATION_NAME>-000001`, with `DESTINATION_NAME` as its write-index alias.
`ILM_POLICY` is required for an index destination and contains the exact ILM
policy sent to Elasticsearch. It can be either the policy object itself or a
full Elasticsearch ILM request body containing a top-level `policy` object.

`DESTINATION_TYPE=data_stream` creates a matching composable index template
and the data stream itself. In both modes, events use the `create` bulk action,
which is valid for data streams and aliases that have a write index.

Setup can be safely repeated: the policy and template are updated from the
configuration, while an existing initial index or data stream is not recreated.
It also creates or updates a Kibana data view named `DESTINATION_NAME`, whose
data view title is `DESTINATION_NAME`: the index alias for an index destination,
or the data stream name for a data-stream destination.

For an index destination, setup also checks every index currently assigned to
the `DESTINATION_NAME` alias. If one is marked as `is_write_index`, it creates
no new index. If the alias has no write index, setup creates the next
zero-padded sequence index after the highest alias index (for example,
`logs-000007` becomes `logs-000008`) and marks it as the write index.

## Running

```bash
uv sync
cp .env.example .env
uv run python src/elastic_mock_logger.py
```

The main program invokes setup from [`src/setup.py`](src/setup.py) before it
starts the generator. Run setup only, for example from an init container:

```bash
uv run python src/setup.py --env-file .env
# or
uv run python src/elastic_mock_logger.py --env-file .env --setup-only
```

Select another configuration file with `--env-file`. Without that option, the
program looks for `.env` only in the current directory. Use an explicit path,
for example `--env-file run_src/.env`, when the file lives elsewhere.
Environment variables always take precedence, so Docker and Kubernetes
deployments do not need a configuration file inside the container.

## Tests

```bash
uv run pytest
```

The tests use a fake Elasticsearch client and do not need a running cluster.
