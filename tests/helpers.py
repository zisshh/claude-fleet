"""Shared test plumbing: repo on sys.path, fake CLIs on PATH, temp targets."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / "fakes"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

for fake in FAKES.iterdir():
    fake.chmod(0o755)


def fake_env(**extra: str) -> dict:
    """Environment with the fake cmux / claude / cursor-agent first on PATH."""
    env = dict(os.environ)
    env["PATH"] = f"{FAKES}{os.pathsep}{env.get('PATH', '')}"
    env.update(extra)
    return env


class TempDirs:
    """A temp root with a target project dir whose name needs shell quoting."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        # resolve(): on macOS the temp dir lives under /var -> /private/var, and
        # the spawner resolves --cwd, so unresolved paths never match its output.
        self.root = Path(self._tmp.name).resolve()
        self.target = self.root / "Div's Second Brain" / "plugin project"
        self.target.mkdir(parents=True)
        self.dump = self.root / "dump"
        self.dump.mkdir()

    def cleanup(self):
        self._tmp.cleanup()
