# Express.dev integration: architecture and implementation plan

Assessment date: 2026-11-02.

**Status: shared data access, bounded search/filter, population statistics,
numeric rankings, comparisons, opt-in SQLite deadlines, and authenticated HTTP
search/details/statistics/rankings/comparisons implemented; OpenAPI pending.**
Player/country lookups are
parameterized, reads explicitly close their read-only SQLite connections, and
missing-player handling has regression coverage. Internal search supports validated filters and
deterministic pagination; statistics reuse those filters and explicitly report
missing values and omitted groups. Rankings reuse the filters and pagination,
with deterministic ties and explicit null exclusions. Comparisons preserve
requested player order, report pairwise numeric differences, and reject missing IDs.
All shared reads can opt into a monotonic execution budget and bounded lock waits;
legacy calls retain their existing timeout defaults.
All five opt-in HTTP operations now have
fail-closed configuration, bearer authentication, strict HTTP validation,
correlation IDs, and sanitized structured request log messages.
Detail reads validate exact IDs, bound duplicate detection,
and return an explicit 404 for absent players.
Statistics describe the entire filtered population with bounded groups and
explicit missing/omitted counts, not a paginated subset.
Rankings use required allowlisted metrics, bounded pages, and explicit exclusions.
Comparisons accept 2-5 ordered IDs and return full records and null-aware
pairwise differences, with an explicit 404 if any selected player is missing.
[Their configuration and contracts](express-api.md) distinguish
implemented behavior from the remaining OpenAPI, metrics, packaging, and validation work.
No MCP server is implemented. No infrastructure was deployed
and no Nexla resources were created. This document distinguishes the existing
application, verified platform capabilities, and proposed changes.

## 1. Repository assessment

### Application and API contracts

The application is a small Flask service in
[`player-service-app/app.py`](../player-service-app/app.py), not the separate
recommendation service in `player-service-model`.

| Existing surface | Actual behavior | Integration implication |
| --- | --- | --- |
| `GET /v1/players` | Returns a bare JSON array of every player through `PlayerService.get_all_players()`. No search, filters, sorting contract, or pagination. | Preserve this response; add a separate bounded integration API. |
| `GET /v1/players/<player_id>` | Returns a single player dictionary, or the intended error object with HTTP 200 when absent. The unassigned-variable failure for absent IDs was repaired in increment 2. | Preserve the legacy responses and define a separate 404 contract for new tools. |
| `GET /v1/chat/list-models` | Calls `ollama.list()`; exceptions produce a JSON error with HTTP 503. | Keep independent of the integration. |
| `POST /v1/chat` | Calls TinyLlama with the hard-coded question "Why is the sky blue?"; it does not use a client prompt or query player data. Exceptions produce HTTP 503. | This is not an existing player-question-answering engine. Do not route player tools through it. |
| `PlayerService.search_by_country()` | Unrouted service method returning raw SQLite rows. | Reuse the country-filter concept, not its raw output as a public tool contract. |

[`player_service.py`](../player-service-app/player_service.py) now binds player IDs
and countries as SQL parameters and explicitly closes each read-only connection.
The initial assessment found interpolated lookups and unclosed connections; the
safe shared data-access increment below addresses those prerequisites.
Legacy routes remain unauthenticated and retain their original responses.
The additive integration endpoints implement
authentication, validation, request correlation, and structured log messages.
Operational metrics and production HTTP request limits remain pending. Shared service reads support
optional SQLite deadlines, enabled for all implemented integration operations but not for legacy routes.
The development entry point binds port 8000 on all interfaces with debug enabled.
Live Ollama requests remain unverified; the missing-player error path now has
local Flask test-client coverage.

### SQLite and available data

[`app_paths.py`](../player-service-app/app_paths.py) anchors `Player.csv` and
`player.db` to the application directory. On import, `ensure_player_db()` creates
the database from the CSV using pandas/SQLAlchemy **only if the file is absent**.
There is no migration or refresh mechanism. An existing database is not checked
for schema compatibility. `player.db` is ignored by Git.

The supplied CSV and the database generated for the baseline contain **19,370
rows**, with 19,370 distinct, nonempty `playerId` values. Read-only SQLite
inspection found no primary key or indexes. The 24 columns are:

| Group | Columns |
| --- | --- |
| Identity | `playerId`, `nameFirst`, `nameLast`, `nameGiven`, `retroID`, `bbrefID` |
| Birth | `birthYear`, `birthMonth`, `birthDay`, `birthCountry`, `birthState`, `birthCity` |
| Death | `deathYear`, `deathMonth`, `deathDay`, `deathCountry`, `deathState`, `deathCity` |
| Physical attributes / handedness | `weight`, `height`, `bats`, `throws` |
| Career dates | `debut`, `finalGame` |

Nullable numeric CSV columns become SQLite `FLOAT`; other columns are `TEXT`.
Keep existing field casing and legacy JSON types. Define nullable types explicitly
in the new API rather than assuming every year or measurement is present.

Important constraints for tool design:

- There are **no batting, pitching, fielding, season, team, or position statistics**.
  Statistics can describe this population; rankings cannot claim player quality,
  home runs, batting average, or wins.
- Birth years span 1820-1997. Recorded career dates extend through 2017, not the
  current season. The repository does not establish a refresh schedule or data
  provenance/measurement units; confirm these before labeling units or freshness.
- `bats` contains `L`, `R`, `B`, and missing values. `throws` contains `L`, `R`,
  **`S`**, and missing values. Do not reject the recorded switch-throwing value.
- Birth country has 55 nonempty values and 68 missing records; height is missing
  for 755 records, weight for 837, and birth year for 126. Unknown is not zero.
- The model's separate CSV uses `playerID`, unlike the application's `playerId`.
  Do not substitute that dataset or its transformed features for the API source.

### Ollama, the model service, and containers

The pinned Ollama SDK is `ollama==0.3.3`. The root README describes an independent
Ollama container on port 11434 and a TinyLlama download. Neither is required by the
existing player-list test or by the proposed deterministic player tools.

The repository's only Dockerfile belongs to `player-service-model`: Python 3.11,
Poetry dependencies, and a Flask development server on port 8657. Its
`/team/generate` endpoint uses a persisted nearest-neighbor model over normalized
birth, height, weight, and handedness features. It deliberately simulates some
failures/delays. `/team/feedback` changes in-memory exclusions; the `/llm/*`
endpoints are placeholders. The training notebook builds these similarity
features, not baseball performance predictions. These endpoints are out of scope
for the read-only player integration.

There is no Player Service Dockerfile or Compose configuration. `.gitpod.yml`
starts Ollama and the model service; its model port mapping uses 5000 rather than
the model's 8657. Do not treat that setup as a verified production deployment.

### Test baseline

At the initial assessment, the only tracked automated test was
[`tests/test_app.py`](../player-service-app/tests/test_app.py), which checks that
`GET /v1/players` returns a nonempty array containing `playerId`.
`pytest.ini` supplies the application import path. There is no configured linter,
application build command, model test suite, or external integration test suite.

On 2026-11-02, the initial `python3 -m pytest -q` attempt could not run because
the default Python 3.15 environment lacked pytest. Using an isolated Python
3.11.17 environment and the unchanged requirements, the existing suite passed:
**1 passed**.

Reproduction from the repository root using the available `uv` tool:

```shell
uv venv --python 3.11 player-service-app/.venv
uv pip install --python player-service-app/.venv/bin/python -r player-service-app/requirements.txt
player-service-app/.venv/bin/python -m pytest -c player-service-app/pytest.ini player-service-app/tests -q
```

