"""Run VoxPad's tests and build Python distributions on any supported OS."""

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run(*arguments: str) -> int:
    return subprocess.run([sys.executable, *arguments], cwd=ROOT).returncode


def test() -> int:
    # Audio tests otherwise skip when dependencies are absent. CI must run them.
    try:
        import codecpod  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as error:
        print(f"Audio test dependency missing: {error}", file=sys.stderr)
        print('Install dependencies with: python -m pip install -e ".[dev]"', file=sys.stderr)
        return 1
    return run("-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v")


def build() -> int:
    if importlib.util.find_spec("build") is None:
        print('Install build tools with: python -m pip install -e ".[dev]"', file=sys.stderr)
        return 1
    return run("-m", "build")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("test", "build", "check"))
    command = parser.parse_args().command
    if command in ("test", "check"):
        result = test()
        if result:
            return result
    if command in ("build", "check"):
        return build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
