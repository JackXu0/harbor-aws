"""harbor-aws pod runner — TCP server, dial-in from control server.

Runs as PID 1 inside a Fargate pod. Listens on a TCP port. The harbor-control
server (which lives in the same VPC) opens a connection in, authenticates,
then pumps commands one at a time. The runner pipes each command into a
long-lived bash subprocess and writes the result back.

This replaces the kubectl-exec / apiserver-proxy path entirely:
  - kubectl create_pod      — apiserver call by harbor (cheap, reliable)
  - control_server -> pod   — direct in-VPC TCP, no apiserver involvement
  - kubectl delete_pod      — apiserver call by harbor (cheap, reliable)

Pure stdlib (no pip-installable dependencies) so the runner can run inside
any base image that has Python 3.8+. The harbor-aws adapter ships this file
into the pod via a ConfigMap volume mount.

Wire protocol — length-prefixed JSON over the TCP socket:

  Each frame is exactly 4 bytes of big-endian length followed by that many
  bytes of UTF-8 JSON. Length-prefixing avoids any ambiguity around stdout
  containing newlines or other delimiters.

  Frame types:
    {"type": "auth", "token": "..."}        (control -> runner, first frame)
    {"type": "auth_ok"}                     (runner -> control)
    {"type": "auth_fail", "reason": "..."}  (runner -> control, then close)
    {"type": "exec", "id": "...", "cmd": "...", "cwd": null,
     "env": {...}, "timeout_sec": 300}      (control -> runner)
    {"type": "result", "id": "...", "stdout": "...", "stderr": "...",
     "rc": N}                               (runner -> control)
    {"type": "shutdown"}                    (control -> runner, optional)
    {"type": "ping"}                        (control -> runner, idle keepalive)
    {"type": "pong"}                        (runner -> control)

Env vars (set by harbor-aws when creating the pod):
  HARBOR_TOKEN          shared secret. The control server must present this
                        in its first 'auth' frame.
  HARBOR_LISTEN_PORT    optional, defaults to 8765
"""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

# --- config from env ---------------------------------------------------------

TOKEN = os.environ.get("HARBOR_TOKEN", "")
LISTEN_PORT = int(os.environ.get("HARBOR_LISTEN_PORT", "8765"))
LISTEN_HOST = "0.0.0.0"  # noqa: S104 — must be reachable from the control server

if not TOKEN:
    sys.stderr.write("harbor-runner: missing HARBOR_TOKEN env\n")
    sys.exit(2)


# --- bash session ------------------------------------------------------------