`uv` is not a new application requirement. A conventional compatible virtual
environment can run the same suite. The original test imported the application
and created the ignored database when absent. Increment 2 redirects the database
path before importing Flask in its test fixture, so the suite now uses isolated
temporary databases instead.

### Safe shared data access (increment 2)

`PlayerService` now accepts an optional filesystem `db_path`, retaining the
application-relative default. Each query opens an existing database using an
escaped file URI with `mode=ro`, enables `PRAGMA query_only`, materializes its
results, and explicitly closes the connection using `contextlib.closing`.
Connection cleanup also applies when SQLite raises an error; a missing file is
not silently created by a service read. The application's existing
`ensure_player_db()` startup initialization is unchanged.

Player and country inputs are bound parameters, not SQL fragments. All stored
fields, nullable values, and legacy return shapes are retained: lists of
dictionaries for player listings, a dictionary for a detail result, and tuples
for the unrouted country lookup. An absent player returns `{}` from the service,
allowing the unchanged legacy route to return its intended
`{"error": "No record found with player_id=..."}` with HTTP 200. This is distinct
from the integration detail API's 404 contract. Storage failures still raise;
they are not converted into empty successful results.

At the end of increment 2, the local suite had **29 passing tests** covering exact legacy responses,
all 24 fields, nullable records, quote-containing/Unicode values, SQL-shaped
inputs, empty/missing results, read-only enforcement, missing/corrupt storage,
URI-sensitive filesystem names, and connection cleanup on success and failure.
Both service and Flask tests use temporary SQLite fixtures; no Ollama or Express
connectivity is required or established by these tests.

Run the complete suite with the existing compatible virtual environment:

```shell
source player-service-app/.venv/bin/activate
python -m pytest -c player-service-app/pytest.ini player-service-app/tests -q
```

### Bounded search and filtering (increment 3)

`PlayerService.search_players()` was introduced as an internal Python service
method; increment 8 now calls it through authenticated HTTP search, not a
published MCP tool. It uses the immutable, validated `PlayerFilters`
value object and a shared parameterized filter builder intended for later
statistics and ranking operations. Existing legacy methods and routes retain
their response formats and semantics.

For an existing, initialized database:

```python
from player_service import PlayerFilters, PlayerService

service = PlayerService("/absolute/path/to/player.db")
page = service.search_players(
    PlayerFilters(q="Aaron", birth_country="USA"),
    limit=20,
    offset=0,
)
```

All filters are optional (`None` means no constraint) and are combined with AND:

| Filter | Implemented service semantics |
| --- | --- |
| `q` | Literal substring across `playerId`, `nameFirst`, `nameLast`, `nameGiven`, or first name + space + last name. SQLite's default LIKE folds ASCII case only: `rené` matches `René`, but `RENÉ` does not. `%`, `_`, and backslash are literal characters, not caller-supplied wildcards. |
| `birth_country` | Exact, case-sensitive match, including Unicode and apostrophes. |
| `birth_year_min`, `birth_year_max` | Inclusive integer bounds, 1-9999; minimum must not exceed maximum. Null years do not satisfy a year constraint. |
| `bats` | Exactly `L`, `R`, or `B`. |
| `throws` | Exactly `L`, `R`, or the dataset's recorded `S`. |

Text filters must contain 1-100 printable Unicode characters and cannot be
whitespace-only. Text is not trimmed or otherwise normalized. Invalid values raise
`ValueError`; numeric strings, floats, and booleans are not silently coerced to
integers. The integration HTTP layer parses its string parameters explicitly
before constructing the service.
Search requires a `PlayerFilters` instance rather than an arbitrary dictionary.

Results contain `data` (full stored records, including nulls) and `pagination`
with `limit`, `offset`, `total`, and nullable `next_offset`. The default limit is
20, accepted limits are 1-100, and offsets are 0-100000. Results are ordered by
`playerId` using binary collation, independent of insertion order. The total
counts the entire filtered population, including when a page is empty or beyond
the end.

Count and page queries share one explicitly started read transaction, so they
observe the same snapshot even if a concurrent WAL writer commits between them.
Connections close on both count and page failures; errors propagate instead of
becoming empty successful results. Separate page requests still do not promise
snapshot consistency across a later database refresh.

`next_offset` advances by `limit` only when more results exist and the next offset
is within the supported cap. It is also null when advancing would exceed 100000,
even if `total` is larger. Full-scan clients must compare collected rows with
`total` and narrow filters if the supported window is insufficient; a null next
offset alone does not prove a complete scan of an arbitrarily large population.

At the end of increment 3, the local suite had **128 passing tests**. Coverage includes literal
search and case behavior, combined filters, nulls, switch handedness, strict
validation, exact page/offset caps, ordering, concurrent snapshot consistency,
and connection cleanup on failure. A temporary database seeded from `Player.csv`
is traversed in 100-row pages and produces **all 19,370 unique IDs** in order,
without duplicates or missing records. This is local SQLite validation, not a
Nexla connector or Express chat test.

Filtered population statistics, rankings, and comparisons are implemented in
increments 4 through 6 below. HTTP search, authentication, and validation are
implemented in increment 8; the other HTTP operations follow in increments 9-12.
OpenAPI, operational metrics, and production packaging remain future increments.
The search limits bound inputs and result sizes, **not query duration**;
opt-in database execution deadlines are implemented in increment 7 below.
Do not expose this service directly as an unauthenticated integration.

### Filtered population statistics (increment 4)

`PlayerService.get_player_statistics(filters=None, *, group_limit=20)` was
introduced as an internal Python method; increment 10 now exposes it through
authenticated HTTP, not a published tool. It reuses the
same validated `PlayerFilters` and parameterized filter builder as search. All
statistics describe the **entire filtered population**, independently of search
pagination or the categorical group limit.

The result has three top-level fields:

| Field | Implemented contents |
| --- | --- |
| `total` | Number of matching players, including players with missing attributes. |
| `numeric` | Summaries keyed by `birthYear`, `height`, and `weight`; each has `known_count`, `missing_count`, `min`, `max`, and `mean`. |
| `groups` | Summaries keyed by `birthCountry`, `bats`, and `throws`; each has `data`, `known_count`, `missing_count`, `total_groups`, `limit`, and `other_count`. |

Numeric summaries exclude SQL NULL values from the mean and extrema. Zero is a
known value, not missing. When no values are known, `min`, `max`, and `mean` are
null, not zero. An empty population has `total=0`, zero known/missing counts,
null numeric measures, and empty group arrays. The values retain the source's
meaning; no measurement units, performance statistics, or current-season claims
are introduced.

Each group's `data` contains `{"value": <stored value>, "count": <player count>}`
objects ordered by count descending, then exact value ascending using binary
collation. Grouping is case-sensitive and does not normalize source values.
SQL NULL is reported separately in `missing_count`, never replaced by a synthetic
"Unknown" category or counted toward the group limit. A stored empty string or
literal "Unknown" remains a distinct known value.

`group_limit` is a strict integer from 1 through 100 (default 20), applied
independently to each categorical field. Booleans, numeric strings, floats, and
out-of-range values raise `ValueError` before opening a query connection.
`total_groups` counts all distinct non-null values, even when only some are
returned. `other_count` counts players in omitted non-null groups, not the number
of omitted groups. For each field:

- `known_count + missing_count = total`.
- `sum(data[*].count) + other_count = known_count`.

A positive `other_count` explicitly signals a truncated distribution. There is
no group offset pagination; callers can raise the limit or narrow filters.
Using `group_limit=100` returns all 55 known birth-country groups in the current
seed, but does not promise complete distributions for arbitrary larger datasets.

