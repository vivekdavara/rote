"""Run the CoreOne mock in a background thread (for tests, the spike and the demo)."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn

from mockapp.app import create_app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class MockServer:
    port: int
    server: uvicorn.Server
    thread: threading.Thread

    def base_url(self, tenant: str = "harbor") -> str:
        return f"http://{tenant}.localhost:{self.port}"

    def _admin(self, method: str, path: str, tenant: str = "harbor", **kwargs: Any) -> Any:
        # The admin API is localhost-only; talk to 127.0.0.1 and route the tenant by Host.
        response = httpx.request(
            method, f"http://127.0.0.1:{self.port}{path}", headers={"host": f"{tenant}.localhost"}, **kwargs
        )
        response.raise_for_status()
        return response.json()

    def fault(self, fault: str, *, tenant: str = "harbor", page: str | None = None, times: int = 1, ms: int = 0,
              message: str | None = None) -> None:
        self._admin("POST", "/__faults", tenant,
                    json={"fault": fault, "page": page, "times": times, "ms": ms, "message": message})

    def clear_faults(self) -> None:
        self._admin("DELETE", "/__faults")

    def reset(self) -> None:
        self._admin("POST", "/__reset")

    def state(self) -> dict[str, Any]:
        result: dict[str, Any] = self._admin("GET", "/__state")
        return result

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def start_mock(seed: int = 7, *, faults: bool = True, port: int | None = None) -> MockServer:
    port = port or free_port()
    config = uvicorn.Config(create_app(seed=seed, faults_enabled=faults), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock app did not start")
        time.sleep(0.02)
    return MockServer(port=port, server=server, thread=thread)