class BashSession:
    """A long-lived bash subprocess. Commands are framed by a unique end token,
    same trick as harbor-aws's PersistentShell.
    """

    def __init__(self) -> None:
        self._proc = subprocess.Popen(  # noqa: S603 — running bash is the entire point
            ["bash"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,  # unbuffered — we read byte-by-byte
            text=False,
        )
        if self._proc.poll() is not None:
            raise RuntimeError("bash exited immediately")

    def run(
        self,
        cmd: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int = 300,
    ) -> tuple[str, str, int]:
        """Run a single command. Returns (stdout, stderr, return_code)."""
        if self._proc.poll() is not None:
            raise RuntimeError("bash subprocess exited")

        marker = uuid.uuid4().hex[:16]
        end_token = f"__HEND_{marker}__"

        parts: list[str] = []
        if env:
            for k, v in env.items():
                parts.append(f"export {k}={_shell_quote(v)};")
        if cwd:
            parts.append(f"cd {_shell_quote(cwd)} &&")
        parts.append(cmd)
        full_cmd = " ".join(parts)

        # Echo end token to BOTH stdout and stderr so we know both streams are drained.
        framed = f"{full_cmd}\n_rc=$?; echo \"{end_token} $_rc\"; echo \"{end_token}\" >&2\n"
        assert self._proc.stdin is not None
        self._proc.stdin.write(framed.encode("utf-8"))
        self._proc.stdin.flush()

        stdout_buf = b""
        stderr_buf = b""
        deadline = time.monotonic() + timeout_sec
        out_done = False
        err_done = False
        rc = 1

        import fcntl

        for stream in (self._proc.stdout, self._proc.stderr):
            assert stream is not None
            fd = stream.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        out_fd = self._proc.stdout.fileno()  # type: ignore[union-attr]
        err_fd = self._proc.stderr.fileno()  # type: ignore[union-attr]

        end_token_b = end_token.encode("ascii")

        while not (out_done and err_done):
            if time.monotonic() > deadline:
                return ("", f"command timed out after {timeout_sec}s", 124)

            try:
                chunk = os.read(out_fd, 65536)
                if chunk:
                    stdout_buf += chunk
            except BlockingIOError:
                pass
            try:
                chunk = os.read(err_fd, 65536)
                if chunk:
                    stderr_buf += chunk
            except BlockingIOError:
                pass

            if not out_done and end_token_b in stdout_buf:
                idx = stdout_buf.find(end_token_b)
                eol = stdout_buf.find(b"\n", idx)
                if eol != -1:
                    line = stdout_buf[idx:eol]
                    parts2 = line.decode("utf-8", errors="replace").split()
                    try:
                        rc = int(parts2[-1])
                    except (ValueError, IndexError):
                        rc = 1
                    stdout_buf = stdout_buf[:idx].rstrip(b"\n")
                    out_done = True
            if not err_done and end_token_b in stderr_buf:
                idx = stderr_buf.find(end_token_b)
                eol = stderr_buf.find(b"\n", idx)
                if eol != -1:
                    stderr_buf = stderr_buf[:idx].rstrip(b"\n")
                    err_done = True

            if not (out_done and err_done):
                time.sleep(0.01)

        return (
            stdout_buf.decode("utf-8", errors="replace"),
            stderr_buf.decode("utf-8", errors="replace"),
            rc,
        )

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(b"exit\n")
                self._proc.stdin.flush()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def _shell_quote(s: str) -> str:
    """Quote a string for shell substitution. Simple but safe."""
    return "'" + s.replace("'", "'\\''") + "'"


# --- length-prefixed framing -------------------------------------------------


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from sock. Raises ConnectionError if the peer closed."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection")
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock: socket.socket) -> dict:
    header = _recv_exact(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > 64 * 1024 * 1024:  # 64 MB hard cap
        raise ValueError(f"invalid frame length {length}")
    payload = _recv_exact(sock, length)
    return json.loads(payload.decode("utf-8"))


def send_frame(sock: socket.socket, msg: dict) -> None:
    payload = json.dumps(msg).encode("utf-8")
    header = struct.pack(">I", len(payload))
    sock.sendall(header + payload)


# --- main loop ---------------------------------------------------------------


_bash: BashSession | None = None
_listen_sock: socket.socket | None = None
_client_sock: socket.socket | None = None


def _shutdown_handler(signum, frame) -> None:  # noqa: ARG001
    sys.stderr.write(f"harbor-runner: signal {signum} received, shutting down\n")
    if _bash is not None:
        _bash.close()
    if _client_sock is not None:
        try:
            _client_sock.close()
        except Exception:
            pass
    if _listen_sock is not None:
        try:
            _listen_sock.close()
        except Exception:
            pass
    sys.exit(0)


def _serve_one_client(client: socket.socket) -> None:
    """Authenticate, then run the command loop until the client disconnects."""
    global _bash

    # Step 1: auth
    try:
        msg = recv_frame(client)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"harbor-runner: auth recv failed: {e}\n")
        return
    if msg.get("type") != "auth" or msg.get("token") != TOKEN:
        try:
            send_frame(client, {"type": "auth_fail", "reason": "bad token"})
        except Exception:
            pass
        sys.stderr.write("harbor-runner: auth failed\n")
        return
    send_frame(client, {"type": "auth_ok"})
    sys.stderr.write("harbor-runner: client authenticated\n")

    # Step 2: lazily start bash on first authenticated connection
    if _bash is None:
        _bash = BashSession()

    # Step 3: command loop
    while True:
        try:
            msg = recv_frame(client)
        except ConnectionError:
            sys.stderr.write("harbor-runner: client disconnected\n")
            return
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"harbor-runner: recv error: {e}\n")
            return

        mtype = msg.get("type")
        if mtype == "ping":
            send_frame(client, {"type": "pong"})
            continue
        if mtype == "shutdown":
            sys.stderr.write("harbor-runner: shutdown requested\n")
            return
        if mtype != "exec":
            sys.stderr.write(f"harbor-runner: unknown frame type {mtype!r}\n")
            continue

        cmd_id = msg.get("id", "")
        cmd = msg.get("cmd", "")
        cwd = msg.get("cwd")
        env = msg.get("env") or None
        timeout_sec = int(msg.get("timeout_sec", 300))

        if not cmd_id or not cmd:
            sys.stderr.write(f"harbor-runner: malformed exec frame: {msg}\n")
            continue

        try:
            stdout, stderr, rc = _bash.run(cmd, cwd=cwd, env=env, timeout_sec=timeout_sec)
        except Exception as e:  # noqa: BLE001
            stdout, stderr, rc = "", f"runner error: {e}", 1

        try:
            send_frame(
                client,
                {"type": "result", "id": cmd_id, "stdout": stdout, "stderr": stderr, "rc": rc},
            )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"harbor-runner: send result error: {e}\n")
            return


def main() -> None:
    global _listen_sock, _client_sock

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    _listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _listen_sock.bind((LISTEN_HOST, LISTEN_PORT))
    _listen_sock.listen(1)
    sys.stderr.write(f"harbor-runner: listening on {LISTEN_HOST}:{LISTEN_PORT}\n")

    # Accept one client at a time. If a client disconnects we just go back to
    # accept(); the trial controller may reconnect within the same pod.
    while True:
        client, addr = _listen_sock.accept()
        _client_sock = client
        sys.stderr.write(f"harbor-runner: accepted {addr}\n")
        try:
            client.settimeout(None)  # blocking reads
            _serve_one_client(client)
        finally:
            try:
                client.close()
            except Exception:
                pass
            _client_sock = None


if __name__ == "__main__":
    main()
