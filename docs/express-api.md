# Express integration API: authenticated search

**Status: the opt-in search/filter endpoint is implemented and locally tested.**
Player details, statistics, rankings, comparisons, and the OpenAPI contract are
not yet exposed through this API. Their shared service methods already exist.
Metrics, production packaging, real loopback HTTP smoke tests, and authenticated
Express/Nexla validation remain pending.

The [architecture assessment](express-integration-architecture.md) records the
verified platform mechanisms, integration decision, remaining acceptance gates,
and external blockers. This endpoint is not a published MCP server or proof of
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

## Search and filtering

`GET /integrations/express/v1/players` requires
`Authorization: Bearer <application token>`. The scheme is case-insensitive;
the token is case-sensitive. Use exactly one space between scheme and token.
Malformed, duplicate, missing, or incorrect credentials receive the same 401
response with a `WWW-Authenticate: Bearer` challenge.

Authentication precedes query parsing, routing errors, and database access for
the entire `/integrations/express/v1` namespace, including unknown URLs and
unsupported methods. Flask's automatic HEAD and OPTIONS methods are also
authenticated. HEAD performs the bounded read but returns no body; OPTIONS
does not query the database. No CORS access is enabled.

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
connections on success and failure. Both schema inspection and the search read
use configured deadlines, but **each has its own budget**. These cooperative
SQLite deadlines are not a hard total HTTP timeout; response conversion,
serialization, and blocked filesystem operations require separate production
controls. Nonfinite numeric values or unserializable stored values return a
storage error, not a successful nonstandard `Infinity`/`NaN` JSON response.

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
| 404 | `not_found` | No implemented route at this URL; detail and analytics URLs are still unavailable. |
| 405 | `method_not_allowed` | Unsupported method; the response preserves the `Allow` header. |
| 414 | `request_too_large` | Encoded query string exceeds 4096 bytes. |
| 503 | `storage_unavailable` | Missing, locked, corrupt, or unusable storage, including invalid stored JSON values. |
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
path. Tokens, request/query/body contents, player records, SQL, and exception
messages/tracebacks are excluded. Successes use INFO and failures use WARNING
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

The complete local suite passes **721 tests**. Coverage includes disabled/invalid
startup, token isolation/rotation behavior,
strict HTTP parsing, exact size/page bounds, auth before database access,
correlated/redacted errors, real SQLite lock/deadline failures, cleanup,
nonfinite-output rejection, and complete authenticated Flask test-client
pagination over **19,370 unique seed IDs**. Legacy player responses and mocked
Ollama success/failure behavior are retained. Flask test-client coverage is not
a real network, container, live Ollama, or Express/Nexla end-to-end result.
