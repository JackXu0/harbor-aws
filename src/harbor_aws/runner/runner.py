"""harbor-aws pod runner — outbound HTTP long-poll to the control server.

Runs as PID 1 inside a Fargate pod. Connects out to a harbor-aws control server,
reads commands via long-poll, executes them in a long-lived bash subprocess, and
posts results back. Replaces the kubectl-exec / WebSocket path entirely.

Pure stdlib — no pip-installable dependencies. Will run inside any base image
that has Python 3.8+ available.

Wire protocol:
  POST {url}/register
       body: {"session_id": "...", "token": "..."}
       resp: {"ok": true}

  GET  {url}/next-command/{session_id}?token=...
       (long poll, resp: 200 with {"id": "...", "cmd": "...",
        "cwd": "...", "env": {...}, "timeout_sec": N}
        or 204 No Content on idle timeout — pod just polls again)

  POST {url}/result/{session_id}/{cmd_id}?token=...
       body: {"stdout": "...", "stderr": "...", "rc": N}
       resp: {"ok": true}

  POST {url}/exit/{session_id}?token=...
       (best-effort, fired in the SIGTERM handler)

Env vars (set by harbor-aws when creating the pod):
  HARBOR_CONTROL_URL    e.g. https://harbor-control.example.com
  HARBOR_SESSION_ID     unique session id matching the trial
  HARBOR_TOKEN          shared secret for this session
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# --- config from env ---------------------------------------------------------

CONTROL_URL = os.environ.get("HARBOR_CONTROL_URL", "").rstrip("/")
SESSION_ID = os.environ.get("HARBOR_SESSION_ID", "")
TOKEN = os.environ.get("HARBOR_TOKEN", "")

if not CONTROL_URL or not SESSION_ID or not TOKEN:
    sys.stderr.write(
        "harbor-runner: missing required env (HARBOR_CONTROL_URL, "
        "HARBOR_SESSION_ID, HARBOR_TOKEN)\n"
    )
    sys.exit(2)

REGISTER_RETRY_INITIAL = 1.0
REGISTER_RETRY_MAX = 30.0
LONG_POLL_TIMEOUT_SEC = 30  # how long the server holds the GET open
HTTP_TIMEOUT_SEC = LONG_POLL_TIMEOUT_SEC + 5

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
        # Sanity: bash is alive
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

        # We can't easily do select() on Popen pipes portably without nonblocking I/O.
        # Use os.read with O_NONBLOCK on the underlying fds.
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
                # Find the line with the token + rc
                idx = stdout_buf.find(end_token_b)
                # The rc line looks like "__HEND_xxx__ 0\n"
                eol = stdout_buf.find(b"\n", idx)
                if eol == -1:
                    # Token found but newline not yet — wait for more bytes
                    pass
                else:
                    line = stdout_buf[idx:eol]
                    parts2 = line.decode("utf-8", errors="replace").split()
                    try:
                        rc = int(parts2[-1])
                    except (ValueError, IndexError):
                        rc = 1
                    stdout_out = stdout_buf[:idx].rstrip(b"\n")
                    stdout_buf = stdout_out
                    out_done = True
            if not err_done and end_token_b in stderr_buf:
                idx = stderr_buf.find(end_token_b)
                eol = stderr_buf.find(b"\n", idx)
                if eol == -1:
                    pass
                else:
                    stderr_out = stderr_buf[:idx].rstrip(b"\n")
                    stderr_buf = stderr_out
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


# --- HTTP helpers ------------------------------------------------------------


def _http_request(
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float = HTTP_TIMEOUT_SEC,
) -> tuple[int, bytes]:
    url = f"{CONTROL_URL}{path}"
    data = None
    headers = {"User-Agent": "harbor-runner/1"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — URL is from trusted env
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""


def register() -> None:
    """Register with the control server. Retries forever with backoff."""
    delay = REGISTER_RETRY_INITIAL
    while True:
        try:
            status, body = _http_request(
                "POST",
                "/register",
                {"session_id": SESSION_ID, "token": TOKEN},
            )
            if status == 200:
                sys.stderr.write(f"harbor-runner: registered as {SESSION_ID}\n")
                return
            sys.stderr.write(
                f"harbor-runner: register returned {status}: {body[:200]!r}; retrying in {delay:.1f}s\n"
            )
        except Exception as e:  # noqa: BLE001 — we want to retry on anything
            sys.stderr.write(f"harbor-runner: register failed: {e}; retrying in {delay:.1f}s\n")
        time.sleep(delay)
        delay = min(delay * 1.5, REGISTER_RETRY_MAX)


def long_poll_next_command() -> dict | None:
    """Long-poll for the next command. Returns the command dict, or None on timeout."""
    qs = urllib.parse.urlencode({"token": TOKEN})
    try:
        status, body = _http_request(
            "GET", f"/next-command/{SESSION_ID}?{qs}", timeout=HTTP_TIMEOUT_SEC,
        )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"harbor-runner: long-poll error: {e}\n")
        time.sleep(1.0)
        return None
    if status == 204:
        return None  # idle timeout, just poll again
    if status == 200:
        return json.loads(body.decode("utf-8"))
    if status == 410:
        # Session ended — server told us to exit
        sys.stderr.write("harbor-runner: server returned 410 (session ended), exiting\n")
        sys.exit(0)
    sys.stderr.write(f"harbor-runner: unexpected long-poll status {status}: {body[:200]!r}\n")
    time.sleep(1.0)
    return None


def post_result(cmd_id: str, stdout: str, stderr: str, rc: int) -> None:
    qs = urllib.parse.urlencode({"token": TOKEN})
    try:
        status, body = _http_request(
            "POST",
            f"/result/{SESSION_ID}/{cmd_id}?{qs}",
            {"stdout": stdout, "stderr": stderr, "rc": rc},
        )
        if status != 200:
            sys.stderr.write(f"harbor-runner: post_result returned {status}: {body[:200]!r}\n")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"harbor-runner: post_result error: {e}\n")


def post_exit() -> None:
    qs = urllib.parse.urlencode({"token": TOKEN})
    try:
        _http_request("POST", f"/exit/{SESSION_ID}?{qs}")
    except Exception:  # noqa: BLE001
        pass


# --- main loop ---------------------------------------------------------------


_bash: BashSession | None = None


def _shutdown_handler(signum, frame) -> None:  # noqa: ARG001
    sys.stderr.write(f"harbor-runner: signal {signum} received, shutting down\n")
    if _bash is not None:
        _bash.close()
    post_exit()
    sys.exit(0)


def main() -> None:
    global _bash

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    register()
    _bash = BashSession()

    sys.stderr.write("harbor-runner: ready, polling for commands\n")
    while True:
        cmd_msg = long_poll_next_command()
        if cmd_msg is None:
            continue

        cmd_id = cmd_msg.get("id", "")
        cmd = cmd_msg.get("cmd", "")
        cwd = cmd_msg.get("cwd")
        env = cmd_msg.get("env") or None
        timeout_sec = int(cmd_msg.get("timeout_sec", 300))

        if not cmd_id or not cmd:
            sys.stderr.write(f"harbor-runner: malformed command message: {cmd_msg}\n")
            continue

        try:
            stdout, stderr, rc = _bash.run(cmd, cwd=cwd, env=env, timeout_sec=timeout_sec)
        except Exception as e:  # noqa: BLE001 — return any error to caller
            stdout, stderr, rc = "", f"runner error: {e}", 1

        post_result(cmd_id, stdout, stderr, rc)


if __name__ == "__main__":
    main()
