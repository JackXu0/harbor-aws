"""Runs in the EKS control pod; bridges the Harbor client and EKS trial pods."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import resource
import secrets
import shlex
import ssl
import uuid
from dataclasses import dataclass, field

from aiohttp import web

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB
RUNNER_MAX_FRAME_BYTES = 6 * 1024 * 1024 * 1024  # 6 GiB


@dataclass
class _TrialConn:
    """One trial's state in the control server."""

    trial_id: str
    trial_token: str
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    runner_connected: asyncio.Event = field(default_factory=asyncio.Event)
    inflight: dict[str, asyncio.Future] = field(default_factory=dict)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_task: asyncio.Task | None = None
    closed: bool = False


class ControlServer:
    """In-VPC API gateway. One per cluster."""

    def __init__(self) -> None:
        self.host = "0.0.0.0"  # noqa: S104 — bind all in-VPC interfaces
        self.api_port = int(os.environ["HARBOR_CONTROL_PORT"])
        self.runner_port = int(os.environ["HARBOR_RUNNER_PORT"])
        self.bearer_token = os.environ["HARBOR_BEARER_TOKEN"]
        self.tls_cert_file = os.environ["HARBOR_TLS_CERT_FILE"]
        self.tls_key_file = os.environ["HARBOR_TLS_KEY_FILE"]
        self.trials: dict[str, _TrialConn] = {}
        self.trials_lock = asyncio.Lock()

    async def start(self) -> None:
        app = web.Application(client_max_size=MAX_PAYLOAD_BYTES)
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_post("/register", self._handle_register)
        app.router.add_post("/exec", self._handle_exec)
        app.router.add_post("/stop", self._handle_stop)
        self.api_runner = web.AppRunner(app, access_log=None)
        await self.api_runner.setup()
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(certfile=self.tls_cert_file, keyfile=self.tls_key_file)
        site = web.TCPSite(self.api_runner, self.host, self.api_port, ssl_context=ssl_ctx)
        await site.start()
        logger.info("control API listening on %s:%d (TLS)", self.host, self.api_port)
        
        self.trial_tcp_server = await asyncio.start_server(
            self._handle_runner_connection,
            self.host,
            self.runner_port,
            limit=RUNNER_MAX_FRAME_BYTES,
        )
        logger.info("trial TCP server listening on %s:%d", self.host, self.runner_port)

    async def stop(self) -> None:
        async with self.trials_lock:
            trials = list(self.trials.values())
            self.trials.clear()
        for t in trials:
            await self._close_trial_conn(t)
        self.trial_tcp_server.close()
        await self.trial_tcp_server.wait_closed()
        await self.api_runner.cleanup()

    async def __aenter__(self) -> ControlServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _handle_runner_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Accept an inbound runner connection, validate auth, register it."""
        peer = writer.get_extra_info("peername")
        try:
            frame_type = await read_frame_line(reader)
            if frame_type != "A":
                logger.warning("runner %s: bad first frame %r", peer, frame_type)
                await reject_runner(writer, "bad-first-frame")
                return
            token = await read_frame_line(reader)
            trial_id = await read_frame_line(reader)
        except ConnectionError as e:
            logger.warning("runner %s: auth read error: %s", peer, e)
            writer.close()
            return

        async with self.trials_lock:
            t = self.trials.get(trial_id)
            if t is None:
                logger.warning("runner %s: unknown trial_id %r", peer, trial_id)
                await reject_runner(writer, "unknown-trial")
                return
            if not secrets.compare_digest(t.trial_token, token):
                logger.warning("runner %s: bad token for trial %s", peer, trial_id)
                await reject_runner(writer, "bad-token")
                return
            if t.reader is not None:
                logger.warning("runner %s: trial %s already has a runner", peer, trial_id)
                await reject_runner(writer, "already-connected")
                return
            t.reader = reader
            t.writer = writer

        writer.write(b"OK\n")
        await writer.drain()
        logger.info("runner %s authenticated for trial %s", peer, trial_id)

        t.reader_task = asyncio.create_task(self._reader_loop(t))
        t.runner_connected.set()

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
                    await self._process_result_frame(t)
                elif frame_type in ("Q", ""):
                    pass  # pong or blank line, ignore
                else:
                    logger.warning("trial %s: unexpected runner frame: %r", t.trial_id, frame_type)
        except ConnectionError:
            pass  # expected end-of-life when the runner disconnects
        except asyncio.CancelledError:
            raise  # propagate so asyncio marks the task as cancelled
        except Exception:  # noqa: BLE001
            logger.exception("trial %s reader loop crashed", t.trial_id)
        finally:
            t.closed = True
            for fut in t.inflight.values():
                if not fut.done():
                    fut.set_exception(ConnectionError(f"trial {t.trial_id} disconnected"))
            t.inflight.clear()

    async def _process_result_frame(self, t: _TrialConn) -> None:
        """Parse the body of an R frame and resolve the matching in-flight Future."""
        assert t.reader is not None
        cmd_id = await read_frame_line(t.reader)
        rc_str = await read_frame_line(t.reader)
        b64_out = await read_frame_line(t.reader)
        b64_err = await read_frame_line(t.reader)
        try:
            rc = int(rc_str)
        except ValueError:
            logger.warning(
                "trial %s cmd %s: malformed rc %r, treating as failure",
                t.trial_id, cmd_id, rc_str,
            )
            rc = 1
        stdout = base64.b64decode(b64_out).decode("utf-8", errors="replace") if b64_out else ""
        stderr = base64.b64decode(b64_err).decode("utf-8", errors="replace") if b64_err else ""
        fut = t.inflight.pop(cmd_id, None)
        if fut is not None and not fut.done():
            fut.set_result((stdout, stderr, rc))

    async def _close_trial_conn(self, t: _TrialConn) -> None:
        t.closed = True
        try:
            if t.writer is not None:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    t.writer.write(b"S\n")
                    await t.writer.drain()
                with contextlib.suppress(ConnectionError, RuntimeError):
                    t.writer.close()
                    await t.writer.wait_closed()
            if t.reader_task is not None and not t.reader_task.done():
                t.reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t.reader_task
        except Exception:  # noqa: BLE001
            logger.exception("trial %s: cleanup failed unexpectedly", t.trial_id)

    def _check_admin(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return secrets.compare_digest(auth[7:], self.bearer_token)

    async def _handle_healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "trials": len(self.trials)})

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
        if not trial_id or not token:
            return web.json_response({"error": "missing trial_id or token"}, status=400)
        connect_timeout = float(body.get("connect_timeout", 600))

        t = _TrialConn(trial_id=trial_id, trial_token=token)
        async with self.trials_lock:
            if trial_id in self.trials:
                return web.json_response({"error": "trial already registered"}, status=409)
            self.trials[trial_id] = t
        logger.info("register: pre-registered trial %s, waiting for runner...", trial_id)

        try:
            await asyncio.wait_for(t.runner_connected.wait(), timeout=connect_timeout)
        except TimeoutError:
            async with self.trials_lock:
                self.trials.pop(trial_id, None)
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
        if not trial_id or not cmd:
            return web.json_response({"error": "missing trial_id or cmd"}, status=400)
        try:
            timeout_sec = int(body.get("timeout_sec", 300))
        except (TypeError, ValueError):
            return web.json_response({"error": "timeout_sec must be an integer"}, status=400)

        t = self.trials.get(trial_id)
        if t is None:
            return web.json_response(
                {"error": "unknown trial", "reason": "not_registered"}, status=404,
            )
        if t.closed:
            return web.json_response(
                {"error": "trial closed", "reason": "runner_disconnected"}, status=404,
            )

        wrapped = _wrap_command(cmd, cwd=cwd, env=env)
        b64_cmd = base64.b64encode(wrapped.encode("utf-8")).decode("ascii")

        cmd_id = uuid.uuid4().hex[:16]
        fut: asyncio.Future = asyncio.Future()
        t.inflight[cmd_id] = fut
        try:
            try:
                async with t.write_lock:
                    assert t.writer is not None
                    t.writer.write(f"E\n{cmd_id}\n{timeout_sec}\n{b64_cmd}\n".encode())
                    await t.writer.drain()
            except Exception as e:  # noqa: BLE001
                return web.json_response({"error": f"write failed: {e}"}, status=502)
            try:
                stdout, stderr, rc = await asyncio.wait_for(fut, timeout=timeout_sec + 30)
            except TimeoutError:
                return web.json_response({"stdout": "", "stderr": "control-server timeout", "rc": 124})
            except ConnectionError as e:
                return web.json_response({"error": f"runner disconnected: {e}"}, status=502)
            return web.json_response({"stdout": stdout, "stderr": stderr, "rc": rc})
        finally:
            # Covers write-failure, timeout, and disconnect paths. Success path
            # already popped via _process_result_frame, so this is a no-op then.
            t.inflight.pop(cmd_id, None)

    async def _handle_stop(self, request: web.Request) -> web.Response:
        if not self._check_admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        trial_id = body.get("trial_id")
        async with self.trials_lock:
            t = self.trials.pop(trial_id, None)
        if t is None:
            return web.json_response({"ok": True, "note": "not registered"})
        await self._close_trial_conn(t)
        return web.json_response({"ok": True})


def _wrap_command(cmd: str, *, cwd: str | None, env: dict[str, str] | None) -> str:
    """Inline cwd and env into the command since the runner uses per-call subshells."""
    prefix = ""
    if env:
        prefix += " ".join(f"export {k}={shlex.quote(v)};" for k, v in env.items()) + " "
    if cwd:
        prefix += f"cd {shlex.quote(cwd)} && "
    return prefix + cmd

def raise_fd_limit() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = min(hard if hard != resource.RLIM_INFINITY else 1048576, 1048576)
    if target > soft:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        logger.info("RLIMIT_NOFILE %d -> %d", soft, target)


async def read_frame_line(reader: asyncio.StreamReader) -> str:
    """Read one newline-terminated wire-protocol line."""
    return (await reader.readline()).decode().rstrip("\n")


async def reject_runner(writer: asyncio.StreamWriter, kind: str) -> None:
    """Send a structured FAIL frame to the runner and close the socket cleanly."""
    writer.write(f"FAIL\n{kind}\n".encode())
    await writer.drain()
    writer.close()


async def async_main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raise_fd_limit()
    async with ControlServer() as server:
        logger.info("control server bearer token: %s", server.bearer_token)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(async_main())
