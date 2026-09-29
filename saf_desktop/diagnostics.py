"""Minimal local diagnostics; never collect environment variables or trading data."""
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from . import __version__


def write_incident(exc_type, exc_value, exc_tb, directory=None):
    # Deliberately exclude exception messages, source lines and local variables.
    frames = []
    while exc_tb is not None:
        code = exc_tb.tb_frame.f_code
        frames.append({"file": Path(code.co_filename).name, "line": exc_tb.tb_lineno, "function": code.co_name})
        exc_tb = exc_tb.tb_next
    root = Path(directory) if directory is not None else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SAFEngine" / "incidents"
    root.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "app_version": __version__, "timestamp": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(), "system": platform.system(), "exception_type": exc_type.__name__, "frames": frames}
    target = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex + ".json")
    with target.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    return target


def install_exception_hook():
    previous = sys.excepthook
    def hook(exc_type, exc_value, exc_tb):
        try:
            write_incident(exc_type, exc_value, exc_tb)
        except Exception:
            pass
        previous(exc_type, exc_value, exc_tb)
    sys.excepthook = hook
