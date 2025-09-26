from __future__ import annotations

import os
import subprocess
from typing import Dict, Optional


def cmd(
    command: str,
    *,
    check: bool = False,
    timeout: Optional[float] = None,
    capture_output: bool = True,
    text: bool = True,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a plaintext shell command and return the CompletedProcess.

    Parameters
    - command: The shell command to run (exact string; no list splitting performed).
    - check: If True, raise CalledProcessError for non-zero exit code.
    - timeout: Optional seconds to wait before terminating the process.
    - capture_output: If True, capture stdout and stderr (default True).
    - text: Return text (str) for stdout/stderr instead of bytes (default True).
    - env: Optional environment vars to overlay on top of current os.environ.

    Returns
    - subprocess.CompletedProcess[str]: includes returncode, stdout, stderr.

    Behavior
    - Uses shell=True to allow commands like "vtysh -c 'show ip route'" to work
      as written on both Windows and POSIX.
    - Inherits current environment by default; pass `env` to override variables
      (merged with os.environ).
    """
    merged_env = None
    if env is not None:
        merged_env = os.environ.copy()
        merged_env.update(env)

    return subprocess.run(
        command,
        shell=True,
        check=check,
        timeout=timeout,
        capture_output=capture_output,
        text=text,
        env=merged_env,
    )