The numeric/count query and the three bounded group queries share one explicit
read transaction. They therefore observe the same SQLite snapshot even if a
concurrent WAL writer commits between queries. Only fixed field names enter the
SQL; filter values and the group limit are bound parameters. Connections close
on success and failure. Invalid/nonfinite numeric summaries (including an
overflowing mean) raise `sqlite3.DataError` rather than silently reporting zero,
NaN, or infinity. Other storage failures also propagate, never becoming empty
successful statistics.

At the end of increment 4, the local suite had **194 passing tests**. Statistics coverage
includes every shared filter, literal search, empty/all-null populations, zeros,
switch handedness, exact group-limit boundaries, binary tie ordering, omitted
counts, invalid numeric storage, concurrent snapshot consistency, and cleanup
after failures at each query stage. Independent Python calculations verify
full-seed and filtered-seed summaries across the supplied **19,370 players**.
The existing complete seed-pagination and Flask compatibility tests still pass.
These are local SQLite/Flask results, not Express/Nexla, MCP, or Docker validation.

Rankings and comparisons are implemented in increments 5 and 6 below.
The statistics increment adds no HTTP routes, authentication, query deadlines,
or environment variables.

### Bounded numeric rankings (increment 5)

`PlayerService.rank_players(metric, filters=None, *, direction="desc", limit=20,
offset=0)` was introduced as an internal Python method; increment 11 now exposes
it through authenticated HTTP, not a published tool. The required `metric` is
exactly `height`, `weight`, or `birthYear`.
`direction` is exactly `asc` or `desc`, defaulting to `desc`. No performance
statistics, measurement units, age calculations, or player-quality claims are
introduced.

```python
page = service.rank_players(
    "height",
    PlayerFilters(birth_country="USA", throws="R"),
    direction="desc",
    limit=20,
    offset=0,
)
```

Rankings reuse the same validated `PlayerFilters` as search and statistics.
Records are ordered numerically by the selected metric, then by `playerId`
ascending with binary collation **in both directions**. Ties are stable across
pages for the unchanged seed's unique IDs; no competition/dense rank numbers are
assigned. Full stored records and nullable fields retain their legacy names and
types. Zero and fractional measurements remain known numeric values.

| Result field | Implemented meaning |
| --- | --- |
| `metric`, `direction` | The selected metric and direction, including the default direction. |
| `data` | The bounded page of full player records, excluding SQL NULL values of the selected metric. |
| `pagination` | The same `limit`, `offset`, `total`, and nullable `next_offset` structure as search. Here, `total` counts all **rankable** players in the filtered population, not just this page. |
| `excluded_count` | Number of players in the entire filtered population with a null selected metric, independently of the page. |

`pagination.total + excluded_count` is the full filtered population size.
Unknown values are never ranked as zero. An empty population returns no data,
zero total, and zero exclusions. An all-null population has no ranked data and a
zero pagination total, but reports all matching players in `excluded_count`.
Pages beyond the end still report the full counts.

Page limits remain strict integers from 1 through 100 (default 20), and offsets
from 0 through 100000. Search and rankings share the pagination metadata helper,
including the cap on `next_offset`; a null next offset can mean the supported
window is exhausted rather than that every record was returned. Narrow filters
if the total exceeds that window. Invalid metric/direction values, wrong filter
types, and invalid page arguments raise before opening a query connection.
Only fixed, allowlisted columns and directions enter SQL; filter values, limits,
and offsets are bound parameters.

The population count, null exclusions, metric validation, and page share one
explicit read transaction. Concurrent writes cannot mix counts or validation
from one revision with rows from another within a call. Separate requests do
not promise a snapshot across database refreshes.

The selected metric's extrema are checked across the **entire filtered
population before pagination**. Nonnumeric or infinite values raise
`sqlite3.DataError`, even when the offending row would be outside the requested
page; they are not silently excluded or coerced. Rankings do not compute a mean,
so large but finite source values do not require a nonoverflowing aggregate.
Connections close on success and on summary, validation, or page failure;
storage errors propagate rather than becoming successful empty rankings.

At the end of increment 5, the local suite had **324 passing tests**. Ranking coverage includes
every supported metric/direction, binary ties across pages and insertion orders,
all shared filters, literal search, empty/all-null populations, exact page/offset
limits, invalid numeric storage, concurrent snapshot consistency, and failure
cleanup. Independent Python sorting verifies complete pagination in all six
metric/direction combinations over the supplied **19,370 players**, plus a
filtered seed population. Rankable totals are **18,615** for height, **18,533** for
weight, and **19,244** for birth year. Legacy Flask responses and the search
pagination contract remain unchanged. These are local SQLite/Flask results, not
Express/Nexla, MCP, or Docker validation.

Comparisons are implemented in increment 6 below.

### Bounded player comparisons (increment 6)

`PlayerService.compare_players(player_ids)` was introduced as an internal Python
method; increment 12 now exposes it through authenticated HTTP, not a published
tool. It accepts a list or tuple of **2-5 distinct
player IDs**. Each ID must be 1-32 ASCII alphanumeric characters, matched exactly
and case-sensitively. IDs are not trimmed, normalized, or coerced. The supplied
seed's 19,370 IDs all satisfy these bounds; legacy lookup methods retain their
existing acceptance of other stored IDs.

```python
comparison = service.compare_players(["aaronto01", "aaronha01"])
```

The result has two top-level fields:

| Field | Implemented contents |
| --- | --- |
| `data` | Full stored records in the caller's requested order, retaining field names, types, and nulls. |
| `differences` | Every unordered pair, ordered by the earlier then later position in the input. Each entry contains `left_player_id`, `right_player_id`, and `numeric`. |

`numeric` always contains `birthYear`, `height`, and `weight`. Each difference is
**left value minus right value**. With input `[a, b, c]`, the pairs are `(a, b)`,
`(a, c)`, then `(b, c)`. Reversing a pair reverses the sign. A difference is null
if either source value is null; two unknown values are not a zero difference.
Zeros and fractional values remain numeric. No measurement units, age
calculations, current-season claims, or performance-quality judgments are added.
There is no pagination: a successful response contains at most five records and
ten pairwise differences.

Wrong container types raise `TypeError`; malformed IDs, duplicate input IDs, and
out-of-range cardinality raise `ValueError`, before opening a query connection.
Any absent ID raises `PlayersNotFoundError` (a `LookupError`) with an ordered
`missing_ids` tuple. No partial comparison is returned. Its message is generic;
callers can inspect the attribute without parsing an exception message. The
integration HTTP layer now maps this error to a sanitized 404 (increment 12),
without exposing missing IDs in the message or returning partial records.

The operation reuses shared read-only connection handling and reads all selected
players in **one parameterized SELECT**, so records and differences describe one
SQLite snapshot even during a concurrent WAL write. It does not perform separate
detail calls that could observe different revisions. Because the current schema
does not enforce unique IDs, the query fetches at most the requested count plus
one row (at most six); this is enough to detect duplicate selected IDs without
an unbounded fetch. Duplicate stored IDs raise `sqlite3.DataError` rather than
arbitrarily selecting one record.

All three compared numeric attributes are checked for every selected player.
Nonnumeric or infinite values raise `sqlite3.DataError`, including when the other
player's attribute is null. Finite operands can still produce an infinite
difference, so subtraction overflow also raises explicitly. Invalid values or
duplicate IDs outside the selected population do not block a comparison.
Connections close on success and on query, missing-ID, duplicate-ID, numeric
validation, or overflow failure. Storage errors propagate instead of becoming
empty successful results.

