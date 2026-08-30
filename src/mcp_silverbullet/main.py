"""Bridge entry point.

T1 only ships a smoke-test print so ``python -m mcp_silverbullet`` and
``mcp-silverbullet`` (the console script) both succeed. The real
``MCPServer`` wiring lands in T4.
"""

from __future__ import annotations

import sys


def run() -> int:
    """Smoke-test entry point used by ``python -m mcp_silverbullet``.

    Returns 0 on success so the T1 done-when clause
    (``python -m mcp_silverbullet prints a hello and exits 0``) holds.
    """
    print("hello from mcp-silverbullet")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())