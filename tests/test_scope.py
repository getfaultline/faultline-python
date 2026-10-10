"""Scope isolation across threads, asyncio tasks and nested with_scope blocks."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from typing import Dict, List

import faultline
from tests.conftest import IngestServer, make_harness


def test_with_scope_changes_do_not_leak() -> None:
    h = make_harness()
    h.hub.set_tag("global", "1")
    with h.hub.with_scope() as scope:
        scope.set_tag("inner", "1")
        h.hub.set_tag("also_inner", "1")
        h.hub.capture_message("inside")
        with h.hub.with_scope():
            h.hub.set_tag("nested", "1")
            h.hub.capture_message("nested")
    h.hub.capture_message("outside")
    tags = [e.get("tags") for e in h.events]
    assert tags[0] == {"global": "1", "inner": "1", "also_inner": "1"}
    assert tags[1] == {"global": "1", "inner": "1", "also_inner": "1", "nested": "1"}
    assert tags[2] == {"global": "1"}


def test_with_scope_restores_scope_when_body_raises() -> None:
    h = make_harness()
    try:
        with h.hub.with_scope():
            h.hub.set_tag("temp", "1")
            raise ValueError("boom")
    except ValueError:
        pass
    assert h.hub.get_scope() is h.hub.root_scope
    assert dict(h.hub.get_scope().get_data().tags) == {}


def test_scope_snapshot_is_taken_at_capture_time() -> None:
    h = make_harness()
    scope = h.hub.get_scope()
    data = scope.get_data()
    scope.set_tag("later", "x")
    assert "later" not in data.tags


def test_isolation_across_threads() -> None:
    h = make_harness()
    barrier = threading.Barrier(8)
    errors: List[BaseException] = []

    def worker(i: int) -> None:
        try:
            with h.hub.with_scope():
                h.hub.set_tag("worker", str(i))
                h.hub.set_user({"id": f"user-{i}"})
                h.hub.add_breadcrumb({"message": f"crumb-{i}"})
                barrier.wait(timeout=5)
                h.hub.capture_message(f"from {i}")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(h.events) == 8
    for ev in h.events:
        i = ev["message"].split()[-1]
        assert ev["tags"] == {"worker": i}
        assert ev["user"] == {"id": f"user-{i}"}
        assert [b["message"] for b in ev["breadcrumbs"]] == [f"crumb-{i}"]


def test_isolation_across_asyncio_tasks() -> None:
    h = make_harness()

    async def handler(i: int) -> None:
        with h.hub.with_scope():
            h.hub.set_tag("task", str(i))
            await asyncio.sleep(0.01 * (5 - i))
            h.hub.add_breadcrumb({"message": f"after-sleep-{i}"})
            await asyncio.sleep(0)
            h.hub.capture_message(f"task {i}")

    async def main() -> None:
        await asyncio.gather(*(handler(i) for i in range(5)))

    asyncio.run(main())
    assert len(h.events) == 5
    for ev in h.events:
        i = ev["message"].split()[-1]
        assert ev["tags"] == {"task": i}
        assert [b["message"] for b in ev["breadcrumbs"]] == [f"after-sleep-{i}"]


def test_tasks_created_inside_a_scope_inherit_it() -> None:
    h = make_harness()
    seen: Dict[str, str] = {}

    async def child() -> None:
        seen.update(h.hub.get_scope().get_data().tags)

    async def main() -> None:
        with h.hub.with_scope():
            h.hub.set_tag("parent", "yes")
            await asyncio.create_task(child())

    asyncio.run(main())
    assert seen == {"parent": "yes"}


def test_copy_context_propagates_scope_to_threads() -> None:
    h = make_harness()
    seen: Dict[str, str] = {}
    with h.hub.with_scope():
        h.hub.set_tag("req", "42")
        ctx = contextvars.copy_context()
        t = threading.Thread(
            target=ctx.run, args=(lambda: seen.update(h.hub.get_scope().get_data().tags),)
        )
        t.start()
        t.join()
    assert seen == {"req": "42"}


def test_public_with_scope(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url, integrations={"http": False})
    faultline.set_tag("app", "demo")
    with faultline.with_scope() as scope:
        scope.set_tag("checkout", "1")
        faultline.capture_message("inside")
    faultline.capture_message("outside")
    assert faultline.flush(5)
    by_msg = {e["message"]: e for e in ingest.events}
    assert by_msg["inside"]["tags"] == {"app": "demo", "checkout": "1"}
    assert by_msg["outside"]["tags"] == {"app": "demo"}


def test_public_with_scope_propagates_user_exceptions() -> None:
    try:
        with faultline.with_scope():
            raise KeyError("mine")
    except KeyError as exc:
        assert exc.args == ("mine",)
    else:  # pragma: no cover
        raise AssertionError("exception swallowed")
