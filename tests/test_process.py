"""End-to-end behavior in a real interpreter: atexit delivery, uncaught crashes, no hangs."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from typing import Tuple

from tests.conftest import IngestServer

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def run(code: str, timeout: float = 20) -> Tuple[int, str, float]:
    env = {**os.environ, "PYTHONPATH": SRC}
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return proc.returncode, proc.stderr, time.monotonic() - start


def test_events_are_flushed_at_interpreter_exit(ingest: IngestServer) -> None:
    code, err, _ = run(
        f"""
        import faultline
        faultline.init(api_key="flt_ingest_test", endpoint="{ingest.url}")
        faultline.capture_message("bye")
        """
    )
    assert code == 0, err
    ingest.wait_for(1)
    assert ingest.events[0]["message"] == "bye"


def test_uncaught_exception_is_captured_and_default_behavior_kept(ingest: IngestServer) -> None:
    code, err, _ = run(
        f"""
        import faultline
        faultline.init(api_key="flt_ingest_test", endpoint="{ingest.url}")
        def main():
            raise RuntimeError("fatal crash")
        main()
        """
    )
    assert code == 1
    assert "RuntimeError: fatal crash" in err  # the default hook still printed the traceback
    ingest.wait_for(1)
    ev = ingest.events[0]
    assert ev["level"] == "fatal"
    assert ev["exception"]["value"] == "fatal crash"
    assert ev["exception"]["stacktrace"]["frames"][-1]["function"] == "main"
    assert ev["tags"]["mechanism"] == "excepthook"


def test_exit_does_not_hang_when_the_server_is_unresponsive(ingest: IngestServer) -> None:
    ingest.delay = 30
    code, err, elapsed = run(
        f"""
        import faultline
        faultline.init(api_key="flt_ingest_test", endpoint="{ingest.url}")
        faultline.capture_message("slow")
        """
    )
    assert code == 0, err
    assert elapsed < 6


def test_exit_does_not_hang_when_the_server_is_down() -> None:
    code, err, elapsed = run(
        """
        import faultline
        faultline.init(api_key="flt_ingest_test", endpoint="http://127.0.0.1:9")
        for i in range(50):
            faultline.capture_message(f"m{i}")
        raise SystemExit(3)
        """
    )
    assert code == 3, err
    assert elapsed < 6
