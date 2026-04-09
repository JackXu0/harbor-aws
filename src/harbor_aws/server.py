"""harbor-aws control server — in-VPC bridge between Mac and Fargate pods.

Deployed as a single pod in the harbor-eks cluster. Exposes two ports:

  - **8443 (HTTPS API)**: served via the harbor-control-nlb LoadBalancer
    Service. The Mac-side adapter calls /register, /exec, /stop, /healthz.
  - **8444 (runner accept)**: served via the harbor-control ClusterIP
    Service. Trial pods (running runner.sh as PID 1) dial in here.

Reverse-runner direction. Previously the control server dialed pod IPs
over in-VPC TCP, which forced the runner image to ship a Python TCP
listener. Flipping the direction lets the runner be ~80 lines of bash
using built-in ``/dev/tcp`` — no Python or extra binaries required.

Wire protocol — newline-delimited frames. base64 on payloads avoids
newline collisions inside command/stdout content.

  Runner -> control:
    A\\n<token>\\n<trial_id>\\n              (auth, first frame)
    R\\n<cmd_id>\\n<rc>\\n<b64_stdout>\\n<b64_stderr>\\n   (result)
    Q\\n                                   (pong)

  Control -> runner:
    OK\\n                                  (auth ok)
    FAIL\\n<reason>\\n                      (auth fail)
    E\\n<cmd_id>\\n<timeout_sec>\\n<b64_cmd>\\n   (exec)
    P\\n                                   (ping, optional)
    S\\n                                   (shutdown)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import resource
import secrets
import uuid
from dataclasses import dataclass, field

from aiohttp import web

logger = logging.getLogger(__name__)


def _raise_fd_limit() -> None:
    """Raise the per-process file descriptor soft limit to the hard limit.

    Each in-flight trial uses several FDs: incoming HTTP connection from the Mac,
    inbound TCP connection from the runner pod, plus aiohttp/asyncio bookkeeping.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = hard if hard != resource.RLIM_INFINITY else 1048576
        if target > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            logger.info("raised RLIMIT_NOFILE %d -> %d", soft, target)
        else:
            logger.info("RLIMIT_NOFILE already at %d", soft)
    except (ValueError, OSError) as e:
        logger.warning("failed to raise RLIMIT_NOFILE: %s", e)


# --- per-trial connection state ---------------------------------------------


@dataclass
class _TrialConn:
    """One trial's state in the control server.

    Lifecycle:
      1. Created in the *pending* state by /register, waiting for a runner
         to dial in with the matching trial_id + token.
      2. When the runner connects and authenticates, ``ready_event`` fires,
         and ``reader``/``writer`` are populated.
      3. /exec calls write E frames to ``writer`` and await Futures placed
         in ``inflight``.
      4. The reader task pops Futures and resolves them when R frames arrive.
      5. /stop sends an S frame and closes the connection.
    """

    trial_id: str
    token: str
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    # In-flight exec calls keyed by cmd_id, awaiting their result frame.
    inflight: dict[str, asyncio.Future] = field(default_factory=dict)
    # Serializes writes; the single reader task demuxes results by cmd_id.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_task: asyncio.Task | None = None
    closed: bool = False


# --- the control server -----------------------------------------------------


