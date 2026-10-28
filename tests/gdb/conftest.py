"""Fixtures for the GDB integration tests (``-m gdb``)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .harness import FIXTURES

_MISSING = [tool for tool in ("gdb", "gcc", "g++") if shutil.which(tool) is None]


def pytest_collection_modifyitems(config, items):
    reason = None
    if os.name == "nt":
        reason = "GDB integration tests use the POSIX PTY backend"
    elif _MISSING:
        reason = f"missing tools: {', '.join(_MISSING)}"
    here = Path(__file__).resolve().parent
    for item in items:
        if Path(str(item.fspath)).resolve().parent != here:
            continue
        item.add_marker(pytest.mark.gdb)
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def build(tmp_path_factory):
    """``build("name.c", "-O2")`` compiles a fixture once per session."""
    out_dir = tmp_path_factory.mktemp("bin")
    cache: dict[tuple[str, tuple[str, ...]], Path] = {}

    def compile_fixture(name: str, *flags: str) -> Path:
        key = (name, flags)
        if key not in cache:
            source = FIXTURES / name
            compiler = "g++" if source.suffix == ".cpp" else "gcc"
            binary = out_dir / f"{source.stem}{'_'.join(flags).replace('-', '_')}"
            opts = list(flags) or ["-O0"]
            subprocess.run([compiler, "-g", *opts, str(source), "-o", str(binary)], check=True)
            cache[key] = binary
        return cache[key]

    return compile_fixture
