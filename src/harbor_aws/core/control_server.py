"""Harbor-aws control server — accepts outbound connections from pod runners.

This is the orchestrator-side counterpart to harbor_aws.runner.runner. It runs
a small aiohttp HTTP server inside the harbor-aws process. Pods dial *out* to
this server (instead of harbor-aws dialing *in* via kubectl exec), so we
bypass the apiserver -> kubelet TLS proxy entirely.

Lifecycle:
  1. AWSEnvironment.start() lazily starts the control server (once per process)
  2. For each trial, the adapter calls server.reserve_session(session_id, token).
     This returns a SessionHandle and registers the session as "pending".
  3. The adapter creates the pod with HARBOR_CONTROL_URL, HARBOR_SESSION_ID,
     HARBOR_TOKEN env vars set.
  4. Pod runner calls POST /register with its session_id+token. The matching
     pending SessionHandle is moved to "registered" state and its
     wait_for_register() future fires.
  5. The adapter awaits handle.wait_for_register(), then can call handle.exec()
     to dispatch commands. Each exec() puts a command on the session's queue
     and awaits a result Future.
  6. The pod runner long-polls /next-command, runs each command in its bash
     subprocess, posts results back via /result.
  7. On stop(), the adapter calls handle.close() which removes the session
     and (optionally) tells the pod to exit.

This module is self-contained — it doesn't import anything from harbor-aws,
so it can be used standalone for testing.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# How long the server holds a /next-command poll open before returning 204.
# Pods will immediately re-poll, so this just bounds idle latency.
LONG_POLL_TIMEOUT_SEC = 30


@dataclass
class _PendingCommand:
    cmd_id: str
    cmd: str
    cwd: str | None
    env: dict[str, str] | None
    timeout_sec: int
    result: asyncio.Future = field(default_factory=asyncio.Future)


class SessionHandle:
    """Per-trial state on the orchestrator side.

    The adapter holds one SessionHandle per pod. Use:
        await handle.wait_for_register(timeout=...)
        out, err, rc = await handle.exec("ls /tmp", timeout_sec=10)
        await handle.close()
    """

    def __init__(self, session_id: str, token: str) -> None:
        self.session_id = session_id
        self.token = token
        self._registered = asyncio.Event()
        self._closed = asyncio.Event()
        # Commands waiting to be picked up by the pod's long-poll.
        self._command_queue: asyncio.Queue[_PendingCommand] = asyncio.Queue()
        # In-flight commands keyed by cmd_id, awaiting result from the pod.
        self._inflight: dict[str, _PendingCommand] = {}
        self._lock = asyncio.Lock()

    # --- adapter-facing API ---

    async def wait_for_register(self, timeout: float = 600.0) -> None:
        """Wait until the pod has called POST /register for this session."""
        try:
            await asyncio.wait_for(self._registered.wait(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"Pod {self.session_id} did not register within {timeout}s"
            ) from e

    async def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int = 300,
    ) -> tuple[str, str, int]:
        """Send a command to the pod and await its result."""
        if not self._registered.is_set():
            raise RuntimeError(f"Session {self.session_id} not yet registered")
        if self._closed.is_set():
            raise RuntimeError(f"Session {self.session_id} already closed")

        pending = _PendingCommand(
            cmd_id=uuid.uuid4().hex[:16],
            cmd=cmd,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
        )
        async with self._lock:
            self._inflight[pending.cmd_id] = pending
        await self._command_queue.put(pending)

        try:
            # Allow a small slack on top of the runner's timeout — if the runner
            # times out it returns rc=124, but the network round-trip might add a
            # few seconds.
            return await asyncio.wait_for(pending.result, timeout=timeout_sec + 30)
        except asyncio.TimeoutError:
            async with self._lock:
                self._inflight.pop(pending.cmd_id, None)
            return ("", f"server-side timeout waiting for runner result after {timeout_sec + 30}s", 124)

    async def close(self) -> None:
        """Mark the session closed. Outstanding pollers receive 410."""
        self._closed.set()
        # Fail any in-flight commands so callers don't hang
        async with self._lock:
            for pending in self._inflight.values():
                if not pending.result.done():
                    pending.result.set_result(("", "session closed", 1))
            self._inflight.clear()

    # --- server-internal API ---

    def _on_register(self) -> None:
        self._registered.set()

    async def _on_long_poll(self) -> _PendingCommand | None:
        """Called from the GET /next-command handler. Returns None on idle timeout."""
        if self._closed.is_set():
            return None
        try:
            return await asyncio.wait_for(
                self._command_queue.get(), timeout=LONG_POLL_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            return None

    async def _on_result(
        self, cmd_id: str, stdout: str, stderr: str, rc: int
    ) -> bool:
        async with self._lock:
            pending = self._inflight.pop(cmd_id, None)
        if pending is None:
            return False  # unknown / already-resolved cmd
        if not pending.result.done():
            pending.result.set_result((stdout, stderr, rc))
        return True


class ControlServer:
    """Aiohttp server that pod runners dial out to.

    Singleton-style: one instance per harbor-aws process. Use:

        srv = ControlServer()
        await srv.start()                       # binds and starts serving
        handle = srv.reserve_session(sid, tok)  # before creating the pod
        # ... create pod with HARBOR_CONTROL_URL=srv.base_url ...
        await handle.wait_for_register()
        out, err, rc = await handle.exec("...")
        await handle.close()
        await srv.stop()
    """

    def __init__(
        self,
        host: str = "0.0.0.0",  # noqa: S104 — intentional, server must be reachable from pods
        port: int = 0,
        public_url: str | None = None,
    ) -> None:
        """
        Args:
            host: bind interface
            port: bind port (0 = pick a free one)
            public_url: what we tell pods to dial. Required if running behind
              a tunnel/load-balancer where the bind address isn't reachable
              from outside. If None, we use http://{host}:{port}.
        """
        self._host = host
        self._port = port
        self._public_url = public_url
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._sessions: dict[str, SessionHandle] = {}
        self._sessions_lock = asyncio.Lock()
        self._actual_port: int | None = None

    @property
    def base_url(self) -> str:
        if self._public_url:
            return self._public_url.rstrip("/")
        if self._actual_port is None:
            raise RuntimeError("ControlServer not started")
        return f"http://{self._host}:{self._actual_port}"

    async def start(self) -> None:
        if self._runner is not None:
            return  # already started
        self._app = web.Application()
        self._app.router.add_post("/register", self._handle_register)
        self._app.router.add_get(
            "/next-command/{session_id}", self._handle_next_command
        )
        self._app.router.add_post(
            "/result/{session_id}/{cmd_id}", self._handle_result
        )
        self._app.router.add_post("/exit/{session_id}", self._handle_exit)
        self._app.router.add_get("/healthz", self._handle_healthz)

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

        # Read back the actual port if we asked for 0
        for sock in self._site._server.sockets if self._site._server else []:  # type: ignore[attr-defined]
            self._actual_port = sock.getsockname()[1]
            break
        if self._actual_port is None:
            self._actual_port = self._port

        logger.info("ControlServer listening on %s (public_url=%s)", self.base_url, self._public_url)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._site = None
        self._app = None

    def reserve_session(self, session_id: str, token: str | None = None) -> SessionHandle:
        """Pre-register a session before the pod starts. Returns a SessionHandle.

        Caller is expected to set HARBOR_SESSION_ID + HARBOR_TOKEN env vars on
        the pod, then await handle.wait_for_register().
        """
        if token is None:
            token = secrets.token_urlsafe(16)
        handle = SessionHandle(session_id, token)
        # Lock-free for the common case (sessions are unique per trial)
        if session_id in self._sessions:
            raise RuntimeError(f"session {session_id!r} already reserved")
        self._sessions[session_id] = handle
        return handle

    async def release_session(self, session_id: str) -> None:
        async with self._sessions_lock:
            handle = self._sessions.pop(session_id, None)
        if handle is not None:
            await handle.close()

    # --- HTTP handlers ---

    async def _handle_register(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        session_id = body.get("session_id")
        token = body.get("token")
        if not session_id or not token:
            return web.json_response({"error": "missing session_id or token"}, status=400)

        handle = self._sessions.get(session_id)
        if handle is None:
            return web.json_response({"error": "unknown session"}, status=404)
        if not secrets.compare_digest(handle.token, token):
            return web.json_response({"error": "bad token"}, status=403)
        if handle._closed.is_set():
            return web.json_response({"error": "session closed"}, status=410)

        handle._on_register()
        logger.debug("session %s registered", session_id)
        return web.json_response({"ok": True})

    async def _handle_next_command(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        token = request.query.get("token", "")
        handle = self._sessions.get(session_id)
        if handle is None:
            return web.json_response({"error": "unknown session"}, status=404)
        if not secrets.compare_digest(handle.token, token):
            return web.json_response({"error": "bad token"}, status=403)
        if handle._closed.is_set():
            return web.json_response({"error": "session closed"}, status=410)

        pending = await handle._on_long_poll()
        if pending is None:
            return web.Response(status=204)  # idle timeout, pod re-polls
        return web.json_response(
            {
                "id": pending.cmd_id,
                "cmd": pending.cmd,
                "cwd": pending.cwd,
                "env": pending.env,
                "timeout_sec": pending.timeout_sec,
            }
        )

    async def _handle_result(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        cmd_id = request.match_info["cmd_id"]
        token = request.query.get("token", "")
        handle = self._sessions.get(session_id)
        if handle is None:
            return web.json_response({"error": "unknown session"}, status=404)
        if not secrets.compare_digest(handle.token, token):
            return web.json_response({"error": "bad token"}, status=403)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        ok = await handle._on_result(
            cmd_id, body.get("stdout", ""), body.get("stderr", ""), int(body.get("rc", 1))
        )
        if not ok:
            return web.json_response({"error": "unknown cmd_id"}, status=404)
        return web.json_response({"ok": True})

    async def _handle_exit(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        token = request.query.get("token", "")
        handle = self._sessions.get(session_id)
        if handle is None:
            return web.json_response({"error": "unknown session"}, status=404)
        if not secrets.compare_digest(handle.token, token):
            return web.json_response({"error": "bad token"}, status=403)
        await handle.close()
        return web.json_response({"ok": True})

    async def _handle_healthz(self, request: web.Request) -> web.Response:  # noqa: ARG002
        return web.json_response({"ok": True, "sessions": len(self._sessions)})


# --- module-level singleton helper ------------------------------------------


_singleton: ControlServer | None = None
_singleton_lock = asyncio.Lock()


async def get_control_server(
    host: str = "0.0.0.0", port: int = 0, public_url: str | None = None  # noqa: S104
) -> ControlServer:
    """Get-or-create the process-wide control server singleton."""
    global _singleton
    async with _singleton_lock:
        if _singleton is None:
            _singleton = ControlServer(host=host, port=port, public_url=public_url)
            await _singleton.start()
    return _singleton