class ControlServer:
    """In-VPC API gateway. One per cluster."""

    def __init__(
        self,
        host: str = "0.0.0.0",  # noqa: S104 — must be reachable from the Mac
        http_port: int = 8443,
        runner_port: int = 8444,
        admin_token: str | None = None,
    ) -> None:
        self._host = host
        self._http_port = http_port
        self._runner_port = runner_port
        self._admin_token = admin_token or os.environ.get("HARBOR_ADMIN_TOKEN") or secrets.token_urlsafe(32)
        self._app: web.Application | None = None
        self._http_runner: web.AppRunner | None = None
        self._http_site: web.TCPSite | None = None
        self._runner_server: asyncio.base_events.Server | None = None
        self._trials: dict[str, _TrialConn] = {}
        self._trials_lock = asyncio.Lock()

    @property
    def admin_token(self) -> str:
        return self._admin_token

    async def start(self) -> None:
        if self._http_runner is not None:
            return
        # HTTP API for harbor (Mac side)
        self._app = web.Application(client_max_size=64 * 1024 * 1024)
        self._app.router.add_get("/healthz", self._handle_healthz)
        self._app.router.add_post("/register", self._handle_register)
        self._app.router.add_post("/exec", self._handle_exec)
        self._app.router.add_post("/stop", self._handle_stop)
        self._http_runner = web.AppRunner(self._app, access_log=None)
        await self._http_runner.setup()
        self._http_site = web.TCPSite(self._http_runner, self._host, self._http_port)
        await self._http_site.start()
        logger.info("control HTTP API listening on %s:%d", self._host, self._http_port)

        # Reverse TCP listener for runner pods.
        # The default asyncio StreamReader line limit is 64 KB; we raise it
        # to 256 MB so a single base64-encoded result line (which can hold
        # tens of MB of stdout from upload_dir/download_dir tar payloads)
        # doesn't blow up readline().
        self._runner_server = await asyncio.start_server(
            self._handle_runner_connection,
            self._host,
            self._runner_port,
            limit=256 * 1024 * 1024,
        )
        logger.info("control runner-accept listening on %s:%d", self._host, self._runner_port)

    async def stop(self) -> None:
        async with self._trials_lock:
            trials = list(self._trials.values())
            self._trials.clear()
        for t in trials:
            await self._close_trial_conn(t)
        if self._runner_server is not None:
            self._runner_server.close()
            await self._runner_server.wait_closed()
            self._runner_server = None
        if self._http_runner is not None:
            await self._http_runner.cleanup()
            self._http_runner = None
        self._http_site = None
        self._app = None

    # --- runner connection handling ---

    async def _handle_runner_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Accept an inbound runner connection, validate auth, register it."""
        peer = writer.get_extra_info("peername")
        try:
            # Read auth frame: A\n<token>\n<trial_id>\n
            frame_type = (await asyncio.wait_for(reader.readline(), timeout=10.0)).decode().rstrip("\n")
            if frame_type != "A":
                logger.warning("runner %s: bad first frame %r", peer, frame_type)
                writer.write(b"FAIL\nbad-first-frame\n")
                await writer.drain()
                writer.close()
                return
            token = (await asyncio.wait_for(reader.readline(), timeout=10.0)).decode().rstrip("\n")
            trial_id = (await asyncio.wait_for(reader.readline(), timeout=10.0)).decode().rstrip("\n")
        except (TimeoutError, ConnectionError) as e:
            logger.warning("runner %s: auth read error: %s", peer, e)
            writer.close()
            return

        async with self._trials_lock:
            t = self._trials.get(trial_id)
            if t is None:
                logger.warning("runner %s: unknown trial_id %r", peer, trial_id)
                writer.write(b"FAIL\nunknown-trial\n")
                await writer.drain()
                writer.close()
                return
            if not secrets.compare_digest(t.token, token):
                logger.warning("runner %s: bad token for trial %s", peer, trial_id)
                writer.write(b"FAIL\nbad-token\n")
                await writer.drain()
                writer.close()
                return
            if t.reader is not None:
                logger.warning("runner %s: trial %s already has a runner", peer, trial_id)
                writer.write(b"FAIL\nalready-connected\n")
                await writer.drain()
                writer.close()
                return
            t.reader = reader
            t.writer = writer

        writer.write(b"OK\n")
        await writer.drain()
        logger.info("runner %s authenticated for trial %s", peer, trial_id)

        # Start the reader task, then signal /register that we're ready.
        t.reader_task = asyncio.create_task(self._reader_loop(t))
        t.ready_event.set()

    async def _reader_loop(self, t: _TrialConn) -> None:
        """Read result/pong frames and demux to in-flight Futures."""
        try:
            assert t.reader is not None
            while True:
                line = await t.reader.readline()
                if not line:
                    raise ConnectionError("runner closed connection")
                frame_type = line.decode().rstrip("\n")
                if frame_type == "R":
                    cmd_id = (await t.reader.readline()).decode().rstrip("\n")
                    rc_str = (await t.reader.readline()).decode().rstrip("\n")
                    b64_out = (await t.reader.readline()).decode().rstrip("\n")
                    b64_err = (await t.reader.readline()).decode().rstrip("\n")
                    try:
                        rc = int(rc_str)
                    except ValueError:
                        rc = 1
                    stdout = base64.b64decode(b64_out).decode("utf-8", errors="replace") if b64_out else ""
                    stderr = base64.b64decode(b64_err).decode("utf-8", errors="replace") if b64_err else ""
                    fut = t.inflight.pop(cmd_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result((stdout, stderr, rc))
                elif frame_type == "Q":
                    pass  # pong, ignore
                elif frame_type == "":
                    pass  # blank line
                else:
                    logger.warning("trial %s: unexpected runner frame: %r", t.trial_id, frame_type)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("trial %s reader loop error: %s", t.trial_id, e)
        finally:
            t.closed = True
            for fut in t.inflight.values():
                if not fut.done():
                    fut.set_exception(ConnectionError(f"trial {t.trial_id} disconnected"))
            t.inflight.clear()

    async def _close_trial_conn(self, t: _TrialConn) -> None:
        t.closed = True
        if t.writer is not None:
            try:
                t.writer.write(b"S\n")
                await t.writer.drain()
            except Exception:  # noqa: BLE001
                pass
            try:
                t.writer.close()
                await t.writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        if t.reader_task is not None and not t.reader_task.done():
            t.reader_task.cancel()
            try:
                await t.reader_task
            except Exception:  # noqa: BLE001
                pass

    # --- HTTP handlers (Mac -> control server) ---

    def _check_admin(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return secrets.compare_digest(auth[7:], self._admin_token)

    async def _handle_healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "trials": len(self._trials)})

    async def _handle_register(self, request: web.Request) -> web.Response:
        """Pre-register a trial and wait for the runner to dial in."""
        if not self._check_admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        trial_id = body.get("trial_id")
        token = body.get("token")
        connect_timeout = float(body.get("connect_timeout", 120.0))
        if not trial_id or not token:
            return web.json_response({"error": "missing trial_id or token"}, status=400)

        t = _TrialConn(trial_id=trial_id, token=token)
        async with self._trials_lock:
            if trial_id in self._trials:
                return web.json_response({"error": "trial already registered"}, status=409)
            self._trials[trial_id] = t
        logger.info("register: pre-registered trial %s, waiting for runner...", trial_id)

        try:
            await asyncio.wait_for(t.ready_event.wait(), timeout=connect_timeout)
        except TimeoutError:
            async with self._trials_lock:
                self._trials.pop(trial_id, None)
            logger.warning("register: trial %s timed out after %.0fs", trial_id, connect_timeout)
            return web.json_response(
                {"error": f"runner did not connect within {connect_timeout}s"}, status=504
            )
        logger.info("register: trial %s ready", trial_id)
        return web.json_response({"ok": True})

    async def _handle_exec(self, request: web.Request) -> web.Response:
        if not self._check_admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        trial_id = body.get("trial_id")
        cmd = body.get("cmd")
        cwd = body.get("cwd")
        env = body.get("env") or None
        timeout_sec = int(body.get("timeout_sec", 300))
        if not trial_id or not cmd:
            return web.json_response({"error": "missing trial_id or cmd"}, status=400)

        t = self._trials.get(trial_id)
        if t is None or t.closed:
            return web.json_response({"error": "unknown trial"}, status=404)

        # Wrap cwd / env into the shell command since the bash runner uses
        # per-call subshells (no persistent state across exec calls).
        wrapped = _wrap_command(cmd, cwd=cwd, env=env)
        b64_cmd = base64.b64encode(wrapped.encode("utf-8")).decode("ascii")

        cmd_id = uuid.uuid4().hex[:16]
        fut: asyncio.Future = asyncio.Future()
        t.inflight[cmd_id] = fut
        try:
            async with t.write_lock:
                assert t.writer is not None
                t.writer.write(f"E\n{cmd_id}\n{timeout_sec}\n{b64_cmd}\n".encode())
                await t.writer.drain()
        except Exception as e:  # noqa: BLE001
            t.inflight.pop(cmd_id, None)
            return web.json_response({"error": f"write failed: {e}"}, status=502)

        try:
            stdout, stderr, rc = await asyncio.wait_for(fut, timeout=timeout_sec + 30)
        except TimeoutError:
            t.inflight.pop(cmd_id, None)
            return web.json_response(
                {"stdout": "", "stderr": "control-server timeout", "rc": 124}
            )
        except ConnectionError as e:
            return web.json_response({"error": f"runner disconnected: {e}"}, status=502)
        return web.json_response({"stdout": stdout, "stderr": stderr, "rc": rc})

    async def _handle_stop(self, request: web.Request) -> web.Response:
        if not self._check_admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        trial_id = body.get("trial_id")
        async with self._trials_lock:
            t = self._trials.pop(trial_id, None)
        if t is None:
            return web.json_response({"ok": True, "note": "not registered"})
        await self._close_trial_conn(t)
        return web.json_response({"ok": True})


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _wrap_command(cmd: str, *, cwd: str | None, env: dict[str, str] | None) -> str:
    """Inline cwd and env so the runner's per-call subshell sees them."""
    parts: list[str] = []
    if env:
        for k, v in env.items():
            parts.append(f"export {k}={_shell_quote(v)};")
    if cwd:
        parts.append(f"cd {_shell_quote(cwd)} &&")
    parts.append(cmd)
    return " ".join(parts)


# --- entry point for the Docker image ---------------------------------------


async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _raise_fd_limit()
    http_port = int(os.environ.get("HARBOR_CONTROL_PORT", "8443"))
    runner_port = int(os.environ.get("HARBOR_RUNNER_PORT", "8444"))
    server = ControlServer(host="0.0.0.0", http_port=http_port, runner_port=runner_port)  # noqa: S104
    await server.start()
    logger.info("control server admin token: %s", server.admin_token)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(_amain())
