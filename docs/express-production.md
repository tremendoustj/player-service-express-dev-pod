# Player Service production WSGI profile

**Status: the Gunicorn entry point and logging are implemented and tested over
real loopback HTTP.** This is not a container, deployment, approved public
endpoint, or authenticated Express/Nexla validation. Docker packaging, controlled
database provisioning, and tenant validation remain separate increments.

## Run locally

This optional profile requires **Unix and Python 3.10+**. The runtime manifest
pins the published `gunicorn==26.2.0` release only on those platforms; the legacy
Flask application keeps its Python 3.9+ requirement. The profile was exercised
with Python 3.11.17 on macOS, not Windows or a container.

From `player-service-app`, with a compatible virtual environment activated:

```shell
python -m pip install -r requirements.txt
export PLAYER_INTEGRATION_ENABLED=true
export PLAYER_INTEGRATION_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m gunicorn --config gunicorn.conf.py
```

For an activated `uv` environment without pip, use
`uv pip install -r requirements.txt` instead. For persistent environments, obtain
the application token from an approved secret manager rather than generating a
new token on each shell invocation. No `.env` file is loaded automatically.
Keep credentials out of command-line options, `GUNICORN_CMD_ARGS`, URLs, and
diagnostic configuration dumps. Leave `GUNICORN_CMD_ARGS` unset for this profile.

The listener is **127.0.0.1:8000**, not a public interface. Use the authenticated
[local API examples](express-api.md#local-only-setup) to call it. Stop with Ctrl-C
or send SIGTERM to the specific Gunicorn master PID. This runs the existing Flask
app through `wsgi:app`, without the development debugger or reloader.
`app:app`, `create_app()`, and the existing development command remain available;
using a different server command does not automatically apply this profile.

## Process and HTTP policy

The checked-in [`gunicorn.conf.py`](../player-service-app/gunicorn.conf.py) uses
the [official Gunicorn settings](https://gunicorn.org/reference/settings/):

| Setting | Policy |
| --- | --- |
| Workers | Exactly one `gthread` worker, four threads by default. Configuration overrides selecting multiple workers or a non-threaded worker fail startup. Worker-count changes also fail rather than silently breaking metrics. Scale with separately scraped instances. |
| Reloading | Gunicorn's code reloader is rejected. Do not send live-reload/re-exec or worker-scaling signals; replace/restart the instance for code or credential changes. Graceful reloads can temporarily overlap workers and invalidate the single-process scrape model. |
| Control socket | Disabled; the profile does not create Gunicorn's default runtime-control socket. |
| Worker heartbeat timeout | 30 seconds. With `gthread`, this detects a silent worker, **not an individual request's execution deadline**. |
| Graceful shutdown / keep-alive | 30 seconds for shutdown; 2 seconds of keep-alive idle time. |
| Request line | At most 8190 bytes, excluding its trailing CRLF. |
| Headers | At most 32 fields; each field is at most 4096 bytes including its trailing CRLF in the pinned parser. |
| Proxy trust | No forwarded IPs or secure-scheme headers are trusted automatically. Configure any future proxy trust explicitly for approved ingress; this profile does not install `ProxyFix`. |

Explicit worker/listener settings take precedence over `WEB_CONCURRENCY` and `PORT`.
Do not override the supplied logging configuration or enable debug/spew output.
Changing arbitrary Gunicorn options is not a supported way to preserve these
tested guarantees.

Gunicorn's parser rejects an oversized request line with 400 and oversized
headers with 431 **before Flask runs**. Such responses are server-generated
HTML, not the integration JSON envelope: they have no application request ID
or application metrics observation. Parser diagnostics are still sanitized.
Requests admitted to Flask retain authentication-first validation, the separate
4096-byte encoded-query cap, correlation headers, and all existing API contracts.

These are not a complete slow-client, body-size, rate, concurrency, or total
request-time policy. SQLite deadlines remain cooperative per-read budgets.
Approved ingress must provide TLS, independent HTTP/body/time/rate limits, and
safe access logging. It must expose only the five player operations to Nexla,
not the entire integration prefix: metrics, legacy routes, SQLite, Ollama, and
the model service remain private. Binding this server directly to a public
interface is not an approved deployment.

## Logging and startup failures

The profile emits newline-delimited JSON to stdout. Integration events retain
the existing timestamp, request ID, operation/route template, duration, status,
result count, error code, and exception class, with additional `level`, `logger`,
and `process_id` metadata. Successful requests are retained at INFO and failures
at WARNING. Each request is emitted once, without propagation duplicates.
The application still emits its original JSON log message for other logging
configurations; handler changes are confined to this Gunicorn profile.

Gunicorn access output is explicitly discarded with a non-propagating
`NullHandler`. **`accesslog=None` alone is insufficient**: Gunicorn enables its
access-log path when a logging dictionary is present. Neither raw URIs nor
authorization, user-agent, referrer, or other request headers are emitted.

Other log records become `server_log` events containing timestamp, severity,
logger, process ID, code source location, and exception class when supplied.
The formatter never renders their free-form messages, interpolation arguments,
cached exception text, or tracebacks; parser errors can contain private request
data even when access logs are disabled. This deliberately trades detailed
server exception text for safe diagnostics. Use severity, source, exception
class, process state, and correlated integration events for investigation.

Gunicorn also prints worker boot exceptions directly to stderr, outside logging
handlers. The production WSGI entry point therefore logs expected application
startup failures (configuration, dependency imports, file/CSV errors, and
SQLAlchemy initialization failures) and re-raises a fixed, fatal error:
`Player Service startup failed; inspect structured logs`. The original exception
class remains in JSON, but its message, storage path, SQL, and records do not.
Known profile-policy violations can likewise produce a fixed stderr error
banner. Gunicorn command/config-file parsing errors occur outside this boundary;
never supply secrets through CLI flags or custom logging/configuration code.

Flask startup still seeds its application-relative `player.db` from `Player.csv`
only when the database is absent. Existing database files are not reseeded.
Startup and reads with an existing read-only file are covered, but this is not
container read-only-mount validation. Fresh startup needs a writable application
directory. A configurable database path, secret-file injection, and explicit
controlled initialization/validation remain pending packaging work.

## Validation boundary

The 29 production/logging tests cover exact JSON output and redaction, default
and rejected server policies, fresh CSV initialization, existing read-only
storage, disabled integration, safe startup failures, and actual loopback HTTP
calls for all five player operations. They also cover concurrent request IDs
and metrics in one worker, legacy reads, authentication and validation errors,
unavailable storage, parser failures, and exact request-line/header boundaries.
Each subprocess uses a temporary application copy and an ephemeral loopback
socket; teardown stops and checks its isolated server/worker process group.

Run the focused checks from the repository root:

```shell
source player-service-app/.venv/bin/activate
python -m pytest -c player-service-app/pytest.ini \
  player-service-app/tests/test_server_logging.py \
  player-service-app/tests/test_production_server.py -q
```

The complete suite passes **1553 tests**. Production-server tests are explicitly
skipped on Windows and Python < 3.10, where Gunicorn is not installed by the
conditional dependency; that is not evidence of production support there.
No live Ollama, Docker, external MCP, Nexla connector, or Express chat execution
is established by these checks. The architecture document's
[external blockers](express-integration-architecture.md#current-external-blockers-and-unverified-items)
and approval requirements still apply.
