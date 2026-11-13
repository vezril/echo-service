"""Console entry point (``python -m echo``)."""

from __future__ import annotations

import sys

_NOT_BUILT = (
    "echo-service is scaffolded but not implemented.\n"
    "The design and its verified cross-service constraints are in docs/DESIGN.md;\n"
    "the module skeleton under src/echo/ records what each piece owns.\n"
)


def main() -> int:
    """Report that the worker has no implementation yet.

    Deliberately fails rather than idling: a container that starts cleanly and does nothing
    reads as a healthy deployment, which this is not.
    """
    sys.stderr.write(_NOT_BUILT)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
