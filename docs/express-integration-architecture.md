# Express.dev integration: architecture and implementation plan

Assessment date: 2026-11-02.

**Status: design only.** No integration endpoints, authentication, observability,
or MCP server have been implemented in this iteration. No infrastructure was
deployed and no Nexla resources were created. This document distinguishes the
existing application, verified platform capabilities, and proposed changes.

## 1. Repository assessment

### Application and API contracts

The application is a small Flask service in
[`player-service-app/app.py`](../player-service-app/app.py), not the separate
recommendation service in `player-service-model`.

| Existing surface | Actual behavior | Integration implication |
| --- | --- | --- |
| `GET /v1/players` | Returns a bare JSON array of every player through `PlayerService.get_all_players()`. No search, filters, sorting contract, or pagination. | Preserve this response; add a separate bounded integration API. |
| `GET /v1/players/<player_id>` | Returns a single player dictionary. The route intends to return an error object with HTTP 200 when absent, but `search_by_player()` references an unassigned `dic` for no matches. | Repair the lookup with regression tests, preserve successful legacy responses, and define a separate 404 contract for new tools. |
| `GET /v1/chat/list-models` | Calls `ollama.list()`; exceptions produce a JSON error with HTTP 503. | Keep independent of the integration. |
| `POST /v1/chat` | Calls TinyLlama with the hard-coded question "Why is the sky blue?"; it does not use a client prompt or query player data. Exceptions produce HTTP 503. | This is not an existing player-question-answering engine. Do not route player tools through it. |
| `PlayerService.search_by_country()` | Unrouted service method returning raw SQLite rows. | Reuse the country-filter concept, not its raw output as a public tool contract. |

[`player_service.py`](../player-service-app/player_service.py) currently
interpolates player IDs and countries into SQL and does not explicitly close its
connections. These are prerequisites to address before exposing new tools.
There is no application authentication, validation layer, request correlation,
structured logging, operational metrics, or explicit request/query deadline.
The development entry point binds port 8000 on all interfaces with debug enabled.
These observations come from source inspection; this assessment did not execute
live Ollama requests or the missing-player error path.

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

The only tracked automated test is
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
environment can run the same suite. The current test imports the application and
creates the ignored database when absent; future query tests must use isolated
temporary databases instead.

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

## 4. Proposed application/tool contract

Everything in this section is **to be implemented**, not an existing route.
Use an opt-in blueprint under `/integrations/express/v1`. The proposed operation
names are also the desired tool names; Studio's actual generated names and
schemas must be inspected rather than assumed to match automatically.

| Operation | Proposed GET route | Supported semantics |
| --- | --- | --- |
| `search_players` | `/players` | Search IDs/names and filter by birth country, inclusive birth-year range, bats, and throws. Search and filtering share one query implementation. |
| `get_player` | `/players/{player_id}` | Retrieve a single record by exact ID; new API returns 404 if absent. |
| `get_player_statistics` | `/statistics` | Count the filtered population; bounded groups by birth country/bats/throws; count/min/max/mean for birth year, height, and weight. Include known/missing counts. |
| `rank_players` | `/rankings` | Order by the allowlisted `height`, `weight`, or `birthYear`, with explicit direction and player-ID tie-breaks. Exclude null metric values and report exclusions. No performance-quality claim. |
| `compare_players` | `/comparisons` | Compare 2-5 distinct player IDs in supplied order, showing stored attributes and numeric differences where both values are known. Any missing ID is an explicit error, not a silently incomplete comparison. |

This covers all six requested capabilities with five operations: filtering is a
first-class input to search and aggregate queries, not a duplicated endpoint.
Only fixed, allowlisted columns/operations may participate in queries.

### Validation and pagination

- Validate IDs as bounded alphanumeric strings (proposed maximum 32 characters;
  supplied IDs are at most 9). Accept bounded Unicode name/country text, not SQL
  fragments; define and test search case matching and literal `%`/`_` behavior.