At the end of increment 6, the local suite had **444 passing tests**. Comparison coverage
includes exact cardinality/ID boundaries, input order and case, every pair,
all-null and empty populations, missing-ID details, zeros/fractions, numeric
overflow, bounded duplicate detection, concurrent snapshot consistency, and
connection cleanup. Seed-backed comparisons check full records and differences
against independent CSV expectations, including missing values. The legacy Flask,
search, statistics, and complete ranking-pagination tests still pass. These are
local SQLite/Flask results, not Express/Nexla, MCP, Docker, or live Ollama validation.

Authenticated comparison HTTP is implemented in increment 12 below.
Operational metrics, production packaging, and Express/Nexla validation remain
future increments. Comparison
cardinality limits bound result size, not query duration; optional database
deadlines are implemented in increment 7 below. Increment 6 added no environment
variables or dependencies and did not expose or publish an integration.

### Cancellable SQLite reads (increment 7)

`PlayerService` now accepts two optional, keyword-only timeout arguments. These
are internal Python configuration, **not environment variables or tool inputs**:

```python
service = PlayerService(
    "/absolute/path/to/player.db",
    busy_timeout_seconds=1,
    query_timeout_seconds=2,
)
page = service.search_players(limit=20)
```

| Argument | Contract |
| --- | --- |
| `busy_timeout_seconds` | Finite integer/float from 0 through 60. Zero disables waiting on a database lock. Default 5 preserves SQLite's previous default. |
| `query_timeout_seconds` | `None` (the legacy default, no execution deadline), or a finite integer/float from 0.001 through 60 seconds. |

Booleans, strings, nonfinite values, and out-of-range values are rejected with
`ValueError` before opening a database connection. `busy_timeout_seconds=None`
is invalid. The integration HTTP layer must explicitly enable the proposed
1-second busy wait and 2-second query budget; legacy routes remain unchanged.

Each read connection gets its own absolute monotonic deadline, starting before
the connection is opened. Multi-statement search, ranking, and statistics reads
share that deadline across their entire read transaction; executing the next
statement does not reset the budget. The constructor's schema read is also
bounded, but has a separate budget from subsequent calls. Service instances
can be reused: neither idle time nor a previous timeout consumes a later call's
budget.

SQLite's progress handler checks the deadline every 1,000 virtual-machine
instructions, including while fetching rows. Explicit checks before statements
and before completing the read reject expired results even when a short query
never reaches a progress checkpoint. Each statement's lock wait is capped at
the smaller of the configured busy timeout and the remaining read budget,
rounded down to SQLite's integer milliseconds. This matters because a progress
handler does not interrupt SQLite's busy wait, and SQLite restarts that wait
for each statement.

Deadline checks and progress-handler cancellations raise
`PlayerQueryTimeoutError`, a `TimeoutError` with the safe message
`Player database read deadline exceeded`. The original SQLite interruption is
retained as the exception cause, not returned as a successful empty result.
Lock-wait exhaustion remains a SQLite storage error, including when its wait
was shortened by the deadline; it must not be mistaken for an empty population.
Other storage/data errors retain their original types rather than being
relabeled solely because time elapsed. All five integration operations now map
deadline errors to 504 and unavailable/locked storage to 503 through the same
sanitized HTTP error boundary.

Progress handlers are removed and connections closed on success, query failure,
or cancellation. Closing an interrupted read transaction releases its locks;
subsequent reads can succeed. Read-only mode, query-only enforcement, snapshot
consistency, and existing response shapes are preserved.

These are **cooperative database-read deadlines, not hard HTTP wall-clock
limits**. SQLite progress callbacks cannot interrupt a blocked filesystem call
or arbitrary Python work. Conversion after the read context and HTTP response
serialization are outside this budget. Production ingress/WSGI request limits
remain necessary; no cancellation thread or long-running process is introduced.

At the end of increment 7, the local suite had **527 passing tests**. New coverage checks
strict configuration bounds, unchanged legacy defaults, every shared read
operation, schema/setup time, cumulative multi-statement budgets, decreasing
busy waits, short-query expiry, failure cleanup, and fresh budgets after prior
calls. Real SQLite tests interrupt CPU-heavy execution and row fetching, cancel
an expensive search transaction, verify an exclusive writer can proceed after
cancellation, and bound actual lock contention. Full-seed reads using the
proposed integration budgets match the legacy service results over all
**19,370 players**. This is local SQLite/Flask validation, not Express/Nexla,
MCP, Docker, or live Ollama validation.

Increment 7 added no HTTP routes, environment settings, authentication, or
publication. Increment 8 begins the authenticated HTTP boundary below.

### Opt-in authenticated HTTP search (increment 8)

`GET /integrations/express/v1/players` now exposes shared search and filtering
behind a dedicated bearer credential. The integration is disabled by default.
Enabled apps reject invalid settings before database initialization; legacy
`/v1/*` routes, the `app` export, and development startup behavior are preserved.
A small `create_app()` factory isolates each app's captured integration settings.

The [implemented HTTP contract and environment guide](express-api.md) documents
the four active settings, safe local-only setup, token rotation, accepted inputs,
pagination, errors, request IDs, and logging. The secret-free
[`player-service-app/.env.example`](../player-service-app/.env.example) is a
reference only, not automatically loaded by the application.

Authentication precedes routing/validation/database access throughout the
integration namespace. Search rejects unknown/duplicate parameters, malformed
UTF-8/percent encoding, invalid decimal values, oversized query strings, and
declared GET/HEAD bodies. It explicitly enables the default 1-second SQLite lock wait
and 2-second per-read budget. Missing/unusable storage returns a sanitized 503,
deadline cancellation returns 504, and unexpected failures return a logged,
sanitized 500. Nonfinite/unserializable stored values cannot become a successful
nonstandard JSON response.

Successful search returns `data`, `pagination`, and `request_id`, retaining full
legacy player records and shared pagination semantics. Safe incoming request IDs
are echoed; others are replaced by UUIDs. Response headers and structured log
messages cover success, auth failures, invalid input, and routing/storage/timeout
errors without logging raw inputs, credentials, or exception messages.

At the end of increment 8, the local suite had **721 passing tests**. HTTP coverage
includes configuration/authentication boundaries, input/size/page limits,
application-instance isolation, WSGI safe-stream compatibility, real SQLite lock and deadline failures,
connection cleanup, sanitized logs, and complete authenticated Flask test-client
pagination over **19,370 unique seed IDs**. Legacy player and mocked Ollama
success/failure responses remain compatible. These are local Flask/SQLite
results, not real network, Express/Nexla, MCP, Docker, or live Ollama validation.

This was deliberately the first HTTP operation, not the entire API gate. Detail
and statistics are implemented in increments 9 and 10 below; rankings and
comparisons follow in increments 11 and 12. The OpenAPI contract, operational
metrics, production packaging/logging, and
real network/external validation remain pending. No platform resources were
created or published.

### Authenticated player details (increment 9)

`GET /integrations/express/v1/players/{player_id}` now exposes one exact-ID
lookup through the same opt-in, authenticated boundary. The decoded ID must be
1-32 ASCII alphanumeric characters; matching is case-sensitive and no trimming
or normalization is applied. All nonempty query strings are rejected. ID and
query validation happen before constructing the service or reading its schema.
Authentication still precedes validation and routing errors.

The new `PlayerService.get_player()` shares its ID validator with comparisons
but leaves `search_by_player()` unchanged. One parameterized SELECT uses
explicit binary collation and a two-row limit: a second selected row proves
duplicate identity and raises `sqlite3.DataError`, rather than selecting the
last record as the legacy method does. No match raises `PlayersNotFoundError`
with a one-element `missing_ids` tuple. Read-only access, deadline checks, and
connection cleanup use the existing shared implementation.

