"""Frozen-build entry point (see qslcard.spec)."""

from __future__ import annotations

from qslcard.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
