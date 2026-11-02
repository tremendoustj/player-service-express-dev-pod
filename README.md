# ⚾ Player Service

Player Service is a backend application that serves baseball player data. In addition, Player service integrates with [Ollama](https://github.com/ollama/ollama/blob/main/docs/api.md), which allows us to run the [tinyllama LLM]((https://ollama.com/library/tinyllama)) locally.

## Express.dev integration (in progress)

The [architecture assessment and implementation plan](docs/express-integration-architecture.md)
evaluates REST and MCP integration with Express.dev/Nexla, defines the proposed
read-only player tools, and records the test baseline and external validation
requirements. Shared SQLite reads are now parameterized, read-only, and explicitly
closed, with isolated regression coverage and deterministic missing-player handling.
The shared service also supports validated name/ID search, country/year/handedness
filters, and bounded, deterministic pagination with snapshot-consistent totals.
Filtered population statistics include known/missing measurement counts and
bounded categorical summaries from a consistent snapshot. Numeric rankings
support height, weight, and birth year with stable ties, bounded pages, and
explicit missing-value exclusions. Ordered comparisons support 2-5 distinct IDs,
null-aware numeric differences, and explicit missing-player errors.
Shared reads can opt into validated SQLite deadlines and bounded lock waits;
cancellation closes connections without changing legacy timeout defaults.
An [opt-in authenticated read API](docs/express-api.md) now exposes search/filter
at `GET /integrations/express/v1/players` and exact-ID details at
`GET /integrations/express/v1/players/{player_id}`, with strict validation, bounded
reads, correlation IDs, sanitized errors, and structured request log messages.
Details return one full record, a 404 for an absent ID, or a storage error for
duplicate stored IDs; legacy detail behavior is unchanged.
`GET /integrations/express/v1/statistics` exposes full-population summaries
through the same boundary, with shared filters and a bounded `group_limit`.
It reports null measurements and omitted groups explicitly, without pagination
or performance-statistics claims.
`GET /integrations/express/v1/rankings` exposes numeric rankings with a required
`metric` (`height`, `weight`, or `birthYear`), shared filters, bounded pagination,
and explicit null exclusions. Both sort directions retain ascending ID tie-breaks.
It is disabled by default; see the linked configuration and local-only setup.
The comparison HTTP endpoint, the OpenAPI contract, metrics, packaging, and
Express/Nexla validation are still pending. Legacy API response formats are
unchanged.

## Dependencies

- Python3.9+
- sqllite3
- Docker
- [Ollama Python SDK](https://github.com/ollama/ollama-python)

## 🛠️ Setup Instructions

1. Verify system dependencies
   1. Python
      - Verify installation: `python3 --version`
   3. Container Manager
      - Download and install from [docker.com](https://www.docker.com/)(recommended) or [podman](https://podman.io/) (alternative)
      - Verify installation, run: `docker --version` for docker

2. Clone this repository or Download the code as zip
   - run `git clone https://github.com/Intuit-A4A/backend-python-player-service.git`

## Run the application

### Part 1: Application Dependencies

*OPTIONAL* Create & activate virtual env
```shell
   $ python3 -m venv env # use `python -m venv env` on Windows
   $ source env/bin/activate  # use `env\Scripts\activate` on Windows
```

1. Install application dependencies
    - Move into the application directory: `cd player-service-app`.
    - Create and activate a virtual environment (recommended on macOS/Linux with system Python):

    ```shell
    python3 -m venv .venv
    source .venv/bin/activate  # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    ```

### Part 1b: Run tests

Tests do not require Docker or Ollama. From the repository root or from `player-service-app`:

```shell
cd player-service-app
pytest
```

### Part 2: Run Player Service (without LLM)

1. Start the Player service

   ```shell
    python3 app.py
   ```

2. Verify the Player service is running
      1. Open your browser and visit `http://localhost:8000/v1/players`
      2. If the application is running successfully, you will see player data appear in the browser

### Part 3: Start LLM Docker Container

Player service integrates with Ollama 🦙, which allows us to run LLMs locally. This app runs [tinyllama](https://ollama.com/library/tinyllama) model.

- [Ollama API documentation](https://github.com/ollama/ollama/blob/main/docs/api.md)
- [Ollama Python SDK](https://github.com/ollama/ollama-python)

1. Pull and run Ollama docker image and download `tinyllama` model
   - Pull Ollama docker image

    ```shell
    docker pull ollama/ollama
    ```

2. Run Ollama docker image on port 11434 as a background process

    ```shell
    docker run -d -v ollama:/root/.ollama -p 11434:11434 --name ollama ollama/ollama
    ```

3. Download the `tinyllama` model (wait until the Ollama container is running)

    ```shell
    until docker exec ollama ollama list >/dev/null 2>&1; do sleep 2; done
    docker exec ollama ollama pull tinyllama
    ```

4. Confirm the model is available

    ```shell
    curl -s http://localhost:11434/api/tags | grep -i tinyllama
    ```

5. Test Ollama API server

    ```curl
    curl -v --location 'http://localhost:11434/api/generate' --header 'Content-Type: application/json' --data '{"model": "tinyllama","prompt": "why is the sky blue?", "stream": false}'
    ```

Having trouble with docker? Try using podman as an alternative. Instructions [here](https://github.com/Intuit-A4A/backend-python-player-service/wiki/Supplemental-Materials:-Set-up-help)

### Part 4: Verify Player Service and LLM Integration

1. Ensure Player Service is running

    ```shell
   python3 app.py
    ```

2. Open your browser and visit `http://localhost:8000/v1/chat/list-models`
   - If the application is running successfully, you will see a json response that include information about tinyllama
