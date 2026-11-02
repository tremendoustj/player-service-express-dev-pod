# Express integration API: authenticated reads and analytics

**Status: all five opt-in read operations (search/filter, player details,
population statistics, numeric rankings, and comparisons) are implemented and
locally tested, with a validated OpenAPI 3.1 contract and authenticated operational
metrics.** Production packaging/log-handler configuration, real loopback HTTP
smoke tests, and authenticated Express/Nexla validation remain pending.

The [architecture assessment](express-integration-architecture.md) records the
verified platform mechanisms, integration decision, remaining acceptance gates,
and external blockers. These endpoints are not a published MCP server or proof of
a working Express chat integration.

## OpenAPI contract

[`express-openapi.json`](express-openapi.json) is the versioned **OpenAPI 3.1.0**
document for API contract version **1.0.0**. It contains exactly the five
implemented GET operations, their stable operation IDs, bearer security,
parameter bounds/defaults/enums, full nullable response schemas, error envelopes,
and correlation/cache headers. It excludes legacy routes, Ollama, mutations,
arbitrary SQL, and MCP endpoints.
The implemented private metrics route is deliberately excluded: it is an
operator scrape surface, not a player tool.

This is a repository artifact, **not an additional HTTP route**, deployed
service, or verified Express/Nexla import mechanism. Its relative server URL `/`
does not identify a remotely reachable deployment. Configure an approved HTTPS
origin when reviewing or using it in a client; keep local HTTP on loopback.
Use it to review generated connector/tool contracts, not to assume that Studio
will import it or use its operation IDs automatically.

Important binding details:

- `player_ids` is an OpenAPI array with `style: form` and `explode: false`:
  serialize it into **one comma-separated value**, not repeated query keys.
- Output player schemas preserve all 24 source fields. Numeric fields are
  nullable numbers, not necessarily integers; career dates are source strings,
  not asserted date formats. Stored IDs in search/ranking records are not
  constrained by the stricter detail/comparison **input** ID schema.
- Incoming `X-Request-ID` is advisory: invalid values are replaced, not rejected.
  Returned IDs always satisfy the bounded response schema.
- OpenAPI parameter schemas alone do not express every wire rule: unknown or
  duplicate parameters, canonical integer spelling, printable/nonblank Unicode,
  and the relationship between the two year bounds still require the documented
  server validation. The tests exercise those rules through Flask.

Standards validation and response-schema checks run locally without tenant
credentials or external schema references. Test-only validators live in
[`requirements-test.txt`](../player-service-app/requirements-test.txt); the
validators are not runtime dependencies.

## Configuration

The application reads these environment variables when creating the Flask app.
The existing `app` export remains available; `create_app()` creates another app
with its own captured integration settings. Invalid enabled configuration raises
a safe `ValueError` **before database initialization**.

| Variable | Implemented behavior |
| --- | --- |
| `PLAYER_INTEGRATION_ENABLED` | Exactly `true` or `false`, case-sensitive; defaults to `false`. Other values, including an empty string, fail startup. |
| `PLAYER_INTEGRATION_TOKEN` | Required when enabled. Exactly 43-256 ASCII letters, digits, hyphens, or underscores. No default. Generate from at least 32 cryptographically random bytes; format validation cannot establish entropy. |
| `PLAYER_DB_BUSY_TIMEOUT_SECONDS` | Integration-only finite number from 0 through 60; default 1. Zero disables waiting on a lock. |
| `PLAYER_QUERY_TIMEOUT_SECONDS` | Integration-only finite number from 0.001 through 60; default 2. A deadline cannot be disabled while the integration is enabled. |

