"""harbor-aws control server — in-VPC bridge between Mac and Fargate pods.

Runs as a single Deployment in the harbor-eks cluster, exposed via a Service
of type LoadBalancer (NLB). Two roles:

1. HTTPS server (aiohttp) for the Mac:
     POST /register   {trial_id, pod_ip, pod_port, token}
     POST /exec       {trial_id, cmd, cwd, env, timeout_sec}
     POST /stop       {trial_id}
     GET  /healthz

2. TCP client to runner pods:
     For each registered trial, opens ONE direct TCP connection from this pod
     to the trial pod's IP:port (in-VPC, no apiserver involved). Authenticates
     with the trial token. Holds the connection open for the trial's lifetime.
     Each /exec from the Mac sends an exec frame on that socket and awaits
     the matching result frame.

Why this exists: the Mac can't dial pod IPs directly (corporate network has
no route into the EKS VPC). The control server lives inside the VPC where
pod IPs ARE routable, so it acts as a small in-cluster gateway. The Mac
talks to ONE public NLB endpoint over plain HTTPS; the gateway talks to the
2492 trial pods over native VPC TCP.

Failure mode: every command in the data path is now Mac->NLB->server->pod_ip,
all of which are reliable. The fragile apiserver->kubelet TLS dial is
eliminated. The apiserver is only used for create_pod / delete_pod (cheap,
stateless).

This module is self-contained — no harbor-aws imports — so it can be packaged
into a small Docker image and deployed independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import secrets
import struct
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


def _raise_fd_limit() -> None:
    """Raise the per-process file descriptor soft limit to the hard limit.

    Each in-flight trial uses several FDs: incoming HTTP connection from the Mac,
    outgoing TCP connection to the runner pod, plus aiohttp/asyncio bookkeeping.
    The default soft limit in containers is 1024, which we exhaust around ~500
    concurrent trials. Set the soft limit to the hard limit so we can handle
    the full 2492+ concurrent trials we run from harbor.
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


# --- length-prefixed framing on the runner-side socket ----------------------


async def _read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = await reader.read(n - len(buf))
        if not chunk:
            raise ConnectionError("runner closed connection")
        buf.extend(chunk)
    return bytes(buf)


async def recv_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await _read_exactly(reader, 4)
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > 64 * 1024 * 1024:
        raise ValueError(f"invalid frame length {length}")
    payload = await _read_exactly(reader, length)
    return json.loads(payload.decode("utf-8"))  # type: ignore[no-any-return]


def encode_frame(msg: dict[str, Any]) -> bytes:
    payload = json.dumps(msg).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


# --- per-trial connection state ---------------------------------------------


@dataclass
class _TrialConn:
    trial_id: str
    pod_ip: str
    pod_port: int
    token: str
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    # in-flight exec calls keyed by cmd_id, awaiting result
    inflight: dict[str, asyncio.Future] = field(default_factory=dict)
    # one-shot lock to serialize writes (each exec writes a frame, the reader task
    # demultiplexes results to the right Future based on cmd_id)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_task: asyncio.Task | None = None
    closed: bool = False


# --- the control server -----------------------------------------------------


