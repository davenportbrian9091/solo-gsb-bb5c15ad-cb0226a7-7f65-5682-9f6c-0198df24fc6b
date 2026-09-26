# Tail Sampling Service

A self-contained **distributed-tracing tail sampler** built with Python +
FastAPI. Clients submit spans in batches; the service aggregates them into
complete traces, waits for a silence window, and decides **keep / drop** using:

1. **Priority rules (evaluated first)**
   * any span in the trace has `status: "error"` (or a conventional error
     attribute such as `http.status_code: 5xx`, `error: true`,
     `exception.type`);
   * the trace end-to-end duration exceeds the slow-trace threshold.
2. **Per-minute sampling budget** — at most `N` retained traces per minute.
   Priority traces take slots first; when the minute settles, ordinary traces
   fill the remaining slots with **stable-hash ranking** (BLAKE2b over the
   trace id), so the same trace is always selected on every process/restart
   and selection spreads uniformly over trace ids.

Every response carries the decision (`keep` / `drop` / `pending`) and the
**reason** it was hit (`error_trace`, `slow_trace`,
`priority_budget_exhausted`, `stable_hash_selected`,
`stable_hash_not_selected`, `normal_budget_exhausted`).

State lives in a single SQLite file (WAL mode). No third-party accounts, no
external services.

---

## Quick start (Docker Compose)

```bash
docker compose up --build
```

| What | Where |
| --- | --- |
| Service / API base | <http://localhost:8080> |
| Interactive API docs (Swagger) | <http://localhost:8080/docs> |
| Health check | `GET http://localhost:8080/health` |

The SQLite database is kept in the named volume `sampler-data`
(container path `/data/traces.db`) and survives `docker compose down`
(use `docker compose down -v` to wipe it).

Stop:

```bash
docker compose down
```

### Run without Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
TS_DATABASE_PATH=./data/traces.db \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## Try it (no external tools needed)

With the service running, use the zero-dependency demo client (it sends a mix
of normal/error/slow traces and prints the decisions):

```bash
python3 scripts/demo_client.py http://localhost:8080
```

Or with curl:

```bash
curl -s -X POST http://localhost:8080/api/v1/spans:batch \
  -H 'Content-Type: application/json' \
  -d '{
    "spans": [
      {
        "trace_id": "abc-123",
        "span_id": "root",
        "service": "checkout",
        "name": "POST /order",
        "start_time": "2026-09-18T10:00:00Z",
        "end_time":   "2026-09-18T10:00:02.5Z",
        "status": "ok",
        "attributes": {"http.status_code": 200}
      }
    ]
  }'

# wait the silence window (default 10s) ...
curl -s http://localhost:8080/api/v1/decisions/abc-123
```

Decision lifecycle for a trace:

```
open  ──(silence window elapsed)──►  pending (normal traces, until minute settles)
open  ────────────────────────────► priority_kept / dropped (error|slow decided at close)
pending ──(minute end + silence window)──► normal_kept / dropped
```

For tests/demos you can skip the waiting with
`POST /api/v1/admin/force-flush` (closes all open traces and settles all
minutes immediately).

---

## API

All endpoints are JSON and documented at `/docs`.

### `POST /api/v1/spans:batch` — submit a batch of spans

```json
{
  "spans": [
    {
      "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
      "span_id": "00f067aa0ba902b7",
      "parent_span_id": null,
      "name": "GET /cart",
      "service": "shop-web",
      "start_time": "2026-09-18T10:00:00.100Z",
      "end_time":   "2026-09-18T10:00:00.320Z",
      "status": "ok",
      "attributes": {"http.status_code": 200, "user_id": "u-42"}
    }
  ]
}
```

* `start_time` / `end_time` accept ISO-8601 strings (`...Z` or with offset)
  **or** epoch numbers in seconds, milliseconds, microseconds or nanoseconds
  (unit is auto-detected).
* `status`: `ok` | `error` | `unset` (case-insensitive).
* `(trace_id, span_id)` duplicates are idempotent and reported as
  `duplicates`; spans arriving after the trace was decided are `rejected`.

Response:

```json
{
  "accepted": 1,
  "rejected": 0,
  "duplicates": 0,
  "traces": {
    "4bf92f3577b34da6a3ce929d0e0e4736": {
      "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
      "state": "open",
      "decision": "pending",
      "reason": "trace_aggregating",
      "span_count": 1
    }
  }
}
```

### `GET /api/v1/decisions/{trace_id}` — query one decision

```json
{
  "trace_id": "abc-123",
  "state": "priority_kept",
  "decision": "keep",
  "reason": "slow_trace",
  "is_error": false,
  "is_slow": true,
  "duration_ns": 2500000000,
  "span_count": 1,
  "minute_bucket": 1789716000,
  "decided_at_ns": 1789716022123000000
}
```

Unknown trace id → `404`.

### `POST /api/v1/decisions:batch` — query many decisions

```json
{ "trace_ids": ["abc-123", "def-456"] }
```

Returns `{ "decisions": { ... }, "missing": [ ... ] }`.

### `GET /api/v1/traces/{trace_id}/service-time` — exclusive time per service

