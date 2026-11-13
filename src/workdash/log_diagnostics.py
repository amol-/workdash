"""Safe exception details for server diagnostics."""

import json
import re
import subprocess
import traceback


def safe_error(error: BaseException) -> str:
    """Describe a failure and where it happened without logging external command output.

    Messages are kept unless the failure came from an external command, whose error
    text carries the command's own output.
    """

    chain = []
    while error is not None:
        chain.append(error)
        error = error.__cause__
    command_failed = any(isinstance(item, subprocess.CalledProcessError) for item in chain)
    details = []
    for item in chain:
        detail = type(item).__name__
        if isinstance(item, json.JSONDecodeError):
            detail += f": {item.msg} (line {item.lineno}, column {item.colno})"
        elif isinstance(item, subprocess.CalledProcessError):
            detail += f" (exit {item.returncode})"
            status = re.search(r"\bHTTP (\d{3})\b", item.stderr or "", re.IGNORECASE)
            if status:
                detail += f" HTTP {status[1]}"
        elif not command_failed:
            detail += f": {item}"
        frames = traceback.extract_tb(item.__traceback__)
        if frames:
            frame = frames[-1]
            detail += f" at {frame.name}:{frame.lineno}"
        details.append(detail)
    return " <- ".join(details)
