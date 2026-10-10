"""Traceback extraction: frame order, in-app rules, columns and source context."""

from __future__ import annotations

import json
import sys
from typing import List

import pytest

from faultline.domain import StackFrame
from faultline.infrastructure import LinecacheSourceReader, TracebackStackExtractor, is_in_app
from faultline.infrastructure.stack import default_stdlib_paths

STDLIB = ("/usr/lib/python3.12/", "c:/python312/lib/")


def level_three() -> None:
    raise ValueError("deep")


def level_two() -> None:
    level_three()


def level_one() -> None:
    level_two()


def frames_of(exc: BaseException) -> List[StackFrame]:
    return TracebackStackExtractor().from_exception(exc)


def test_frames_are_ordered_oldest_to_newest() -> None:
    try:
        level_one()
    except ValueError as exc:
        frames = frames_of(exc)
    names = [f.get("function", "").rsplit(".", 1)[-1] for f in frames]
    assert names == [
        "test_frames_are_ordered_oldest_to_newest",
        "level_one",
        "level_two",
        "level_three",
    ]
    # The newest frame is where the exception was raised.
    assert frames[-1]["lineno"] == level_three.__code__.co_firstlineno + 1
    assert all(f["filename"] == __file__ for f in frames)
    assert all(f["inApp"] for f in frames)
    assert all(f.get("module") == __name__ for f in frames)


def test_library_frames_are_not_in_app() -> None:
    try:
        json.loads("{not json")
    except ValueError as exc:
        frames = frames_of(exc)
    assert frames[0]["inApp"] is True
    lib = [f for f in frames if f["filename"].endswith(("decoder.py", "__init__.py"))]
    assert lib, frames
    assert all(not f["inApp"] for f in lib)
    assert frames[-1]["inApp"] is False


def test_lambda_and_nested_function_names() -> None:
    def outer() -> None:
        (lambda: 1 / 0)()

    with pytest.raises(ZeroDivisionError) as info:
        outer()
    frames = frames_of(info.value)
    functions = [f.get("function", "") for f in frames]
    assert functions[-1].endswith("<lambda>")
    assert any(fn.endswith("outer") for fn in functions)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="column info needs Python 3.11+")
def test_column_numbers_are_one_based() -> None:
    try:
        level_three()
    except ValueError as exc:
        frame = frames_of(exc)[-1]
    assert frame["colno"] == 5  # "    raise ..." starts at column 5


def test_from_current_stack_ends_at_the_caller() -> None:
    frames = TracebackStackExtractor().from_current_stack()
    assert frames[-1].get("function", "").endswith("test_from_current_stack_ends_at_the_caller")
    assert not any(str(f.get("module", "")).startswith("faultline") for f in frames[-1:])


@pytest.mark.parametrize(
    ("filename", "module", "expected"),
    [
        ("/srv/app/handlers.py", "app.handlers", True),
        ("/srv/app/.venv/lib/python3.12/site-packages/flask/app.py", "flask.app", False),
        ("/usr/lib/python3/dist-packages/requests/api.py", "requests.api", False),
        ("/usr/lib/python3.12/json/decoder.py", "json.decoder", False),
        ("<frozen importlib._bootstrap>", "importlib._bootstrap", False),
        ("<string>", None, False),
        ("", None, False),
        ("C:\\Python312\\Lib\\json\\decoder.py", "json.decoder", False),
        ("C:\\work\\venv\\Lib\\site-packages\\django\\core\\handlers\\base.py", "django", False),
        ("C:\\work\\app\\views.py", "app.views", True),
        ("/srv/app/faultline/__init__.py", "faultline", False),
        ("/srv/app/faultline/application/client.py", "faultline.application.client", False),
        ("/srv/app/faultline_ext/x.py", "faultline_ext.x", True),
    ],
)
def test_in_app_rules(filename: str, module: str, expected: bool) -> None:
    assert is_in_app(filename, module, STDLIB) is expected


def test_default_stdlib_paths_cover_this_interpreter() -> None:
    paths = default_stdlib_paths()
    assert not is_in_app(json.__file__, "json", paths)
    assert is_in_app(__file__, __name__, paths)


def test_source_context_for_in_app_frames() -> None:
    try:
        level_one()
    except ValueError as exc:
        frames = LinecacheSourceReader().add_source_context(frames_of(exc))
    top = frames[-1]
    assert top["contextLine"] == '    raise ValueError("deep")'
    assert top["preContext"][-1] == "def level_three() -> None:"
    assert len(top["preContext"]) == 5
    assert len(top["postContext"]) == 5
    assert top["postContext"][0] == ""


def test_no_source_context_for_library_frames() -> None:
    try:
        json.loads("[")
    except ValueError as exc:
        frames = LinecacheSourceReader().add_source_context(frames_of(exc))
    for f in frames:
        if not f["inApp"]:
            assert "contextLine" not in f


def test_source_context_lines_are_capped(tmp_path: pytest.TempPathFactory) -> None:
    path = tmp_path / "long_module.py"  # type: ignore[operator]
    path.write_text("x = 1\n" + "y = '" + "a" * 1000 + "'\n" + "raise RuntimeError('x')\n")
    code = compile(path.read_text(), str(path), "exec")
    with pytest.raises(RuntimeError) as info:
        exec(code, {"__name__": "long_module"})
    frames = LinecacheSourceReader().add_source_context(frames_of(info.value))
    top = frames[-1]
    assert top["filename"] == str(path)
    assert all(len(line) <= 300 for line in top["preContext"])
    assert top["preContext"][-1].endswith("\u2026")


def test_unreadable_file_yields_no_context() -> None:
    frame: StackFrame = {"filename": "/does/not/exist.py", "inApp": True, "lineno": 3}
    assert LinecacheSourceReader().add_source_context([frame]) == [frame]


def test_extractor_never_raises_on_odd_input() -> None:
    exc = ValueError("no traceback")
    assert frames_of(exc) == []