- Validate handedness against actual values, year bounds/order, ranking enums,
  and integer page parameters. Reject unknown parameters, ambiguous duplicate
  scalar parameters, malformed values, and over-limit input; do not silently
  clamp or substitute defaults for invalid input.
- Proposed page limits: default 20, maximum 100; offset 0-100000. Order search
  pages by `playerId`; add `playerId` as the deterministic ranking tie-breaker.
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
- Proposed limits: 1-second SQLite busy wait and 2-second query execution budget,
  enforced with a monotonic deadline/progress handler. A connection timeout alone
  does **not** limit query execution. Bound request size and comparison cardinality.
  Verify cancellation releases resources.
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

Proposed configuration names below are **not currently read by the application**:

| Proposed setting | Purpose / default |
| --- | --- |
| `PLAYER_INTEGRATION_ENABLED` | Opt-in integration, false by default. |
| `PLAYER_INTEGRATION_TOKEN` | Dedicated secret, no default; required when enabled. Document secret-file injection for containers without embedding it in an image. |
| `PLAYER_DB_PATH` | Operator-controlled database path; default remains the existing application-relative path. Never a tool argument. |
| `PLAYER_DB_BUSY_TIMEOUT_SECONDS` | Bounded lock wait, proposed default 1. |
| `PLAYER_QUERY_TIMEOUT_SECONDS` | Execution budget, proposed default 2; reject invalid/nonfinite configuration. |

Add a secret-free environment example and document settings when implementing
them. Keep page/argument caps in the versioned contract rather than introducing
unnecessary configuration. A Nexla service key is an operator/platform
credential, not a required Flask runtime secret. Do not silently raise the
advertised Python 3.9+ requirement; isolate any optional MCP runtime requirements
or document and validate a deliberate compatibility decision.

## 6. Incremental implementation and acceptance gates

| Increment | Individually verifiable completion gate |
| --- | --- |
| Assessment (this document) | Repository/data inventory, official-source decision, and existing pytest baseline recorded. No runtime behavior changed. |
| Safe shared data access (**next smallest unit**) | Parameterized player/country lookups, deterministic no-match behavior, explicit connection cleanup, and temporary-database tests. Existing list/detail response contracts retained; regression tests cover quote-containing input and intended legacy missing-ID error behavior. |
| Bounded player query capabilities | Fixture-backed tests for search/filter, full-population aggregates, nulls, switch handedness, stable rankings/ties, comparisons, and exact pagination boundaries. Shared query methods, not duplicated business logic. |
| Authenticated integration API and schema | Opt-in routes, fail-closed configuration, strict validation, deadlines, safe errors, and OpenAPI contract tests. Flask test-client tests cover all operations, unauthorized calls, malformed inputs, unavailable/locked storage, and unchanged legacy/Ollama routes (Ollama mocked). |
| Observability | Correlation IDs across success/failure, redacted structured logs, bounded metrics labels, and counter/latency tests. Include timeout/error paths, not just happy paths. |
| Packaging and local integration | Production container and environment/runbook documentation; real loopback HTTP smoke tests, fresh/existing DB startup, disabled/misconfigured integration, and read-only mounts. Run the entire existing and new suite. Build/test only locally, stop all started processes. |
| Express/Nexla validation | Operator-approved, authenticated Studio/connector setup; review generated contracts and call every capability, inspect receipts, and compare with local expected results. If a custom MCP adapter is necessary, add and verify it before claiming this gate. Otherwise document exact outstanding external blockers. |

Every implementation increment must update the relevant documentation and run
its focused tests plus the existing regression suite. No formatter/linter
installation is needed just for this documentation increment. Live Ollama or
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

**End-to-end integration is not complete.** The baseline test result above is
only local Flask coverage, not an Express, Nexla, MCP, Docker, or live Ollama
integration success. Implementation work remains in addition to the external
items; documenting blockers does not make the implementation complete.

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