When disabled, no integration blueprint or hooks are installed, and unused token
and timeout settings are ignored. Integration URLs return the application's
normal 404 response. Legacy `/v1/*` behavior and timeout defaults remain unchanged.
No integration metrics registry is created while disabled. When enabled,
`PROMETHEUS_MULTIPROC_DIR` and its legacy lowercase spelling
`prometheus_multiproc_dir` must both be **unset**: even an empty value fails
startup before database initialization. Multi-process collection is unsupported;
see [operational metrics](#operational-metrics) for the required worker model.

[`player-service-app/.env.example`](../player-service-app/.env.example) contains
no credential. It is a reference, **not an automatically loaded configuration
file**. Provision environment variables through the process environment or an
approved secret manager; do not put a token in source, shell history, chat, logs,
tool arguments, or URL parameters. Secret-file injection and `PLAYER_DB_PATH` are
not implemented yet. The application-relative database path and existing startup
CSV initialization are unchanged.

Each running app captures one token at startup. Rotation requires provisioning a
new random token, replacing/restarting **all** service instances, and updating
the Nexla credential through its secure UI. Confirm the old token is rejected
on every instance; changing the environment alone does not revoke a running
app's token. To disable access, set the flag to `false` and replace/restart all
instances, or deny traffic at ingress immediately. Tokens are not logged, and
the settings object's representation excludes the token.

## Local-only setup

With the existing application dependencies installed, from `player-service-app`:

```shell
source .venv/bin/activate
export PLAYER_INTEGRATION_ENABLED=true
export PLAYER_INTEGRATION_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
flask --app app --no-debug run --host 127.0.0.1 --port 8000 --no-reload
```

This command deliberately binds **loopback only** and disables debug/reloading.
It is a development check, not a production deployment. Stop it with Ctrl-C.
The original `python app.py` development entry point still enables debug and
must not be exposed publicly.

From another shell with the token securely available, a small authenticated read
can send the header on standard input rather than putting the credential in
curl's command-line arguments:

```shell
printf 'Authorization: Bearer %s\n' "$PLAYER_INTEGRATION_TOKEN" |
  curl --silent --show-error --fail-with-body --header @- \
    --header 'X-Request-ID: local-search-1' \
    'http://127.0.0.1:8000/integrations/express/v1/players?q=Aaron&limit=1'
```

Do not use verbose HTTP logging with real credentials. A loopback URL is not
reachable from hosted Nexla. Production use requires an approved HTTPS ingress,
authentication/rate controls, request/header/body limits, a production WSGI
server, and private storage. Those deployment gates are not implemented or
validated by this increment. Never expose the legacy routes, SQLite, Ollama,
debugger, or model service to make the connector work.

## Authentication

All implemented integration operations require
`Authorization: Bearer <application token>`. The scheme is case-insensitive;
the token is case-sensitive. Use exactly one space between scheme and token.
Malformed, duplicate, missing, or incorrect credentials receive the same 401
response with a `WWW-Authenticate: Bearer` challenge.

Authentication precedes query parsing, routing errors, and database access for
the entire `/integrations/express/v1` namespace, including unknown URLs and
unsupported methods. Flask's automatic HEAD and OPTIONS methods are also
authenticated. HEAD performs the bounded read but returns no body; OPTIONS
does not query the database. No CORS access is enabled.

## Search and filtering

`GET /integrations/express/v1/players` accepts the following parameters:

| Query parameter | Semantics |
| --- | --- |
| `q` | Literal substring of ID, first/last/given name, or first + space + last name. 1-100 printable Unicode characters; whitespace-only text is invalid. `%`, `_`, and backslash are literals. SQLite folds ASCII case only. |
| `birth_country` | Exact, case-sensitive country, 1-100 printable Unicode characters; whitespace-only text is invalid. Apostrophes and Unicode are accepted as data. |
| `birth_year_min`, `birth_year_max` | Inclusive bounds from 1 through 9999; minimum cannot exceed maximum. Unknown years do not satisfy a bound. |
| `bats` | Exactly `L`, `R`, or `B`. |
| `throws` | Exactly `L`, `R`, or the dataset's recorded `S`. |
| `limit` | 1-100, default 20. |
| `offset` | 0-100000, default 0. |

Omitted filters impose no restriction; supplied filters combine with AND.
Inputs are not trimmed or case-normalized. Integer values must use canonical
unsigned ASCII decimal notation: no signs, whitespace, leading zeros (except
`0` itself), decimal points, exponents, or Unicode digits.

Only the eight named scalar parameters are accepted, each at most once.
Unknown parameters, duplicate parameters (including encoded duplicate names),
empty/malformed pairs, invalid UTF-8/percent escapes, and invalid values return
400. The **encoded query string** is capped at 4096 bytes; exceeding this cap
returns 414 after authentication. Bodies are never used as tool inputs. For
GET/HEAD, a nonzero declared `Content-Length` or any `Transfer-Encoding` is
rejected without reading the stream. The WSGI
`input_terminated` capability flag alone does not mean a body is present;
servers such as Gunicorn also set it for empty requests. Ingress must enforce
framing and independent HTTP limits; this is not a hard server-level request-size
limit.

Successful responses have `data`, `pagination`, and `request_id`. `data` contains
the full stored records with the same 24 field names, types, and nullable values
as the legacy service. An empty search is a successful result, for example:

```json
{
  "data": [],
  "pagination": {
    "limit": 20,
    "offset": 0,
    "total": 0,
    "next_offset": null
  },
  "request_id": "local-search-1"
}
```

Rows are ordered by `playerId` using binary collation. The total and page share a
read snapshot, including pages beyond the end. `next_offset` advances by `limit`
only when more rows exist and the next offset is at most 100000. A null next
offset can therefore indicate the supported window is exhausted; compare the
collected count to `total` and narrow filters if necessary. Separate page
requests do not promise consistency across a later database refresh.

The existing service performs parameterized, read-only SQLite reads and closes
connections on success and failure. Both schema inspection and the operation read
use configured deadlines, but **each has its own budget**. These cooperative
SQLite deadlines are not a hard total HTTP timeout; response conversion,
serialization, and blocked filesystem operations require separate production
controls. Nonfinite numeric values or unserializable stored values return a
storage error, not a successful nonstandard `Infinity`/`NaN` JSON response.

## Player details

`GET /integrations/express/v1/players/{player_id}` retrieves one player by exact
ID, for example `/integrations/express/v1/players/aaronha01`. It uses the same
authentication, request-size/body restrictions, SQLite deadlines, safe JSON
serialization, correlation headers, and no-store policy as search.

The decoded ID must contain **1-32 ASCII alphanumeric characters**. Matching is
case-sensitive using SQLite binary collation, even if the stored column uses a
different collation. IDs are not trimmed, case-normalized, or coerced. Percent
encoding of valid ASCII characters is accepted, but is not decoded a second time.
Invalid IDs return 400 before any database access. An embedded slash, such as
`a%2Fb`, is decoded before Flask routing and creates extra path segments. This
returns a routing 404 instead; authentication still precedes that response.

No query parameters are supported: **any nonempty query string returns 400**,
including search filters, pagination, duplicate parameters, or malformed pairs.
The namespace's 4096-byte encoded-query cap still applies first after
authentication. HEAD performs the same validated read without a response body;
automatic OPTIONS is authenticated and performs no database read.

A successful response contains exactly `data` and `request_id`. Unlike search,
`data` is a **single object**, not an array: the full stored record with the same
24 fields, types, and nullable values as the legacy service. There is no
pagination. An absent, well-formed ID returns 404 with code `player_not_found`,
not an empty object or an HTTP-200 error. The error message does not echo the ID.

`PlayerService.get_player()` shares ID validation with comparisons and uses one
parameterized, read-only SELECT. It retrieves at most **two rows**, allowing it
to detect duplicate stored IDs in the current schema without an unbounded fetch.
Duplicate selected IDs return 503 `storage_unavailable`, never an arbitrary
record. Invalid or duplicate records outside the selection do not block the
lookup. All operation connections close on success or failure.

The existing `/v1/players/{player_id}` route is unchanged: it still accepts its
legacy ID inputs, returns a bare record on success, and retains its HTTP-200
missing-player error. The stricter bounds and 404 contract apply only to the
integration detail operation.

## Population statistics

`GET /integrations/express/v1/statistics` describes the **entire filtered
population**, not a search page. For example,
`/integrations/express/v1/statistics?birth_country=USA&group_limit=1`
summarizes all matching players while returning at most one category per grouped
field. It uses the same bearer authentication, query-size/body restrictions,
SQLite deadlines, correlation headers, and no-store policy as search.

| Query parameter | Semantics |
| --- | --- |
| `q`, `birth_country`, `birth_year_min`, `birth_year_max`, `bats`, `throws` | The same optional, AND-combined [search filters](#search-and-filtering), with identical validation, literal text matching, and null behavior. |
| `group_limit` | 1-100, default 20. Caps the returned categories independently for each grouped field, never the number of players summarized. |

Only these seven scalar parameters are accepted, each at most once.
`group_limit` uses the same canonical unsigned ASCII decimal notation as search
integers. Unknown/duplicate parameters, malformed UTF-8/percent escapes, empty
values, and invalid filters return 400 before any database access. **`limit` and
`offset` are not supported**, nor are caller-supplied grouping fields or SQL.
There is no group pagination. HEAD performs the same validated, bounded read
without a body; authenticated OPTIONS performs no database read.

A successful response contains exactly `total`, `numeric`, `groups`, and
`request_id`; there is no top-level `data` or `pagination`:

| Response field | Contents |
| --- | --- |
| `total` | Integer count of every matching player, including players with missing attributes. |
| `numeric` | Objects keyed by `birthYear`, `height`, and `weight`, each containing `known_count`, `missing_count`, `min`, `max`, and `mean`. |
| `groups` | Objects keyed by `birthCountry`, `bats`, and `throws`, each containing `data`, `known_count`, `missing_count`, `total_groups`, `limit`, and `other_count`. |
| `request_id` | The same correlation identifier as the `X-Request-ID` response header. |

Numeric summaries exclude SQL NULL from the extrema and mean; zero is a known
value, not missing. If no values are known, `min`, `max`, and `mean` are null.
An empty population is HTTP 200 with `total=0`, zero counts, null numeric measures,
and empty category arrays, not a missing-player error.

Each group's `data` is an array of `{"value": <stored value>, "count": <integer>}`
objects ordered by count descending, then exact value ascending using binary
collation. SQL NULL is counted only in `missing_count`; empty strings and literal
`Unknown` remain distinct known values. `total_groups` counts all distinct
non-null categories; `other_count` counts **players in omitted categories**, not
the number of omitted categories. For each field,
`known_count + missing_count = total`; for each group,
`sum(data[*].count) + other_count = known_count`.

A positive `other_count` means the distribution is truncated. Raise `group_limit`
or narrow the filters rather than treating the returned groups as complete.
The maximum of 100 covers all 55 known birth countries in the supplied seed,
but does not promise complete distributions for an arbitrary future database.
Values retain their source meaning; the API does not assert measurement units,
current-season data, baseball performance statistics, or player quality.

`PlayerService.get_player_statistics()` computes the summary and three group
queries in one read-only transaction and shared operation deadline. They observe
one SQLite snapshot even during a concurrent WAL write. Schema inspection has
its own deadline, as with search; neither budget is a hard total HTTP timeout.
Invalid numeric storage, overflowing aggregates, or unserializable returned
group values produce 503 `storage_unavailable`. A failed group query never
returns a partial summary. Deadline cancellation produces 504 `query_timeout`;
connections close on success and failure.

## Numeric rankings

`GET /integrations/express/v1/rankings` orders players by a supported numeric
attribute, for example
`/integrations/express/v1/rankings?metric=height&birth_country=USA&limit=5`.
It uses the same bearer authentication, query-size/body restrictions, SQLite
deadlines, correlation headers, and no-store policy as search.

| Query parameter | Semantics |
| --- | --- |
| `metric` | **Required.** Exactly `height`, `weight`, or `birthYear`, case-sensitive. There is no default or support for performance statistics, player quality, or computed age. |
| `direction` | Exactly `asc` or `desc`, default `desc`. |
| `q`, `birth_country`, `birth_year_min`, `birth_year_max`, `bats`, `throws` | The same optional, AND-combined [search filters](#search-and-filtering), with identical validation, literal text matching, and null behavior. |
| `limit` | 1-100, default 20. Caps the page size, not the population being ranked. |
| `offset` | 0-100000, default 0. |

Only these ten scalar parameters are accepted, each at most once. Enum values
are not trimmed or case-normalized; integer values use the same canonical
unsigned ASCII decimal notation as search. Missing/invalid metrics, invalid
directions or filters, unknown/duplicate parameters, malformed encoding, and
out-of-range pages return 400 **before any database access**, including schema
inspection. `group_limit`, arbitrary ordering/grouping fields, and SQL are not
accepted. HEAD performs the same validated read without a response body;
authenticated automatic OPTIONS performs no database read.

A successful response contains exactly:

| Response field | Contents |
| --- | --- |
| `metric`, `direction` | The selected metric and direction, including the default direction. |
| `data` | A bounded array of full stored records with the same 24 field names, types, and nullable values as search. Records with a null selected metric are excluded. |
| `pagination` | `limit`, `offset`, `total`, and nullable `next_offset`. The total counts every **rankable** player in the filtered population, not just this page. |
| `excluded_count` | Number of players in the entire filtered population with a null selected metric, independently of the page. |
| `request_id` | The same correlation identifier as the `X-Request-ID` response header. |

Ordering is numeric by the selected metric, then `playerId` **ascending using
binary collation in both directions**. Ties are stable across pages for unchanged
data with unique IDs. No competition/dense rank numbers are assigned. Zero and
fractional values remain known measurements; null is never ranked as zero.
No measurement-unit, freshness, performance, or player-quality claims are added.

`pagination.total + excluded_count` equals the full filtered population size.
Empty populations return HTTP 200 with no rows, zero total, and zero exclusions.
All-null populations also return HTTP 200 with no rows and zero rankable total,
but report every matching player as excluded. Pages beyond the end retain these
full counts. The same [pagination window](#search-and-filtering) applies as in
search: a null `next_offset` can mean the 100000 offset cap was reached, not that
all rankable rows were returned. Narrow filters when the window is insufficient.

`PlayerService.rank_players()` validates, counts, and selects the page within one
read-only transaction and shared operation deadline. Schema inspection has its
own deadline; neither is a hard total HTTP timeout. Separate page requests do not
promise a consistent revision during a database refresh.

Nonnumeric or infinite values of the selected metric **anywhere in the filtered
population** produce 503 `storage_unavailable`, even when the requested page
would be empty or omit the invalid row. Invalid metric values outside the
filters do not block valid results. Returned nonfinite/unserializable fields,
including attributes other than the selected metric, also produce 503, never
partial rows or successful nonstandard JSON. Deadline cancellation produces
504 `query_timeout`; connections close on success and failure.

## Player comparisons

`GET /integrations/express/v1/comparisons` compares an ordered selection of
players, for example
`/integrations/express/v1/comparisons?player_ids=aaronto01,aaronha01`.
It uses the same bearer authentication, query-size/body restrictions, SQLite
deadlines, strict JSON serialization, correlation headers, and no-store policy
as the other operations.

The only accepted query parameter is **`player_ids`**, required exactly once.
Its value is a **comma-separated list of 2-5 distinct IDs**. Each ID must be
1-32 ASCII alphanumeric characters and is matched exactly, case-sensitively
using binary collation. Input order is retained; IDs are not trimmed,
case-normalized, sorted, or deduplicated.

Percent-encoded names, valid ASCII ID characters, and commas (`%2C`) are
accepted. Decoding happens once, before splitting and validating the list.
Repeated query keys are **not** an alternative list encoding; JSON arrays,
empty list elements, spaces around commas, alternate separators, malformed
UTF-8/percent escapes, duplicate IDs, and out-of-range cardinality return 400.
Search filters, ranking/grouping parameters, pagination, and all other query
parameters are rejected. Validation uses the same ID/cardinality/uniqueness
helper as the service and happens **before any database access**, including
schema inspection.

A successful response contains exactly:

| Response field | Contents |
| --- | --- |
| `data` | 2-5 full stored records in the requested order, retaining the same 24 field names, types, and nullable values as search. |
| `differences` | Every unordered pair in input-position order. Each object contains `left_player_id`, `right_player_id`, and `numeric`. |
| `request_id` | The same correlation identifier as the `X-Request-ID` response header. |

Each `numeric` object contains `birthYear`, `height`, and `weight`, calculated
as **left value minus right value**. For `[a, b, c]`, the pairs are `(a, b)`,
`(a, c)`, then `(b, c)`. Reversing two input IDs reverses their difference's sign.
A difference is null if either source value is null, including when both are
unknown; zero and fractional values remain numeric. These are source attributes,
not age calculations, performance statistics, player-quality judgments, or
claims about measurement units or freshness.

There is **no pagination**: at most five records and ten pairwise differences
are returned. Any absent, well-formed ID produces a sanitized 404
`player_not_found`, including an entirely empty database. There are no partial
comparisons, empty successful missing-player results, or echoed missing IDs.
HEAD performs the same validated read without a response body; authenticated
automatic OPTIONS performs no database read.

`PlayerService.compare_players()` performs one parameterized, read-only SELECT
for all selected records, not separate detail calls. Records and differences
therefore describe one SQLite snapshot. Retrieval is bounded to the requested
ID count plus one (at most six rows) to detect duplicate selected stored IDs.
Those duplicates return 503 `storage_unavailable`, never an arbitrary record.

All three compared numeric attributes are checked for every selected player,
even when another player's value is null. Invalid or infinite numeric storage,
overflowing differences from finite operands, and nonfinite/unserializable
returned fields produce 503, never partial or nonstandard JSON. Invalid records
or duplicate IDs outside the selection do not block valid comparisons.
The read uses the configured operation deadline; schema inspection has its own
budget, and neither is a hard total HTTP timeout. Cancellation produces 504
`query_timeout`; connections close on success and failure.

## Errors and correlation

All integration JSON errors use this envelope:

```json
{
  "error": {
    "code": "unauthorized",
    "message": "A valid bearer token is required"
  },
  "request_id": "local-search-1"
}
```

| Status | Code | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | Invalid, ambiguous, or unsupported input, including a GET/HEAD body. |
| 401 | `unauthorized` | Missing, malformed, or incorrect application credential. |
| 404 | `not_found` | No implemented route at this URL. |
| 404 | `player_not_found` | One or more well-formed requested player IDs do not exist. |
| 405 | `method_not_allowed` | Unsupported method; the response preserves the `Allow` header. |
| 414 | `request_too_large` | Encoded query string exceeds 4096 bytes. |
| 503 | `storage_unavailable` | Missing, locked, corrupt, or unusable storage, including invalid stored JSON values, invalid/overflowing numeric analytics, or duplicate selected detail/comparison IDs. |
| 504 | `query_timeout` | Database read deadline exceeded. |
| 500 | `internal_error` | Unexpected failure, with sanitized diagnostics in the request log. |

Messages never include credentials, raw query values, SQL, database paths, or
exception messages. Errors do not become empty successful results. The HTTP
layer catches unexpected exceptions only within the integration blueprint;
legacy exception behavior remains unchanged.

`X-Request-ID` accepts 1-64 ASCII characters: an initial letter/digit followed
by letters, digits, `.`, `_`, or `-`. Missing/invalid values are replaced with a
UUID rather than echoed. JSON success/error envelopes include `request_id`, and
all integration responses, including HEAD/OPTIONS and routing failures, return
the header, `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`.

Request logs use a JSON message with UTC timestamp, fixed operation/route
template, status, duration, request ID, result count, error code, and exception
class where applicable. Unknown routes use the literal `unmatched`, not the raw
path. Detail requests log a fixed route template, not the player ID, and report
one result on success or zero on failure. Statistics log the **full matched
population** as `result_count`, not the number of returned categories; failures
report zero, including failures after some aggregates have been computed.
Rankings log the number of **returned page rows**, not the rankable total or
excluded count, and report zero on failure.
Comparisons log the number of **returned players**, not pairwise differences,
and report zero on failure, including missing IDs and serialization failures.
Tokens, request/query/body contents,
player records, SQL, and exception messages/tracebacks are excluded. Successes
use INFO and failures use WARNING
on Flask's application logger; configure logging to retain INFO when deploying.
Production log-handler formatting remains pending; operational metrics are
implemented below.
Web server/proxy access logs are separate and may include raw URIs; disable or
redact those logs before production exposure. The application log tests do not
validate those server logs. No application rate limiter or per-user authorization
model is claimed.

## Operational metrics

`GET /integrations/express/v1/metrics` exports **Prometheus text**, not a JSON
player response. It is installed only when the integration is enabled and uses
the **same bearer token** as the player operations. Authentication precedes
validation; no query parameters are supported, and the shared query-size/body
restrictions, HEAD/OPTIONS authentication, sanitized JSON errors, correlation,
and no-store headers apply. Successful scrapes carry `X-Request-ID` only in the
header, never in a metric label or response envelope.

**Keep this endpoint private.** The token grants access to both player reads and
metrics; it does not provide a separate operator privilege. Approved ingress
must allowlist only the five player routes for Nexla and deny the metrics route
externally, rather than expose the entire integration prefix. Do not register
metrics as an Express tool, connector source, or MCP operation. The player
OpenAPI contract deliberately omits it.

With the local-only server and token available as described above:

```shell
printf 'Authorization: Bearer %s\n' "$PLAYER_INTEGRATION_TOKEN" |
  curl --silent --show-error --fail-with-body --header @- \
    'http://127.0.0.1:8000/integrations/express/v1/metrics'
```

The pinned `prometheus-client==0.26.0` runtime dependency supports Python 3.9+.
Each Flask app owns a separate registry containing only these metric families;
default Python/process collectors are not exported:

| Metric | Labels and meaning |
| --- | --- |
| `player_service_express_requests_total` | `operation`, `status`; one count per integration response, including failures, HEAD/OPTIONS, routing errors, and metrics scrapes. |
| `player_service_express_errors_total` | `operation`, `code`; the subset with status >= 400. Authentication failures use `code="unauthorized"`; database deadlines use `code="query_timeout"`. Other codes follow the documented errors above, with `http_error` as the bounded fallback. |
| `player_service_express_request_duration_seconds` | `operation`; histogram with `_bucket`, `_count`, and `_sum` samples. Uses the same monotonic duration as the request log, in seconds without millisecond rounding. |

Operations are the five fixed `express_integration.<operationId>` values,
`express_integration.get_metrics`, or `unmatched`. Unknown URLs and unsupported
methods use `unmatched`, not their raw paths or method strings. Status labels
are decimal HTTP codes from 100 through 599, or `other`. Error codes are
allowlisted. Player/request IDs, query text, credentials, exception messages,
raw routes, and methods never become labels. The client also exports standard
`_created` samples; label series appear on their first observation.

Latency covers the integration request hook through response preparation,
including authentication, validation, and serialization. It excludes queueing,
socket transfer, and later log/response-finalization work, so it is **not an
end-to-end network latency or hard HTTP timeout**. Inclusive histogram bounds
are 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, and 120
seconds, plus `+Inf`.

A scrape renders before its own request is recorded, so that scrape becomes
visible on the next scrape. A fresh app's first scrape contains metric
definitions but no samples. Use operation labels to separate scrape traffic
from player-operation rates and latency. Scraping does not initialize or query
SQLite or Ollama and still works with unavailable player storage; it is **not
a database readiness check**. Render failures return a logged, sanitized 500,
not an empty successful scrape.

Collection is **in-memory, per app and per process**, with thread-safe updates.
Counters reset when the app/process is replaced. Use **one WSGI worker process
with threads per independently scraped service instance**. Multiple workers
behind one scrape target would yield incomplete, changing views; multiple
replicas must be scraped individually and aggregated by the monitoring system.
The [Prometheus client's multi-process mode](https://prometheus.github.io/client_python/multiprocess/)
requires a different registry, directory lifecycle, and worker hooks and is
deliberately not implemented. Both multi-process environment-variable spellings
are rejected when the integration is enabled, but the app cannot enforce an
external server's worker count. Production packaging must set and verify this
worker model; no multi-worker, container, or deployed monitoring validation is
claimed.

## Nexla binding and validation boundary

The official [REST credential guide](https://docs.nexla.com/user-guides/connectors/rest_api)
documents **Generic REST API**, authentication **API Key**, location **HEADER**.
Set the parameter name to `Authorization` and its value to `Bearer ` followed by
the application token in Nexla's secure credential form. Do not substitute a
Nexla service key, disable certificate verification, or pass this token as a
tool argument. A shared application token does not establish an individual
Express user's identity in Flask.

Statistics return an aggregate envelope, not rows at `$.data`. Do not reuse the
search response-extraction path for this operation: preserve `total`, `numeric`,
`groups`, and correlation metadata when reviewing its generated tool binding.
Rankings do have rows at `$.data`, but their generated tools must also preserve
`metric`, `direction`, `excluded_count`, pagination, and correlation metadata.
Dropping that metadata can hide excluded players or an incomplete traversal.
Comparison bindings must encode ordered IDs as one comma-separated `player_ids`
value, not repeated parameters, and retain both `data` and `differences` with
correlation metadata. Extracting only `$.data` discards the calculated comparison.
Do not apply search pagination to comparisons or reorder their input/output.

This is a supported header-credential mechanism, not evidence that MCP Studio
has discovered or bound the endpoint. There is no documented arbitrary Express
chat API or assumed OpenAPI import in this implementation. Do not publish a
server, create platform resources, deploy infrastructure, or expose a URL
without approval. Follow the architecture document's tenant validation gates
once the remaining application work and external prerequisites are satisfied.

Run local automated coverage from the repository root:

```shell
source player-service-app/.venv/bin/activate
python -m pip install -r player-service-app/requirements-test.txt
python -m pytest -c player-service-app/pytest.ini player-service-app/tests -q
```

For an activated `uv` environment without pip, use
`uv pip install -r player-service-app/requirements-test.txt` for installation.
To run only the contract checks, select
`player-service-app/tests/test_express_openapi.py` instead of the tests directory.

The complete local suite passes **1524 tests**. Coverage includes disabled/invalid
startup, token isolation/rotation behavior,
strict HTTP parsing, exact size/page bounds, auth before database access,
correlated/redacted errors, real SQLite lock/deadline failures, cleanup,
nonfinite-output rejection, and complete authenticated Flask test-client
pagination over **19,370 unique seed IDs**. Detail coverage includes exact
ID/length/case boundaries, malformed encoded paths, rejected query inputs,
missing/duplicate records, bounded retrieval, and independent CSV expectations
for seed records with nullable fields. Legacy player responses and mocked
Ollama success/failure behavior are retained. Statistics coverage verifies all
shared filters, exact group-limit bounds over 101 categories, empty/all-null
populations, rejected pagination, nonfinite/overflowing aggregates, safe group
failures, redacted logs, and independent full-seed and filtered-seed expectations.
Ranking coverage checks every metric/direction, required enums before schema
reads, all ten inputs together, exact page/offset bounds, binary ties, exclusions,
empty/all-null populations, off-page invalid metrics, and page/serialization
failures. Complete HTTP pagination in all six metric/direction combinations
matches independent CSV-derived records across the **19,370-player** population:
18,615 rankable heights, 18,533 weights, and 19,244 birth years.
Comparison coverage verifies exact 2-5-ID/length/case bounds, input/pair order,
single-value list encoding, strict parsing before schema reads, null/zero/fractional
differences, explicit missing/duplicate errors, bounded retrieval, invalid numeric
storage and subtraction overflow, serialization/query failures, and redacted logs.
Five-player HTTP comparisons match independently converted seed records and all
ten pairwise differences, including missing birth years, heights, and weights.
The 125 OpenAPI checks validate the document and local references, match routes
and parameter allowlists to Flask, execute parameter examples, and validate
success/error bodies and required headers. They cover nullable/fractional
records, empty populations, list encoding, routing failures, actual missing
and locked storage, and rejection of malformed response shapes. Every search
page across all **19,370 seed players** conforms to the response schema;
seed statistics, rankings, nullable details, and maximum-size comparisons are
also schema-checked.
The 74 metrics tests cover exact histogram boundaries/units, all read-operation
success/error observations, authentication and validation, real SQLite locks and
deadlines with recovery, redaction/cardinality, app isolation, thread-safe counts,
concurrent Flask request IDs, private scrape behavior without database access,
sanitized scrape failures, and unsupported multi-process configuration.
Flask test-client coverage is not
a real network, container, live Ollama, or Express/Nexla end-to-end result.