Breaks one trace down by service so you can see where the time actually went.
Each span's **exclusive** time is its own `[start, end)` length minus the time
covered by its direct children (overlapping children are subtracted once,
self-referencing spans are ignored, zero-length spans count as 0). Spans
without a `service` attribute are grouped under `null`. Works on open traces
too — they are computed from a consistent snapshot of the spans received so
far.

```bash
curl -s http://localhost:8080/api/v1/traces/abc-123/service-time
```

```json
{
  "trace_id": "abc-123",
  "span_count": 4,
  "services": [
    { "service": "checkout",     "span_count": 1, "exclusive_ns": 1100000000 },
    { "service": "payments",     "span_count": 1, "exclusive_ns": 500000000 },
    { "service": "payments-db",  "span_count": 1, "exclusive_ns": 500000000 },
    { "service": null,           "span_count": 1, "exclusive_ns": 400000000 }
  ]
}
```

`services` is sorted by `exclusive_ns` descending, then service name
ascending with the `null` group last. Unknown trace id → `404`.

### `GET /api/v1/traces/{trace_id}/concurrency` — concurrent-span windows

Locates the time ranges inside one trace where several spans are running at
once. The stored spans are half-open nanosecond intervals `[start, end)`; a
span ending at the same instant another starts does **not** count as overlap,
and zero-length spans are ignored. The sweep line returns every merged range
in which at least `min_active` spans are active — ranges that touch at a
single instant come back merged, ranges separated by a real (positive-length)
gap stay apart — together with each range's peak concurrency and the total
time covered. Works on open traces too, from a consistent snapshot taken
inside the engine lock.

```bash
curl -s 'http://localhost:8080/api/v1/traces/4bf92f3577b34da6a3ce929d0e0e4736/concurrency?min_active=2'
```

```json
{
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "min_active": 2,
  "span_count": 4,
  "window_count": 2,
  "total_ns": 14000000,
  "max_active": 3,
  "windows": [
    {
      "start_ns": 1789716000100000000,
      "end_ns": 1789716000112000000,
      "peak_active": 3,
      "duration_ns": 12000000
    },
    {
      "start_ns": 1789716000300000000,
      "end_ns": 1789716000302000000,
      "peak_active": 2,
      "duration_ns": 2000000
    }
  ]
}
```

* `min_active` defaults to `2`; values below 2 → `422`.
* `windows` is ordered by `start_ns`; `total_ns` is the sum of the merged
  window lengths. Unknown trace id → `404`.

### Other endpoints

| Method & path | Purpose |
| --- | --- |
| `GET /health` | Liveness |
| `GET /api/v1/stats` | Open/pending counts, retained/dropped totals, per-minute budget usage |
| `GET /api/v1/config` | Effective configuration and current minute bucket |
| `POST /api/v1/admin/force-flush` | Close open traces and settle all minutes immediately |

### Reason codes

| Reason | Meaning |
| --- | --- |
| `trace_aggregating` | Silence window / minute settlement not reached yet |
| `error_trace` | Kept by the error priority rule |
| `slow_trace` | Kept by the slow-trace priority rule |
| `priority_budget_exhausted` | Error/slow trace, but the minute budget was full → dropped |
| `stable_hash_selected` | Normal trace; its hash rank was within the remaining budget → kept |
| `stable_hash_not_selected` | Normal trace; lost the hash draw → dropped |
| `normal_budget_exhausted` | Normal trace; no budget remained after priority traces → dropped |

---

## How sampling works

1. **Aggregation.** Spans are merged per `trace_id`; a trace holds the min
   start, max end, span count and an error flag. The trace is considered
   complete when no new span has arrived for `TS_IDLE_WINDOW_SECONDS`
   (default **10 s**).
2. **Classification at close.** Error wins over slow; either makes the trace
   *priority* and it is decided immediately against the budget of the minute
   the trace belongs to (derived from its end time).
3. **Normal traffic waits for the minute to settle.** A minute is settled
   `idle window + one tick` after the minute ends, so all priority traces
   with timestamps in that minute have already been counted. Remaining budget
   slots are then handed out by stable-hash rank across that minute's normal
   traces.
4. **Persistence.** Spans, aggregates, candidates, decisions and minute
   counters are all in SQLite. On startup the process rebuilds its internal
   state from the database: open traces keep waiting, undecided candidates
   are settled, decisions are immutable.

### Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `TS_DATABASE_PATH` | `/data/traces.db` | SQLite file |
| `TS_IDLE_WINDOW_SECONDS` | `10` | Silence window that completes a trace |
| `TS_TICK_INTERVAL_SECONDS` | `1` | Background aggregation tick |
| `TS_SLOW_THRESHOLD_NS` | `1000000000` | Trace duration (ns) above which a trace is slow (1 s) |
| `TS_SAMPLE_BUDGET_PER_MINUTE` | `100` | Max retained traces per minute |

---

## Scaling note

The container runs a **single uvicorn worker on purpose**: the per-minute
budget is a serialized in-process decision over one SQLite database. To scale
out, run multiple instances and shard clients by `trace_id` (e.g. hash the
trace id to an instance) so a whole trace always lands on one sampler.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```

The test suite covers priority budget saturation, slow/error classification,
stable-hash fill counts, determinism, duplicate/late span handling, batch
queries, timestamp formats, per-service exclusive-time computation,
concurrent-span window detection (half-open endpoints, adjacent merging,
zero-length spans, threshold validation), and crash recovery from SQLite.
