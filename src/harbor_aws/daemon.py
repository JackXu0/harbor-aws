#!/usr/bin/env python3
"""Harbor AWS daemon — runs inside the container, polls S3 for commands.

This script is injected into the container entrypoint. It polls an S3 prefix
for command files, executes them via subprocess, and uploads results back to S3.
File transfers also go through S3.

Environment variables (set by the container definition):
    HARBOR_S3_BUCKET  — S3 bucket name
    HARBOR_S3_PREFIX  — S3 prefix (e.g., harbor-aws/sessions/<session_id>/)
    AWS_DEFAULT_REGION — AWS region
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

# Auto-install boto3 if not available
try:
    import boto3
except ImportError:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "boto3"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    import boto3


def main() -> None:
    bucket = os.environ["HARBOR_S3_BUCKET"]
    prefix = os.environ["HARBOR_S3_PREFIX"]
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    s3 = boto3.client("s3", region_name=region)
    processed: set[str] = set()

    while True:
        try:
            resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}commands/")
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                if key in processed or not key.endswith(".json"):
                    continue
                processed.add(key)
                _handle_command(s3, bucket, prefix, key)
        except Exception as e:
            print(f"[harbor-daemon] poll error: {e}", file=sys.stderr, flush=True)

        time.sleep(0.5)


def _handle_command(s3, bucket: str, prefix: str, cmd_key: str) -> None:
    """Download a command, execute it, upload the result."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=cmd_key)
        cmd = json.loads(resp["Body"].read())
    except Exception as e:
        print(f"[harbor-daemon] failed to read {cmd_key}: {e}", file=sys.stderr, flush=True)
        return

    cmd_type = cmd.get("type", "exec")
    cmd_uuid = cmd_key.rsplit("/", 1)[-1].replace(".json", "")
    result_key = f"{prefix}results/{cmd_uuid}.json"

    if cmd_type == "exec":
        result = _run_exec(cmd)
    elif cmd_type == "upload":
        result = _run_upload(s3, bucket, cmd)
    elif cmd_type == "download":
        result = _run_download(s3, bucket, cmd)
    else:
        result = {"stdout": None, "stderr": f"Unknown command type: {cmd_type}", "return_code": 1}

    try:
        s3.put_object(Bucket=bucket, Key=result_key, Body=json.dumps(result).encode())
    except Exception as e:
        print(f"[harbor-daemon] failed to write result {result_key}: {e}", file=sys.stderr, flush=True)


def _run_exec(cmd: dict) -> dict:
    """Execute a shell command and return stdout/stderr/return_code."""
    command = cmd["command"]
    cwd = cmd.get("cwd")
    env = cmd.get("env")
    timeout = cmd.get("timeout_sec") or 300

    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            env=run_env,
        )
        return {
            "stdout": proc.stdout.decode(errors="replace") or None,
            "stderr": proc.stderr.decode(errors="replace") or None,
            "return_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": None, "stderr": f"Command timed out after {timeout}s", "return_code": 124}
    except Exception as e:
        return {"stdout": None, "stderr": str(e), "return_code": 1}


def _run_upload(s3, bucket: str, cmd: dict) -> dict:
    """Download a file/dir from S3 and place it in the container filesystem."""
    s3_key = cmd["s3_key"]
    target_path = cmd["target_path"]
    is_dir = cmd.get("is_dir", False)

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz" if is_dir else "") as tmp:
            s3.download_file(bucket, s3_key, tmp.name)
            tmp_path = tmp.name

        if is_dir:
            os.makedirs(target_path, exist_ok=True)
            with tarfile.open(tmp_path, "r:gz") as tar:
                tar.extractall(path=target_path)
        else:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            shutil.move(tmp_path, target_path)
            tmp_path = None

        return {"stdout": None, "stderr": None, "return_code": 0}
    except Exception as e:
        return {"stdout": None, "stderr": str(e), "return_code": 1}
    finally:
        if is_dir and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _run_download(s3, bucket: str, cmd: dict) -> dict:
    """Tar a file/dir from the container and upload to S3."""
    source_path = cmd["source_path"]
    s3_key = cmd["s3_key"]
    is_dir = cmd.get("is_dir", False)

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz" if is_dir else "") as tmp:
            tmp_path = tmp.name

        if is_dir:
            with tarfile.open(tmp_path, "w:gz") as tar:
                tar.add(source_path, arcname=".")
        else:
            shutil.copy2(source_path, tmp_path)

        s3.upload_file(tmp_path, bucket, s3_key)
        return {"stdout": None, "stderr": None, "return_code": 0}
    except Exception as e:
        return {"stdout": None, "stderr": str(e), "return_code": 1}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


if __name__ == "__main__":
    main()
