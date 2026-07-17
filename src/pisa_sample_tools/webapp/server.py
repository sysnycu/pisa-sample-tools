from __future__ import annotations

import os
import socket
import tempfile
import threading
import webbrowser
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from .app import create_app


def run_server(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    report_roots: Sequence[Path] | None = None,
    results_roots: Sequence[Path] | None = None,
    config: Path | None = None,
    local_config: Path | None = None,
    state_path: Path | None = None,
    frontend_dir: Path | None = None,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "PISA Analysis Workbench only supports loopback hosts because runner and "
            "repair actions are privileged local operations"
        )
    selected_port = port or _available_port(host)
    if state_path is None:
        state_path = _default_state_path()
    app = create_app(
        report_roots=report_roots,
        results_roots=results_roots,
        config=config,
        local_config=local_config,
        state_path=state_path,
        frontend_dir=frontend_dir,
    )
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    url = f"http://{browser_host}:{selected_port}/ui/"
    print(f"PISA Analysis Workbench: {url}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=selected_port, log_level="warning")


def _available_port(host: str) -> int:
    bind_host = "127.0.0.1" if host in {"0.0.0.0", "::", "localhost"} else host
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    with socket.socket(family) as sock:
        sock.bind((bind_host, 0))
        return int(sock.getsockname()[1])


def _default_state_path() -> Path:
    state_root = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    )
    candidates = [
        state_root / "pisa-analysis-tools",
        Path.cwd() / "analysis" / ".state",
        Path(tempfile.gettempdir()) / f"pisa-analysis-tools-{os.getuid()}",
    ]
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory):
                pass
        except OSError:
            continue
        return directory / "webapp.sqlite"
    raise OSError("no writable location is available for the workbench job database")