class ControlServer:
    """In-VPC API gateway. One per cluster."""

    def __init__(
        self,
        host: str = "0.0.0.0",  # noqa: S104 — must be reachable from the Mac
        port: int = 8443,
        admin_token: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._admin_token = admin_token or os.environ.get("HARBOR_ADMIN_TOKEN") or secrets.token_urlsafe(32)
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._trials: dict[str, _TrialConn] = {}
        self._trials_lock = asyncio.Lock()

    @property
    def admin_token(self) -> str:
        return self._admin_token

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._app = web.Application(client_max_size=64 * 1024 * 1024)
        self._app.router.add_get("/healthz", self._handle_healthz)
        self._app.router.add_post("/register", self._handle_register)
        self._app.router.add_post("/exec", self._handle_exec)
        self._app.router.add_post("/stop", self._handle_stop)
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info("control server listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        async with self._trials_lock:
            trials = list(self._trials.values())
            self._trials.clear()
        for t in trials:
            await self._close_trial_conn(t)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._site = None
        self._app = None

    # --- per-trial connection management ---

    async def _open_trial_conn(self, t: _TrialConn, connect_timeout: float = 30.0) -> None:
        """Open a TCP connection from this control-server pod to the runner pod.

        Bounded by connect_timeout so a bad pod_ip can't hang the request forever.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(t.pod_ip, t.pod_port),
                timeout=connect_timeout,
            )
        except TimeoutError as e:
            raise TimeoutError(
                f"open_connection({t.pod_ip}:{t.pod_port}) timed out after {connect_timeout}s"
            ) from e
        t.reader = reader
        t.writer = writer
        # Send auth frame, await auth_ok — also bounded so a misbehaving runner can't hang us
        writer.write(encode_frame({"type": "auth", "token": t.token}))
        try:
            await asyncio.wait_for(writer.drain(), timeout=10.0)
            msg = await asyncio.wait_for(recv_frame(reader), timeout=10.0)
        except TimeoutError as e:
            raise TimeoutError(f"auth handshake with {t.pod_ip}:{t.pod_port} timed out") from e
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"auth failed for {t.trial_id}: {msg}")
        # Spawn the reader task that demuxes result frames to in-flight Futures
        t.reader_task = asyncio.create_task(self._reader_loop(t))

    async def _reader_loop(self, t: _TrialConn) -> None:
        try:
            while True:
                msg = await recv_frame(t.reader)  # type: ignore[arg-type]
                if msg.get("type") == "result":
                    cmd_id = msg.get("id", "")
                    fut = t.inflight.pop(cmd_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(
                            (msg.get("stdout", ""), msg.get("stderr", ""), int(msg.get("rc", 1)))
                        )
                elif msg.get("type") == "pong":
                    pass
                else:
                    logger.warning("trial %s: unexpected runner frame: %s", t.trial_id, msg)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("trial %s reader loop error: %s", t.trial_id, e)
        finally:
            t.closed = True
            # Fail any in-flight commands
            for fut in t.inflight.values():
                if not fut.done():
                    fut.set_exception(ConnectionError(f"trial {t.trial_id} disconnected"))
            t.inflight.clear()

    async def _close_trial_conn(self, t: _TrialConn) -> None:
        t.closed = True
        if t.writer is not None:
            try:
                t.writer.write(encode_frame({"type": "shutdown"}))
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

    async def _handle_healthz(self, request: web.Request) -> web.Response:  # noqa: ARG002
        return web.json_response({"ok": True, "trials": len(self._trials)})

    async def _handle_register(self, request: web.Request) -> web.Response:
        if not self._check_admin(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        trial_id = body.get("trial_id")
        pod_ip = body.get("pod_ip")
        pod_port = int(body.get("pod_port", 8765))
        token = body.get("token")
        if not all([trial_id, pod_ip, token]):
            return web.json_response({"error": "missing trial_id, pod_ip, or token"}, status=400)

        t = _TrialConn(trial_id=trial_id, pod_ip=pod_ip, pod_port=pod_port, token=token)
        async with self._trials_lock:
            if trial_id in self._trials:
                return web.json_response({"error": "trial already registered"}, status=409)
            self._trials[trial_id] = t

        try:
            await self._open_trial_conn(t)
        except Exception as e:  # noqa: BLE001
            async with self._trials_lock:
                self._trials.pop(trial_id, None)
            return web.json_response({"error": f"connect failed: {e}"}, status=502)
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

        cmd_id = uuid.uuid4().hex[:16]
        fut: asyncio.Future = asyncio.Future()
        t.inflight[cmd_id] = fut
        try:
            async with t.write_lock:
                assert t.writer is not None
                t.writer.write(
                    encode_frame(
                        {
                            "type": "exec",
                            "id": cmd_id,
                            "cmd": cmd,
                            "cwd": cwd,
                            "env": env,
                            "timeout_sec": timeout_sec,
                        }
                    )
                )
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


# --- entry point for the Docker image ---------------------------------------


async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _raise_fd_limit()
    port = int(os.environ.get("HARBOR_CONTROL_PORT", "8443"))
    server = ControlServer(host="0.0.0.0", port=port)  # noqa: S104
    await server.start()
    logger.info("control server admin token: %s", server.admin_token)
    # Run forever
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(_amain())
