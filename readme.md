# rag-ai

rag-ai watches a directory of Markdown, plain-text, and HTML files and serves semantic retrieval over HTTP. PostgreSQL 17 with pgvector stores the index. Ollama supplies embeddings through its OpenAI-compatible API. The default model is `nomic-embed-text` with 768 dimensions.

Run one Uvicorn worker per watched directory and index. The watcher reconciles on startup, skips unchanged files, and processes changes in the background. Markdown chunks retain heading paths for citations and exclude YAML frontmatter from embedded text.

## Local setup

Use `nix develop` with flakes enabled, or install Python 3.12 or newer, uv, and libpq. The database example also requires Docker Compose. Start Ollama and pull the embedding model.

```sh
ollama pull nomic-embed-text
```

Create a project-root `.env` with the following settings. Replace the password placeholders and URL-encode reserved characters in the password used in `POSTGRES_URL`. The example uses Compose's default database user, `postgres`.

```dotenv
POSTGRES_PASSWORD=<local-dev-password>
POSTGRES_URL=postgresql://postgres:<url-encoded-local-dev-password>@127.0.0.1:5432/rag-ai
DOCS_DIR=/absolute/path/to/documents
```

Start Postgres, apply [the schema](scripts/postgres-schema.sql), and start the API.

```sh
docker compose up -d --wait
docker compose exec -T postgres psql -X -U postgres -d rag-ai \
  -v ON_ERROR_STOP=1 < scripts/postgres-schema.sql
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

If you set Compose's `POSTGRES_USER`, use the same user in the schema command and connection URL. Compose stores data in a persistent named volume.

## Nix deployment

`nix build .#rag-ai` builds the service with uv2nix from `uv.lock`. The launcher, `result/bin/rag-ai`, binds to `127.0.0.1`, takes its HTTP port from `RAG_AI_PORT` (default 8000), and starts one Uvicorn worker.

Import `rag-ai.nixosModules.default` in the NixOS host configuration and `rag-ai.homeManagerModules.default` in the Home Manager configuration. The examples assume `docs-user` is an existing Unix account and that Home Manager runs the service as that account.

```nix
# NixOS host
services.rag-ai.backing = {
  enable = true;
  user = "docs-user";
};
```

```nix
# Home Manager user
services.rag-ai = {
  enable = true;
  docsDir = "/home/docs-user/documents";
  target = "default.target";
  port = 8000;
};
```

For a fresh peer-authenticated deployment, the host module provisions Postgres with pgvector and names the database and role after the Unix account. It enables lingering and loads `nomic-embed-text` in Ollama. The `rag-ai-schema.service` unit waits for Postgres setup, creates the `vector` extension as `postgres`, then applies the schema as the database owner. The user service connects through the Unix socket at `/run/postgresql`.

For a nondefault Postgres port, set the host's port and override Home Manager's `postgresUrl`. The Home Manager `port` option sets the HTTP listener.

```nix
# NixOS host
services.postgresql.settings.port = 5433;
```

```nix
# Home Manager user
services.rag-ai.postgresUrl = "postgresql:///docs-user?host=/run/postgresql&port=5433";
```

For an existing deployment, keep the data directory or volume and point `services.rag-ai.postgresUrl` (or `POSTGRES_URL` for a manual launch) at the intended database. Back up that database, stop the watcher, and apply `scripts/postgres-schema.sql` as a role that owns the affected tables or has equivalent privileges before restarting. If the `vector` extension is missing, have a database administrator create it in that database first. The schema adds missing objects in a transaction. The watcher reindexes older records it can match to the configured directory, and replacement rows become searchable as their files are reindexed.

```sh
systemctl --user restart rag-ai.service
journalctl --user -u rag-ai.service -f
journalctl -u rag-ai-schema.service
```

### Optional Wiki Agents deployment

If your Wiki Agents configuration defines `wiki-agents.target`, attach rag-ai to that target. This example keeps the six default ignore patterns and adds two vault-specific exclusions.

```nix
services.rag-ai = {
  enable = true;
  target = "wiki-agents.target";
  docsDir = "/home/docs-user/Documents/obsidian-vault";
  docsIgnorePatterns = [
    ".obsidian" ".git" ".trash" ".tmp-*" "*.tmp" "~*"
    "_System/Templates" "*.sync-conflict-*"
  ];
};
```