HTTP success has exactly `data` (one full nullable record, not an array) and
`request_id`; there is no pagination. The shared error boundary maps missing
players to a sanitized 404 `player_not_found`, duplicate selected IDs or
unserializable data to 503, and read deadlines to 504. Search and detail share
deadline-configured service creation and strict JSON response serialization.
Logs use the detail route template instead of the requested ID, and count one
successful result or zero on failure. The [HTTP guide](express-api.md#player-details)
documents decoding, HEAD/OPTIONS, inputs, and response/error semantics.

At the end of increment 9, the local suite had **839 passing tests**, including 118 additional cases for
detail contracts and shared boundaries. Coverage checks exact ID/length/case
bounds, binary matching even with a NOCASE column, bounded duplicate detection,
missing players, authentication before reads, encoded-path validation, rejected
query/body input, configured deadlines, real SQLite cancellation/lock failures,
redacted logs, and cleanup. Seed-backed HTTP details match independently
converted CSV records, including missing measurements; search pagination and
all legacy/analytics regression tests still pass. These are local Flask
test-client and SQLite results, not network, Docker, live Ollama, or
Express/Nexla validation. No resources were deployed or published.

### Authenticated population statistics (increment 10)

`GET /integrations/express/v1/statistics` now exposes the existing aggregate
service through the opt-in authenticated boundary. It accepts all six search
filters plus `group_limit` (1-100, default 20). Shared strict query decoding and
filter construction keep search and statistics semantics aligned while enforcing
separate parameter allowlists. Statistics reject pagination and search rejects
`group_limit`; invalid inputs fail before schema inspection or query execution.
Legacy routes and response contracts remain unchanged.

Success contains `total`, `numeric`, `groups`, and `request_id`, with no top-level
`data` or pagination. Numeric and categorical summaries cover the entire matching
population from one read snapshot; missing and omitted counts remain explicit.
The group cap does not reduce the population count or numeric aggregates.
Statistics logs report the matched population as `result_count`, including zero
for an empty successful result. Every failed request reports zero results.

The route reuses configured deadlines, strict JSON serialization, safe errors,
correlation headers, and redacted logging. Invalid numeric aggregates, overflow,
unserializable returned categories, and group-query failures return sanitized
503 errors, never a partial successful summary. Cancellations return 504.
The [HTTP contract](express-api.md#population-statistics) documents accepted inputs,
output shape, null/truncation semantics, HEAD/OPTIONS, and connector-binding
considerations; search's `$.data` extraction is not suitable for this envelope.

At the end of increment 10, the local suite had **970 passing tests**, including
131 additional cases for statistics and shared boundaries. Coverage checks each filter, full
population counts, default/minimum/maximum group limits over 101 categories,
nulls, empty results, strict parsing before reads, disabled/authenticated routes,
redacted logs, real SQLite lock/deadline failures, failure cleanup, and independent
CSV-derived aggregates over all **19,370 players** and a filtered population.
The independent statistics expectation helper is shared by service and HTTP
tests; no service result is used as the expected HTTP aggregate. These are local
Flask test-client/SQLite results, not network, Docker, live Ollama, or Express/Nexla
validation. Rankings and comparisons HTTP are implemented in increments 11 and
12 below; OpenAPI, metrics, packaging, and external validation remain pending.
No resources were deployed or published.

### Authenticated numeric rankings (increment 11)

`GET /integrations/express/v1/rankings` now exposes the existing ranking service
through the opt-in authenticated boundary. `metric` is required and exactly
`height`, `weight`, or `birthYear`; `direction` is `asc` or `desc`, defaulting to
`desc`. All six shared filters and search's bounded `limit`/`offset` pagination
are supported. Only these ten scalar parameters are accepted.

The HTTP and service layers share the ranking enum validator and fixed SQL
identifier maps. Search and rankings share HTTP pagination parsing. Invalid
enums, filters, pages, duplicate/unknown parameters, and malformed encoding fail
before constructing the service or reading its schema. Business query semantics
and legacy behavior are unchanged.

Success contains `metric`, `direction`, `excluded_count`, `data`, `pagination`,
and `request_id`. Full nullable records retain ascending binary ID tie-breaks
in both directions. Totals count rankable players; exclusions count null metrics
across the entire filtered population, independently of the page. Errors,
deadlines, strict serialization, and correlation use the shared HTTP boundary.
Invalid metric values anywhere within the filters, including off-page rows,
produce sanitized 503 errors; invalid values outside the filters do not block
valid results. Logs count returned page rows, not rankable totals or exclusions.
The [HTTP contract](express-api.md#numeric-rankings) documents these distinctions
and the metadata that generated connector/tool bindings must retain.

At the end of increment 11, the local suite had **1155 passing tests**, including
185 additional cases for rankings and shared boundaries. Coverage checks required/case-sensitive
enums, all ten inputs together, literal filters, exact page/offset bounds, binary
ties, empty/all-null populations, null exclusions, off-page invalid storage,
page/serialization failures, authentication before reads, configured deadlines,
real SQLite cancellation/lock failures, cleanup, and redacted logs.
An independent ranking expectation helper is shared by service and HTTP tests.
Complete authenticated Flask test-client pagination in all six metric/direction
combinations matches CSV-derived full records over the **19,370-player** seed:
18,615 rankable heights, 18,533 weights, and 19,244 birth years. Filtered seed
rankings and all legacy/analytics regression tests also pass.
These are local Flask/SQLite results, not network, Docker, live Ollama, or
Express/Nexla validation. Comparisons HTTP is implemented in increment 12 below;
OpenAPI, metrics, packaging, and external validation remain pending.
No resources were deployed or published.

### Authenticated player comparisons (increment 12)

`GET /integrations/express/v1/comparisons` now exposes the existing comparison
service through the opt-in authenticated boundary. Its only query parameter,
`player_ids`, is required exactly once and contains 2-5 distinct, comma-separated
IDs. Each ID is 1-32 ASCII alphanumeric characters, matched case-sensitively.
Percent encoding is decoded once before splitting; input order is preserved.
Repeated keys, empty elements, whitespace, alternate separators, duplicate IDs,
filters, pagination, and unknown parameters are rejected.

The HTTP and service layers share the comparison validator. Invalid inputs
therefore fail before constructing the service or inspecting its schema without
changing the internal list/tuple contract or legacy lookup behavior.
Success contains `data`, `differences`, and `request_id`, with full nullable
records in input order and every pair's left-minus-right numeric differences.
There is no pagination: at most five records and ten pairs are returned.

One bounded, parameterized SELECT preserves the existing read snapshot and
duplicate-detection behavior. The shared boundary supplies authentication,
configured deadlines, strict JSON serialization, safe errors, and correlation.
Any missing player returns a sanitized 404, never partial records. Duplicate
selected IDs, invalid stored numeric/JSON values, and overflowing differences
return 503; cancellations return 504. Logs count returned players, not pairs,
and report zero on failure without logging requested IDs or player records.
The [HTTP contract](express-api.md#player-comparisons) documents list encoding,
null/sign semantics, errors, HEAD/OPTIONS, and connector-binding requirements.

The complete local suite has **1324 passing tests**, including 169 additional
cases for comparisons and shared boundaries. Coverage checks exact list/ID bounds
and case, order and all pairs, nulls/zeros/fractions, malformed and unsupported
queries before database access, missing records, bounded duplicate detection,
invalid numeric values and subtraction overflow, query/serialization failures,
configured deadlines, real SQLite cancellation/lock failures, cleanup, and
redacted logs. Service and HTTP seed comparisons share independent CSV-derived
expectations for full records and all ten differences across five players,
including nullable measurements. All legacy/analytics regression tests pass.
These are local Flask test-client/SQLite results, not network, Docker, live
Ollama, or Express/Nexla validation. OpenAPI, metrics, packaging, and external
validation remain pending. No resources were deployed or published.

## 2. Verified Express.dev / Nexla mechanisms

The following are supported by official documentation retrieved on the assessment
date, **not by a successful authenticated integration in this repository**.

| Mechanism | Documented support | Boundary |
| --- | --- | --- |
| Express conversations | [Express guides][express] describe building data flows and reusing credentials, Nexsets, and other account resources, including `@` references. | This is not documentation for an arbitrary public Express chat REST API. |
| Universal REST connector | [Credential documentation][rest-auth] supports Generic REST API credentials, including API keys in headers. [Source configuration][rest-source] documents methods, URLs, response-data JSON paths, and request headers. | Do not connect it to the unbounded, unauthenticated legacy endpoints. Exact pagination settings must be verified for the selected flow and tenant. |
| Express MCP Studio | [MCP Studio][studio] is available inside Express. It probes connected systems, proposes scoped tools, and builds a governed MCP server. Its tools become callable in that Studio chat on the next turn after the build. | Review the generated contracts and source bindings. Availability and discovery of this custom REST API have not been tested in an authenticated tenant. |
| Nexset-backed tools | The [Tools API][tools-api] documents `POST /v1/tools:from_nexset`, schema-derived definitions, lifecycle management, and execution. [Tool management][tools-ui] says generated schemas/bindings are not manually editable in that UI. | The mention of `custom_tool` is not a documented arbitrary tool-registration payload. Do not invent one, or assume OpenAPI import is supported. |
| External MCP servers | The [Gateway guide][gateway] and [Gateway API][gateway-api] document registering an external server, credential storage, preview/sync, and include/exclude filters. | The remote server must support **Streamable HTTP** and be reachable by Nexla. A local stdio server alone is insufficient. |
| Managed MCP serving | The [MCP protocol guide][mcp] documents initialization, discovery, execution, and ToolSet endpoints under `https://api-genai.nexla.io`. | Gateway tools may be discovered through `search_tools`, `describe_tool`, and `call_tool` [meta-tools][meta-tools], not an exhaustive `tools/list`. |
| Identity and governance | [MCP authentication][mcp-auth] supports Google/Microsoft OAuth and service keys. Calls act as the signed-in user or key owner. [API authentication][api-auth] describes organization scoping and Nexset roles. [Receipts][receipts] and [rate limits][rate-limits] document audit and execution controls. | A Nexla service key and the Player Service's own credential are different secrets. Do not assume a shared upstream credential conveys individual end-user identity to Flask. |

For REST credentials, a documented way to send the proposed application bearer
token is **API Key**, location **HEADER**, parameter name **Authorization**, and
value **Bearer <application-token>**. Put the value in Nexla's credential form,
never in chat, a tool argument, a query parameter, or a committed file. Keep TLS
certificate verification enabled.

No undocumented `/chat` automation, MCP registration endpoint on express.dev,
or import format is needed. The OpenAPI document proposed below is our API
contract; it must not be presented as a verified Express import mechanism.

## 3. Architecture decision

**Use a REST-first hybrid: additive Flask read APIs, with Nexla-managed MCP tools
in Express MCP Studio.** Keep business queries in Player Service. Prefer managed
MCP serving over maintaining a second application server without demonstrated
need.

| Approach | Benefits | Costs / decision |
| --- | --- | --- |
| REST connector only | Fits Flask; documented credential storage and data-flow integration; useful for governed ingestion. | Ingested Nexsets may be snapshots, not live query tools. Ingestion alone does not demonstrate all interactive operations. Retain as the data-flow path, not the entire chat solution. |
| Custom MCP only | Explicit typed discovery and invocation; documented Nexla Gateway registration. | Additional server, dependencies, transport/authentication lifecycle, and hosting. Direct attachment of any external server to the requested Express chat session is not established by the reviewed guides. Not the first implementation. |
| REST plus managed MCP | Reuses the application, provides a testable HTTP contract, and uses the documented Studio build/chat workflow for governed tools. | Tenant probing must confirm parameter binding, freshness, pagination, and all required operations. Selected target. |

```text
Express MCP Studio / Nexla governed tools
    -> Generic REST API credential and connector
    -> approved HTTPS ingress (integration routes only)
    -> new authenticated Flask integration blueprint
    -> shared, bounded PlayerService query methods
    -> private SQLite file

Existing private clients -> unchanged /v1/* routes
Existing /v1/chat*       -> Ollama (not used by player tools)
```

If Studio cannot express the exact live contracts, the documented fallback is a
thin Streamable HTTP MCP adapter registered through Nexla Gateway. It should call
the same bounded REST operations, not implement another query engine or expose
SQLite. Implement that adapter only after establishing the need. Test it with an
official MCP SDK client, including initialization, discovery, calls, transport
errors, and authentication; do not hand-roll a partial JSON-RPC server. Confirm
how the selected ToolSet becomes available in the target Express session rather
than assuming Gateway registration alone completes that step.

Neither path permits arbitrary SQL, arbitrary outbound URLs, mutations,
recommendation feedback, or Ollama chat as an integration tool.

## 4. Application/tool contract (partially implemented)

All five read-only HTTP operations are implemented; the OpenAPI contract remains
**planned**, not implemented.
Internal search/filter, statistics, ranking, and comparison semantics are
implemented as described in increments 3 through 6.
The opt-in blueprint uses `/integrations/express/v1`. The proposed operation
names are also the desired tool names; Studio's actual generated names and
schemas must be inspected rather than assumed to match automatically.

| Operation | GET route / status | Supported semantics |
| --- | --- | --- |
| `search_players` | `/players` (**implemented**) | Search IDs/names and filter by birth country, inclusive birth-year range, bats, and throws. Search and filtering share one query implementation. |
| `get_player` | `/players/{player_id}` (**implemented**) | Retrieve a single full record by exact, case-sensitive ID; return 404 if absent, or 503 if selected stored IDs are duplicated. No query parameters or pagination. |
| `get_player_statistics` | `/statistics` (**implemented**) | Count the entire filtered population; bounded groups by birth country/bats/throws; count/min/max/mean for birth year, height, and weight. Include known/missing/omitted counts. Accept shared filters and `group_limit` (1-100, default 20), not pagination. |
| `rank_players` | `/rankings` (**implemented**) | Require `metric` as `height`, `weight`, or `birthYear`; accept shared filters and bounded pagination, with `asc`/`desc` direction (default `desc`) and ascending player-ID tie-breaks. Exclude null metrics and report `excluded_count`; pagination totals count rankable players. No performance-quality claim. |
| `compare_players` | `/comparisons` (**implemented**) | Require one comma-separated `player_ids` parameter containing 2-5 distinct IDs. Return full records in supplied order and all pairwise left-minus-right differences for birth year, height, and weight. Differences are null if either value is unknown; any missing ID returns 404. No filters or pagination. |

This covers all six requested capabilities with five operations: filtering is a
first-class input to search and aggregate queries, not a duplicated endpoint.
Only fixed, allowlisted columns/operations may participate in queries.

### Validation and pagination

- Comparisons and integration details share validation of IDs as 1-32 ASCII
  alphanumeric characters. Supplied IDs are at most 9 characters. Keep legacy
  lookups unchanged. Accept bounded Unicode
  name/country text as data, not SQL fragments; search case matching and literal
  `%`/`_` behavior are already defined and tested.
- Validate handedness against actual values, year bounds/order, ranking enums,
  and integer page parameters. Reject unknown parameters, ambiguous duplicate
  scalar parameters, malformed values, and over-limit input; do not silently
  clamp or substitute defaults for invalid input.
- Page limits already enforced by the internal search method: default 20,
  maximum 100; offset 0-100000. Search pages are ordered by `playerId`; rankings
  already use `playerId` as the deterministic tie-breaker.
  Return `data` plus `pagination.limit`, `offset`, `total`, and nullable
  `next_offset`. Compute totals using the same filters, not from the page length.
- Aggregates apply to the entire matching population, not just a page. Report
  empty populations and null measurements explicitly; never turn unknown into
  zero or generate a NaN JSON value.
- Offset pagination is adequate for the small, static dataset. Full-scan
  validation must use one database revision; do not promise snapshot consistency
  across a later data refresh.
- Publish and test an OpenAPI contract for these new routes, including enums,
  bounds, nullability, envelopes, errors, and authentication. Preserve the legacy
  bare-array/detail responses and do not add mandatory authentication to `/v1/*`.

### Authentication, resource limits, and error handling

- Disable the integration by default. When enabled, require a dedicated,
  high-entropy application token from environment/secret storage and fail startup
  on invalid configuration. Authenticate before querying; use constant-time
  secret comparison. Document rotation and revocation without logging tokens.
- Require HTTPS at approved ingress. Allowlist only the new integration routes
  for Nexla; keep legacy routes, metrics, SQLite, Ollama, and the model service
  private. Application-side read-only enforcement remains necessary even if an
  agent or generated tool proposes a write.
- Use parameterized SQLite queries and fixed maps for identifiers/sort directions.
  Explicitly close connections; integration reads use SQLite read-only mode and
  query-only enforcement. A missing database must fail clearly, not create an
  empty database during a tool call.
- The service now supports bounded busy waits and monotonic query deadlines
  (increment 7). All five integration operations opt into the default 1-second
  busy wait and 2-second read budget through the same boundary.
  Legacy defaults are unchanged. A connection
  timeout alone does **not** limit query execution. Bound request size and
  comparison cardinality, and retain separate HTTP request limits.
- If an HTTP MCP adapter is needed, configure one fixed upstream service origin,
  verify TLS, disable redirects, and enforce connect/read plus total execution
  deadlines. Keep separate inbound and upstream credentials; do not pass through
  arbitrary user authorization. Bound any read retries within the total deadline.
- Use a stable JSON error envelope containing a machine code, safe message, and
  request ID. Distinguish invalid input (400), authentication (401), policy denial
  where applicable (403), absent records (404), rate limiting (429), unavailable
  storage (503), and deadline expiry (504). Unexpected failures return a logged,
  sanitized 500, never an empty success.
- Enforce abuse/rate controls at the approved ingress as well as Nexla's documented
  per-principal limits; honor `Retry-After`. No numeric Nexla quota is assumed.
  Grant access only to the intended player resources/tools, not broad credentials.

### Correlation and operations

Accept a bounded, safe `X-Request-ID` or generate a UUID; return it in the header
and response envelope, including failures. Emit structured JSON logs with
timestamp, operation/route template, status, duration, request ID, and result
count. Do not log authorization values, raw query/body data, player records,
SQL text, or user-controlled strings as unescaped log fields.

Add request/error/auth-failure/timeout counters and a latency histogram with
bounded operation/status labels. Protect the metrics surface and keep health
responses free of configuration details. Do not label metrics with player IDs,
request IDs, or search text. Verify the metrics deployment mode matches the
chosen WSGI worker model.

Capture Nexla receipt IDs during external validation alongside application
request IDs. Whether Nexla forwards correlation headers/trace IDs is a tenant
validation item, not an assumed identity between these fields.

## 5. Packaging and configuration plan

Add a Player Service container, without repurposing the model image: compatible
Python runtime, production WSGI entry point, debug off, non-root execution, and
health checks. Initialize/validate the database in an explicit controlled step
before serving read-only integration traffic; test a fresh installation and an
existing database. Preserve the development `app` export and default file path.
Any optional Compose services should bind local ports to loopback and keep Ollama
optional. Do not run deployment or public tunneling commands automatically.

Configuration status (see the [implemented settings guide](express-api.md#configuration)
for exact validation and scope):

| Setting | Purpose / default |
| --- | --- |
| `PLAYER_INTEGRATION_ENABLED` | **Implemented:** opt-in integration, exactly `true`/`false`, false by default. |
| `PLAYER_INTEGRATION_TOKEN` | **Implemented:** dedicated secret, no default; required when enabled. Secret-file injection for containers remains pending. |
| `PLAYER_DB_PATH` | **Pending:** operator-controlled database path; default remains the existing application-relative path. Never a tool argument. |
| `PLAYER_DB_BUSY_TIMEOUT_SECONDS` | **Implemented:** integration-only bounded lock wait, default 1, range 0-60 seconds. |
| `PLAYER_QUERY_TIMEOUT_SECONDS` | **Implemented:** integration-only per-read budget, default 2, range 0.001-60 seconds; cannot be disabled when enabled. |

A secret-free environment example and implemented settings are documented in
increment 8; extend them when adding packaging settings.
Keep page/argument caps in the versioned contract rather than introducing
unnecessary configuration. A Nexla service key is an operator/platform
credential, not a required Flask runtime secret. Do not silently raise the
advertised Python 3.9+ requirement; isolate any optional MCP runtime requirements
or document and validate a deliberate compatibility decision.

## 6. Incremental implementation and acceptance gates

| Increment | Individually verifiable completion gate |
| --- | --- |
| Assessment (this document) | Repository/data inventory, official-source decision, and existing pytest baseline recorded. No runtime behavior changed. |
| Safe shared data access (**complete**) | Parameterized player/country lookups, deterministic no-match behavior, read-only connections with explicit cleanup, and temporary-database tests. Existing list/detail response contracts retained; regression tests cover quote-containing input and intended legacy missing-ID error behavior. |
| Bounded player search/filter (**complete**) | Validated shared filters, deterministic pages and snapshot-consistent totals, null/switch-handedness coverage, exact input/page boundaries, and a complete local 19,370-ID seed traversal. Legacy routes unchanged. |
| Filtered population statistics (**complete**) | Full-population numeric summaries and bounded categorical groups reuse shared filters; explicit missing/omitted counts, one read snapshot, finite-output checks, failure cleanup, and independent full-seed expectations. |
| Bounded numeric rankings (**complete**) | Shared filters and pagination, allowlisted metrics/directions, binary player-ID ties, explicit null exclusions, full-population finite-metric validation, snapshot consistency, failure cleanup, and complete seed traversal in all six metric/direction combinations. |
| Player comparisons (**complete**) | Ordered comparisons of 2-5 distinct validated IDs; explicit missing/duplicate stored-ID errors; full nullable records and finite pairwise differences from one bounded query snapshot; fixture/seed expectations, concurrency, and cleanup tests. |
| Cancellable SQLite reads (**complete**) | Validated opt-in query budgets and busy waits; monotonic deadlines shared across read statements; progress cancellation and short-query checks; real CPU/lock contention, transaction cleanup, full-seed compatibility, and unchanged legacy defaults. |
| Authenticated search/filter HTTP (**complete**) | Opt-in route and fail-closed settings, strict header/query/body handling before reads, configured deadlines, safe errors, request IDs/log messages, full-seed HTTP test-client pagination, and legacy/Ollama compatibility (Ollama mocked). |
| Authenticated player-detail HTTP (**complete**) | Shared strict ID validation, exact binary identity, one bounded read with duplicate detection, safe missing-player 404, shared authentication/deadlines/serialization/logging, seed-backed records, and unchanged legacy lookup contracts. |
| Authenticated population-statistics HTTP (**complete**) | Shared strict filter parsing and separate parameter allowlists, bounded groups over full-population aggregates, explicit null/omitted counts, safe serialization/failure mapping, configured deadlines, correlated/redacted logs, and independent fixture/seed expectations. |
| Authenticated numeric-ranking HTTP (**complete**) | Shared enum/filter/page validation before database access, bounded pages with stable ties and explicit exclusions, safe errors/serialization, configured deadlines, redacted logs, and complete HTTP seed pagination in all six metric/direction combinations against independent CSV expectations. |
| Authenticated player-comparison HTTP (**complete**) | Shared cardinality/ID/uniqueness validation before database access, one ordered comma-separated ID parameter, bounded read and pairwise output, safe missing/duplicate/overflow errors, configured deadlines, redacted logs, and independent fixture/seed expectations. |
| Integration API schema | Publish/test the OpenAPI contract for all five operations, including list serialization, nullable output, malformed inputs, unavailable/locked storage, and missing records, without changing legacy routes. |
| Operational observability | Extend the implemented correlation IDs/redacted request log messages with production logging configuration, bounded metrics labels, counters, and latency tests. Include timeout/error paths, not just happy paths. |
| Packaging and local integration | Production container and environment/runbook documentation; real loopback HTTP smoke tests, fresh/existing DB startup, disabled/misconfigured integration, and read-only mounts. Run the entire existing and new suite. Build/test only locally, stop all started processes. |
| Express/Nexla validation | Operator-approved, authenticated Studio/connector setup; review generated contracts and call every capability, inspect receipts, and compare with local expected results. If a custom MCP adapter is necessary, add and verify it before claiming this gate. Otherwise document exact outstanding external blockers. |

Every implementation increment must update the relevant documentation and run
its focused tests plus the existing regression suite. No application formatter or
linter is currently configured. Live Ollama or
model-container tests are not prerequisites for deterministic player queries;
mocked chat tests are not evidence of live model integration.

## 7. Operator setup and end-to-end validation plan

These are future, approval-gated actions, not actions completed by this assessment.

1. Establish an approved HTTPS route reachable from Nexla, with the integration
   enabled and its separate credential securely provisioned. Do not expose the
   SQLite file, debug server, legacy routes, or local model ports.
2. In Nexla, create/select a **Generic REST API** credential using the header
   configuration above. Validate against an authenticated, small read such as
   `/integrations/express/v1/players?limit=1`, not a public health endpoint.
3. In Express **MCP Studio**, describe the five bounded operations and connect
   that credential through **Select Credentials**. Have the probe identify the
   endpoints. Require read-only methods, supported-field analytics, nullable
   values, and the documented limits. Review the proposed tools and bindings.
   Record a blocked source honestly; do not substitute fabricated player data.
4. Only after explicit publication approval, use **Build**. Inspect **Server
   ready**, the active tool count, and **Integration config**. On the next chat
   turn, verify the server appears under **MCPs** and inspect actual tool calls,
   not just the prose answer. This is the documented Studio workflow [source][studio].
5. Test exact player lookup; a name search with two pages; country/year/handedness
   filtering; full-population statistics with missing values; ranked ties and
   null exclusions; and a two-player comparison. Compare structured responses
   against locally computed expectations for the same database revision.
6. Exercise absent IDs, invalid/oversized parameters, unauthorized/revoked
   credentials, denied access, timeouts, and unavailable storage. Check local
   request IDs/logs/metrics and Nexla receipts without saving secrets. Confirm
   paused/revoked access actually prevents subsequent execution.
7. For REST ingestion, explicitly configure and verify the connector's
   pagination/response extraction in the selected flow. The proposed row path is
   `$.data`; keep pagination metadata available to the pagination mechanism.
   A complete scan of the supplied seed must produce **19,370 unique IDs** with
   no duplicates/missing pages. Do not assume that Nexla's own API-list
   `limit`/`offset` settings configure source pagination.
8. Record whether each tool queries live REST data or reads an ingested snapshot,
   the refresh policy, actual tool/schema identifiers, request/receipt evidence,
   and pass/fail/blocked status. Generated descriptions must not promise live
   data when the binding reads a snapshot. Clean up local processes and preserve
   only sanitized evidence.

### Current external blockers and unverified items

| Item | Current evidence | Needed before claiming external success |
| --- | --- | --- |
| Express/Nexla access | Public documentation was reachable; `https://www.express.dev/chat` redirected to login. No authorized tenant session or platform credential was supplied for this iteration. | Authorized operator login and confirmation of MCP Studio/connector access. Do not search local files for credentials. |
| Connectivity / deployment | No approved remotely reachable service URL or infrastructure/publication approval was supplied. | Approved hosting/network path and explicit approval before deploying or publishing. `localhost` on this machine is not a hosted Nexla source. |
| Custom REST tool generation | Studio documents connector-backed tool creation, but this API has not been probed. | Confirm exact parameter binding, schemas, live-vs-snapshot semantics, and all five operations. Use the documented Gateway fallback only if necessary. |
| Pagination / correlation | No tenant-level source pagination configuration or correlation propagation was exercised. | Demonstrate a complete bounded scan and observable request/receipt linkage using real calls. |
| Data interpretation | Repository lacks authoritative dataset provenance, measurement-unit documentation, and refresh policy. | Confirm those facts or explicitly expose values as source-defined units and a historical snapshot; never claim current/performance statistics. |

**End-to-end integration is not complete.** The baseline and increment-2/3/4/5/6/7/8/9/10/11/12 test
results above are local Flask/SQLite coverage, not an Express, Nexla, MCP, Docker,
or live Ollama integration success. Implementation work remains in addition to
the external items; documenting blockers does not make the implementation complete.

## Official references

All links below were reviewed on 2026-11-02. UI details may change; verify the
authenticated tenant before turning this plan into an operational runbook.

[express]: https://docs.nexla.com/user-guides/express/getting-started-with-express
[rest-auth]: https://docs.nexla.com/user-guides/connectors/rest_api
[rest-source]: https://docs.nexla.com/user-guides/connectors/rest-api/connect-to-api
[studio]: https://docs.nexla.com/user-guides/mcp-tools/mcp-studio
[tools-api]: https://docs.nexla.com/dev-guides/mcp-tools/tools-api
[tools-ui]: https://docs.nexla.com/user-guides/mcp-tools/tools/create-manage-tools
[gateway]: https://docs.nexla.com/user-guides/mcp-tools/gateway/register-external-servers
[gateway-api]: https://docs.nexla.com/dev-guides/mcp-tools/gateway-api
[mcp]: https://docs.nexla.com/dev-guides/mcp-tools/mcp-protocol
[meta-tools]: https://docs.nexla.com/dev-guides/mcp-tools/gateway-meta-tools
[mcp-auth]: https://docs.nexla.com/user-guides/mcp-tools/mcp-servers/authentication
[api-auth]: https://docs.nexla.com/dev-guides/mcp-tools/authentication
[receipts]: https://docs.nexla.com/dev-guides/mcp-tools/receipts-api
[rate-limits]: https://docs.nexla.com/dev-guides/mcp-tools/rate-limiting
