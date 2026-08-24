"""Optional Darwin kernel memory-pressure telemetry."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from typing import Any

SysctlRunner = Callable[..., Any]


def query_macos_memory_pressure_level(
    *,
    os_name: str,
    runner: SysctlRunner = subprocess.run,
    timeout_seconds: float = 0.5,
    argv: Sequence[str] = (
        "/usr/sbin/sysctl",
        "-n",
        "kern.memorystatus_vm_pressure_level",
    ),
) -> int | None:
    """Return XNU's validated VM pressure level, or ``None`` on any failure.

    Darwin exposes levels 0 (normal), 1 (warning), 2 (urgent/warning), and
    3 (critical).  The interface is deliberately optional: unavailable tools,
    denied queries, timeouts, malformed output, and future values all select
    the portable fallback instead of breaking telemetry collection.
    """

    if os_name != "Darwin":
        return None
    try:
        result = runner(
            list(argv),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if int(getattr(result, "returncode", 1)) != 0:
        return None
    try:
        level = int(str(getattr(result, "stdout", "")).strip())
    except ValueError:
        return None
    return level if level in {0, 1, 2, 3} else None