`systemctl --user restart wiki-agents.target` restarts its attached user services.

## Settings

Process environment values take precedence over dotenv values. From a source checkout, the default file is the project-root `.env`. Set `RAG_AI_ENV_FILE` in the process environment to select another file, or set it to an empty value to disable dotenv. Home Manager supplies process environment values and disables dotenv. Docker Compose selects its environment file independently.

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTGRES_URL` | Required | Postgres connection URI or conninfo string |
| `DOCS_DIR` | Required | Directory to watch. Prefer an absolute path. |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible embedding endpoint |
| `LLM_API_KEY` | `ollama` | API key passed to the embedding client |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Model for ingestion and retrieval queries |
| `EMBEDDING_DIMENSIONS` | `768` | Expected vector size. The supplied schema requires 768. |
| `DOCS_IGNORE_PATTERNS` | `[".obsidian", ".git", ".trash", ".tmp-*", "*.tmp", "~*"]` | JSON array replacing the default ignores |
| `DEPENDENCY_TIMEOUT_SECONDS` | `2` | Timeout for Postgres checks and Ollama model availability checks |
| `EMBEDDING_PROBE_TIMEOUT_SECONDS` | `60` | Timeout for the startup embedding request |
| `STARTUP_RETRY_SECONDS` | `1` | Initial dependency retry delay |
| `STARTUP_RETRY_MAX_SECONDS` | `30` | Maximum dependency retry delay |
| `RETRIEVAL_TIMEOUT_SECONDS` | `10` | Positive, finite deadline for retrieval work. Exceeding it returns HTTP 504. |
| `LOG_LEVEL` | `INFO` | Application log level |
| `ENVIRONMENT` | `development` | Set to `production` for JSON application logs |

Leaving Home Manager's `docsIgnorePatterns` unset uses the defaults. An empty list clears them, and a supplied list replaces them. Patterns without a slash match names at any depth. Patterns containing a slash match paths relative to `DOCS_DIR`. Ignoring a directory also excludes its descendants.

## HTTP API

| Endpoint | Contract |
| --- | --- |
| `GET /health` | Returns `ready`, `startup`, `postgres`, and `ollama`. HTTP 200 means the watcher has started and dependencies pass their checks. HTTP 503 means startup is pending or readiness has failed. |
| `GET /ingestion/status` | Returns watcher state, current file, queue length, and counters. Returns HTTP 503 while startup is pending or if the watcher fails. Use this endpoint to track initial indexing independently of readiness. |
| `POST /search` | Accepts JSON with `query` and optional `k`, `path_prefix`, and `exclude_paths`. Returns chunks with `source_file`, `chunk_index`, `heading`, `content`, and `score`. |
| `GET /similar` | Accepts `path`, optional `k`, and optional repeated `exclude_paths`. Returns other notes with `source_file` and `score`, ranked by similarity between mean chunk embeddings. Returns HTTP 404 when the requested note has no usable embedding. |

Paths use POSIX separators and are relative to `DOCS_DIR`. Search prefixes are literal. Add a trailing slash to select a directory. Exclusions match exact paths. Both routes default to `k=10`, accept `k` from 1 to 100 and up to 100 exclusions, and rank by cosine similarity from −1 to 1. Higher scores mean closer matches. Chunks without headings have `"heading": null`.

```sh
curl -i http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ingestion/status
curl -fsS http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"configure backups","k":5,"path_prefix":"Runbooks/","exclude_paths":["Runbooks/Draft.md"]}'
curl -fsS -G http://127.0.0.1:8000/similar \
  --data-urlencode 'path=Runbooks/Backups.md' \
  --data-urlencode 'k=5'
```

## Tests

Use placeholder settings to run unit tests independently of your local configuration. The opt-in smoke test uses local Ollama with `nomic-embed-text` and applies the schema inside a disposable pgvector container.

```sh
RAG_AI_ENV_FILE= POSTGRES_URL=postgresql:///unused DOCS_DIR=. \
  uv run --locked pytest -m 'not integration'

RAG_AI_ENV_FILE= RAG_AI_LIVE_SMOKE=1 POSTGRES_URL=postgresql:///unused DOCS_DIR=. \
  uv run --locked pytest -rs tests/test_startup_smoke.py
```
