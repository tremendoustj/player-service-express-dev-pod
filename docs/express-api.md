# Express integration API: authenticated search, details, and statistics

**Status: opt-in search/filter, player-detail, and population-statistics endpoints
are implemented and locally tested.** Rankings, comparisons, and the OpenAPI
contract are not yet exposed through this API. The remaining service methods
already exist.
Metrics, production packaging, real loopback HTTP smoke tests, and authenticated
Express/Nexla validation remain pending.

The [architecture assessment](express-integration-architecture.md) records the
verified platform mechanisms, integration decision, remaining acceptance gates,
and external blockers. These endpoints are not a published MCP server or proof of
a working Express chat integration.

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
| 404 | `not_found` | No implemented route at this URL; `/rankings` and `/comparisons` remain unavailable. |
| 404 | `player_not_found` | The well-formed requested player ID does not exist. |
| 405 | `method_not_allowed` | Unsupported method; the response preserves the `Allow` header. |
| 414 | `request_too_large` | Encoded query string exceeds 4096 bytes. |
| 503 | `storage_unavailable` | Missing, locked, corrupt, or unusable storage, including invalid stored JSON values, invalid/overflowing numeric aggregates, or duplicate selected detail IDs. |
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
Tokens, request/query/body contents,
player records, SQL, and exception messages/tracebacks are excluded. Successes
use INFO and failures use WARNING
on Flask's application logger; configure logging to retain INFO when deploying.
Production log-handler formatting and operational metrics are still pending.
Web server/proxy access logs are separate and may include raw URIs; disable or
redact those logs before production exposure. The application log tests do not
validate those server logs. No application rate limiter or per-user authorization
model is claimed.

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

This is a supported header-credential mechanism, not evidence that MCP Studio
has discovered or bound the endpoint. There is no documented arbitrary Express
chat API or assumed OpenAPI import in this implementation. Do not publish a
server, create platform resources, deploy infrastructure, or expose a URL
without approval. Follow the architecture document's tenant validation gates
once the remaining application work and external prerequisites are satisfied.

Run local automated coverage from the repository root:

```shell
source player-service-app/.venv/bin/activate
python -m pytest -c player-service-app/pytest.ini player-service-app/tests -q
```

The complete local suite passes **970 tests**. Coverage includes disabled/invalid
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
Flask test-client coverage is not
a real network, container, live Ollama, or Express/Nexla end-to-end result.
