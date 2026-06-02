# mycelium end-to-end test suite

These tests verify a **real, running** mycelium stack over the wire (MCP/SSE) —
the same surface ContextForge and Claude Code use. They exist to answer one
question before the team k8s rollout:

> Does the team deployment (server + Redis Streams + writer worker) behave the
> same as our current personal mycelium, and as intended?

## Why a pass can't be faked

Every write test mints a fresh random **nonce** (`e2e<uuid>`) and embeds it in
the content it writes, then retrieves it through a *different* tool and asserts
the nonce comes back. For that to pass the server must genuinely accept the
write, persist it, embed it, index it into ChromaDB, and serve it from a real
similarity query — in team mode, via the Redis stream and the writer worker. A
stub, a cache, a no-op, or a broken embedder each break a different link and
fail an assertion. We never import server code; the only contract is the
protocol on the socket.

Layered checks compound this:

- **Cross-tool corroboration** — write via `write_note`, verify via
  `query_notes` *and* `context` *and* the `status` index count delta.
- **Independent ground truth (white-box)** — read the markdown frontmatter on
  the pod's volume and the Redis stream directly via `docker exec` /
  `kubectl exec`. This is the only way to verify `author` / `committed_at` /
  `source_intent` (the read path never returns them) and to prove the team
  async lifecycle: producer writes `committed_at: null`, the worker flips it.
- **Parity oracle** — the identical scenario runs against a personal stack and
  a team stack; their observable contracts must match (modulo the documented
  "enqueued" wording and eventual-consistency window).

## Layout

| File | Covers |
|------|--------|
| `test_00_smoke.py` | reachability, full 22-tool surface, status/taxonomy/federation shapes |
| `test_20_roundtrip.py` | write→(commit)→retrieve nonce; context fan-out; index count delta |
| `test_25_diary.py` | diary write→read (gated; appends can't be cleaned) |
| `test_30_intent_guard.py` | `write_note` rejects empty `intent`; rejected writes don't persist |
| `test_40_dedupe.py` | idempotent upsert by title; near-dup-different-title warning; `check_duplicate` |
| `test_50_links.py` | add/query/find/delete links; `ended_at` filter; context graph expansion |
| `test_70_deletes_sync.py` | deletes/updates are synchronous; resurrection race (xfail) |
| `test_80_whitebox_provenance.py` | `author`/`committed_at`/`source_intent` stamped on disk |

Markers: `blackbox`, `whitebox`, `personal_only`, `known_race`. (The `team_only`
and `parity` markers, and the `test_60_lifecycle_team`/`test_90_parity` suites,
were retired with the ChromaDB→pgvector migration: storage is now synchronous
pgvector in both modes, so there's no async lifecycle or personal-vs-team
backend split to assert.)

## Running locally (hermetic — the deep gate)

Brings up postgres+pgvector + the mycelium server, then runs the contract +
white-box suite against it (both deployment modes share this synchronous
pgvector path, so one stack is representative):

```bash
tests/e2e/run_e2e.sh          # KEEP=1 to leave the stack up for debugging
```

This needs Docker. Same image the k8s deployment uses; the search index lives
in pgvector (no embedded/standalone ChromaDB, no Redis, no writer worker).

## Running against a deployed stack (black-box)

Point the suite at any reachable mycelium endpoint:

```bash
pip install -r tests/e2e/requirements.txt
MYCELIUM_E2E_URL="https://mycelium-sit.vibe.rain.co.za/sse" \
MYCELIUM_E2E_MODE=team \
MYCELIUM_E2E_TRANSPORT=sse \
MYCELIUM_E2E_TOKEN="$MY_JWT" \
  python -m pytest -c tests/e2e/pytest.ini tests/e2e -m "blackbox and not parity"
```

In team mode the black-box roundtrip tests poll within `MYCELIUM_E2E_COMMIT_TIMEOUT`
(default 90s) and so still prove the worker commits — no cluster access needed.

## Environment contract

| Var | Purpose |
|-----|---------|
| `MYCELIUM_E2E_URL` | MCP endpoint (e.g. `…/sse`). Required for black-box. |
| `MYCELIUM_E2E_TRANSPORT` | `sse` (default) or `http` (streamable-http) |
| `MYCELIUM_E2E_TOKEN` | bearer token, when going through the ContextForge gateway |
| `MYCELIUM_E2E_MODE` | `personal` (default) or `team` — sets async expectations |
| `MYCELIUM_E2E_COMMIT_TIMEOUT` | seconds to await a team commit (default 90) |
| `MYCELIUM_E2E_ALLOW_DIARY` | `1` to run diary write tests (default 1; set `0` vs prod) |
| `MYCELIUM_E2E_EXEC` | exec prefix for white-box: `docker exec` (default) or `kubectl exec -n <ns>` |
| `MYCELIUM_E2E_SERVER_CONTAINER` | server container/pod (enables white-box) |
| `MYCELIUM_E2E_WORKER_CONTAINER` | worker container/pod |
| `MYCELIUM_E2E_REDIS_CONTAINER` | redis container/pod (enables stream probes) |
| `MYCELIUM_E2E_DATA_DIR` | vault/chroma root inside the pod (default `/data`) |
| `MYCELIUM_E2E_URL_PERSONAL` / `_TEAM` | both set → parity tests run |

## Wiring into the GitLab SIT integration-test job

See `gitlab-integration-job.example.yml`. The Generic Docker pipeline's optional
job (runs between `sit` and `prod`, gates `prod`) is enabled via pipeline
`inputs`. The job runs the **black-box** suite against the deployed SIT URL with
`MYCELIUM_E2E_MODE=team`. The white-box + parity layers run in the hermetic
`run_e2e.sh` gate (needs Docker-in-Docker), best placed in a pre-merge job.
