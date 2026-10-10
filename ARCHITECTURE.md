# Architecture decision log: `getfaultline` (Python)

## Layers

```
src/faultline/
  domain/          pure Python: event contract (TypedDicts), Scope, options validation, limits,
                   scrubbing, JSON normalization, event sanitization, dedupe key
  application/     FaultlineClient, Hub (contextvars scopes), Integration protocol
    ports.py       Transport, StackExtractor, SourceContextReader, Clock, IdGenerator,
                   RandomSource, Logger, RuntimeInfo
  infrastructure/  HttpTransport (worker thread), TracebackStackExtractor,
                   LinecacheSourceReader, runtime adapters (clock, uuid, stderr logger, os)
  integrations/    excepthook, threading, logging, http.client, wsgi, asgi, flask, fastapi,
                   django
  __init__.py      public API + composition root
```

Dependency rule: `domain <- application <- infrastructure | integrations <- faultline`.
`infrastructure` and `integrations` are siblings and may not import each other. They meet only
in `faultline/__init__.py`. This mirrors the Node SDK one to one.

---

### ADR-001: Enforce boundaries with import-linter plus an AST test

- **Decision:** `pyproject.toml` declares three import-linter contracts: the layer order
  (`integrations | infrastructure > application > domain`), a pure domain (no `os`, `sys`,
  `socket`, `http`, `threading`, `asyncio`, `logging`, `contextvars`, ...), and "frameworks
  only in their integration module". `tests/test_architecture.py` checks the same rules with
  `ast`, so the rule also holds where import-linter is not run.
- **Verification:** We added deliberate violations (`import os` and an infrastructure import
  in `domain/limits.py`) and confirmed both contracts break.
- **Note:** `urllib.parse` is allowed in the domain (it is pure and needed to decode query
  keys while scrubbing). import-linter cannot tell `urllib.parse` from `urllib.request`, so the
  AST test forbids `urllib.request` explicitly.

### ADR-002: Zero runtime dependencies; hand-written options validator

- **Decision:** `resolve_options()` validates `init()` options by hand and never raises. An
  invalid optional value falls back to its default with a warning; only a missing `api_key`
  or an unusable `endpoint` disables the SDK. Unknown keyword arguments are ignored with a
  warning instead of raising `TypeError`.
- **Why:** Same reasoning as the Node SDK's ADR-002. pydantic is a test-only dependency: every
  produced event is validated against strict pydantic models that mirror CONTRACTS.md
  (`tests/contract.py`).

### ADR-003: `contextvars` used directly in the application layer

- **Decision:** The `Hub` owns a `ContextVar[Scope]`. `with_scope()` forks the current scope
  with `clone()` and sets it for the block; framework middleware does the same per request.
- **Why not a port (as Node did with AsyncLocalStorage)?** `contextvars` is a pure language
  primitive with no I/O and identical behavior on every supported Python, so a port adds
  indirection without a second implementation. The domain still never touches it.
- **Semantics:** asyncio tasks copy the context when created, so tasks spawned inside a scope
  inherit it and concurrent tasks are isolated. New threads start from the root scope unless
  started via `contextvars.copy_context().run`, matching the stdlib's own semantics.
  `ContextVar.reset` failures (a block exited in another context) fall back to restoring the
  previous value instead of raising.

### ADR-004: Copy-on-write Scope

- **Decision:** Every `Scope` mutation replaces its dict/tuple instead of changing it in place.
  `clone()` and `get_data()` are O(1) reference copies.
- **Why:** The root scope can be written from many threads at once. With copy-on-write a
  snapshot taken by the client can never change underneath it, and no lock is needed in the
  (pure) domain. Concurrent writers to the same scope may lose an update, but never crash.

### ADR-005: Synchronous event processing, asynchronous delivery

- **Node:** splits capture into a sync phase and an async phase (source reads use
  `fs.promises`).
- **Python:** everything up to `transport.send()` runs in the caller's thread: traceback
  extraction, sampling, `ignore_errors`, dedupe, source context, sanitization, scrubbing and
  `before_send`. Delivery (batching, gzip, HTTP, retries) runs on a worker thread.
- **Why:** Traceback objects reference live frames and must be read before the caller moves
  on; `linecache` is cached and in-memory after the first read. Doing it synchronously also
  means `capture_exception` returns `None` when `before_send` drops the event (Node returns
  the id anyway). Serialization happens in `send()` so the queued event is a frozen snapshot
  and its size is known up front.

### ADR-006: `before_send` receives scrubbed events; a raising `before_send` drops the event

Identical to the Node SDK (ADR-005 there). The result is re-sanitized, so a buggy hook cannot
produce a payload that violates the contract.

### ADR-007: `send_default_pii` semantics

- **`False` (default):** `user.ipAddress` is removed and the `cookie`, `set-cookie`,
  `authorization`, `proxy-authorization`, `x-forwarded-for`, `x-real-ip` and `forwarded`
  headers are removed entirely. Framework integrations do not collect the client IP.
- **`True`:** the IP (`REMOTE_ADDR` / ASGI `client`) is collected and those headers are kept.
  Values under sensitive keys are still replaced with `"[Filtered]"`.

### ADR-008: Cause chain and exception groups

- **Chain:** `__cause__`, else `__context__` unless `__suppress_context__`. At most 5
  exceptions in total (root plus 4), as in Node's ADR-007. Cycles are broken with an
  identity set.
