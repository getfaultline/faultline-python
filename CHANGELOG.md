# Changelog

All notable changes to `getfaultline` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - unreleased

### Added

- `init`, `capture_exception`, `capture_message`, `set_user`, `set_tag(s)`, `set_context`,
  `set_extra`, `add_breadcrumb`, `with_scope`, `flush`, `close`, `is_enabled`.
- Tracebacks ordered oldest to newest with in-app detection, source context and columns
  (3.11+); `__cause__` / `__context__` chains (max 5); exception group support.
- Background batched transport: gzip, retries with backoff and jitter, `429 Retry-After`,
  drops on `401` / `413`, `atexit` flush, fork safety.
- Client-side scrubbing identical to the server and the Node SDK; dedupe within 1 s.
- Integrations: `sys.excepthook`, `threading.excepthook`, `logging`, `http.client`, WSGI,
  ASGI, Flask, FastAPI / Starlette, Django.