- **Exception groups:** the contract has a single `cause` slot, so a group is reported as one
  exception whose `value` lists up to 10 sub-exceptions (`ValueError('x'), ...`) and whose
  `cause` is its explicit cause/context or, failing that, its first sub-exception. Detection
  is duck-typed, so the `exceptiongroup` backport on 3.9/3.10 works too.
- **Non-exception values** (`capture_exception("text")`, a dict, ...) become a synthetic
  `Error` with the caller's stack (SDK frames removed).

### ADR-009: In-app rules and frame data

- Not in-app: paths containing `/site-packages/` or `/dist-packages/`, paths under the
  interpreter's stdlib (`sysconfig` `stdlib`/`platstdlib` and `base_prefix/lib/pythonX.Y`),
  synthetic filenames starting with `<`, and modules named `faultline` / `faultline.*`.
  Windows paths are normalized (`\` to `/`, drive paths lowercased).
- `function` is `co_qualname` on 3.11+ (`Class.method`), `co_name` before. `colno` (1-based)
  comes from `co_positions()` on 3.11+.
- `filename` is the absolute path, as in the Node SDK.

### ADR-010: Dedupe

Same rule as Node's ADR-009 (consecutive events only, 1 s window from the last sent event;
key = fingerprint, else type + value + newest 5 frames, else level + message), guarded by a
lock because captures can come from many threads.

### ADR-011: Transport

- **Queue:** `deque`, max 100 events, drop oldest. Guarded by a `Condition`.
- **Worker:** one daemon thread (`faultline-transport`), started lazily on the first event.
  It sends when 20 events are queued, every 2 s, or when `flush()` asks. A send lock ensures a
  single sender.
- **Batches:** at most 50 events and 1 MB uncompressed, gzip-compressed. Oversized events are
  trimmed (breadcrumbs, then source context, then extra) or dropped.
- **Retries:** network errors, timeouts (5 s), `5xx` and `408` are retried inline with equal
  jitter `min(30, 2^n)/2 + rand * min(30, 2^n)/2` seconds, up to 3 retries. Backoff sleeps use
  an `Event` so `close()` interrupts them. `429` pauses the transport until `Retry-After`
  (seconds or HTTP date, capped at 10 min) and requeues the batch at the front; an event is
  dropped after 3 rate-limited attempts. `401`, `413` and other `4xx` drop the batch.
- **HTTP client:** `urllib.request` (honors proxy environment variables). The SDK's own
  requests are recognized by the http integration via the ingest URL and the worker thread
  name and are never recorded as breadcrumbs.
- **Shutdown:** `atexit` calls `close(2.0)`: flush with a deadline, then stop. Because the worker
  is a daemon thread and every wait is bounded, exit never hangs (tested with a server that
  never answers and with no server at all). If the worker cannot run (for example a thread
  cannot be started during interpreter shutdown), `flush()` sends from the calling thread
  with the same deadline.
- **Fork safety:** `os.register_at_fork(after_in_child=...)` resets locks and the worker in
  the child; queued events are kept and sent by a new worker.

### ADR-012: Integrations patch politely and restore exactly

- `sys.excepthook` / `threading.excepthook`: capture, then always call the previous hook.
  `KeyboardInterrupt` and thread `SystemExit` are not captured.
- `logging`: wraps `Logger.callHandlers` (sees every record that passes level checks, needs no
  handler). A thread-local reentrancy guard and the ignored `faultline` logger prevent loops.
- `http.client`: wraps `putrequest` / `getresponse` and never changes arguments, return values
  or exceptions. The scope is captured at `putrequest`, so a response read in another context
  still lands in the right scope.
- `teardown()` restores the original only if our wrapper is still installed, so we never undo
  somebody else's later patch.

### ADR-013: Framework integrations

- Each lives in its own module and imports its framework lazily (enforced by import-linter
  and the AST test), so `import faultline` never imports Flask, Django or Starlette.
- **WSGI:** each request runs in `contextvars.copy_context()`; the response iterable is
  wrapped so streaming and `close()` run in the request context and streaming errors are
  captured.
- **ASGI:** the request runs inside `with_scope()`; `lifespan` passes through untouched.
- **Flask:** WSGI middleware for scope and request data, plus `got_request_exception` for
  capture (Flask turns exceptions into 500 responses before WSGI middleware can see them).
- **Django:** sync and async capable middleware for the scope, plus Django's
  `got_request_exception` signal for capture (fires only for real 500s).
- An exception is marked once captured, so stacked integrations (Flask signal plus WSGI with
  `PROPAGATE_EXCEPTIONS`) never report it twice.

### ADR-014: Server-side caps enforced client-side

The server's ingest schema also caps fingerprint parts (20), header/query keys (256) and values
(8192), and `user.ipAddress` (64). These are not pinned in CONTRACTS.md, but sending more
would get the event rejected, so the SDK truncates to them.

### ADR-015: Toolchain

- hatchling, src layout, version in `src/faultline/_version.py`.
- ruff (lint + format), mypy `--strict` (tests and examples relax a few error codes for
  deliberately malformed fixtures and untyped framework decorators; the package does not).
- CI runs lint/typecheck/import contracts once and tests on 3.9 to 3.14 (plus Windows and
  macOS on 3.13), then builds, runs `twine check --strict`, and smoke-tests the wheel in an
  empty venv. Locally the suite was also run on 3.9.6 (Django 4.2) and 3.14.

### Suggestions (not implemented, outside spec)

- Offline persistence of the queue across restarts.
- A `transport` option for custom transports (serverless, tests).
- Celery / RQ integrations (per-task scope and capture).
- Capturing local variables per frame (opt-in, PII-sensitive).
