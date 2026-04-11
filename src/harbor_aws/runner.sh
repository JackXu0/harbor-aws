#!/bin/bash
# harbor-aws pod runner — outbound TCP client to the harbor-control server.
#
# Replaces the previous runner.py. Pure bash, uses /dev/tcp, no extra binaries
# beyond coreutils (base64, timeout, mktemp). Works on any image with bash;
# the start-up bootstrap installs bash via apk on Alpine if necessary.
#
# Wire protocol — newline-delimited frames over the TCP connection. base64
# encoding on commands and outputs avoids any newline collisions in payloads.
#
#   Runner -> control:
#     A\n<token>\n<trial_id>\n              (auth, first frame)
#     R\n<cmd_id>\n<rc>\n<b64_stdout>\n<b64_stderr>\n   (result)
#     Q\n                                   (pong)
#
#   Control -> runner:
#     OK\n                                  (auth ok)
#     FAIL\n<reason>\n                      (auth fail)
#     E\n<cmd_id>\n<timeout_sec>\n<b64_cmd>\n   (exec)
#     P\n                                   (ping, optional keepalive)
#     S\n                                   (shutdown)
#
# Env vars (set by harbor-aws when creating the pod):
#   HARBOR_CONTROL_HOST   in-cluster DNS name of the harbor-control service
#   HARBOR_CONTROL_PORT   runner-accept port on the control server (e.g. 8444)
#   HARBOR_TRIAL_ID       unique id for this trial pod
#   HARBOR_TOKEN          shared secret. The control server pre-registers
#                         {trial_id -> token} and validates this on auth.

set -u

: "${HARBOR_CONTROL_HOST:?missing HARBOR_CONTROL_HOST}"
: "${HARBOR_CONTROL_PORT:?missing HARBOR_CONTROL_PORT}"
: "${HARBOR_TRIAL_ID:?missing HARBOR_TRIAL_ID}"
: "${HARBOR_TOKEN:?missing HARBOR_TOKEN}"

# base64 -w0 isn't in busybox base64; fall back to tr -d '\n'.
b64() {
    if base64 --help 2>&1 | grep -q -- '-w'; then
        base64 -w0
    else
        base64 | tr -d '\n'
    fi
}

# Open TCP connection to harbor-control. Retry on DNS / connect failures —
# CoreDNS gets overwhelmed when thousands of pods start simultaneously.
attempt=0
while ! exec 3<>/dev/tcp/"$HARBOR_CONTROL_HOST"/"$HARBOR_CONTROL_PORT" 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ $attempt -ge 30 ]; then
        echo "harbor-runner: failed to connect to $HARBOR_CONTROL_HOST:$HARBOR_CONTROL_PORT after $attempt attempts" >&2
        exit 1
    fi
    sleep 2
done

# Send auth frame.
printf 'A\n%s\n%s\n' "$HARBOR_TOKEN" "$HARBOR_TRIAL_ID" >&3

# Read auth response.
if ! IFS= read -r resp <&3; then
    echo "harbor-runner: auth read failed" >&2
    exit 1
fi
if [ "$resp" != "OK" ]; then
    IFS= read -r reason <&3 || true
    echo "harbor-runner: auth failed: ${reason:-unknown}" >&2
    exit 1
fi
echo "harbor-runner: connected as $HARBOR_TRIAL_ID" >&2

# Command loop. Each iteration reads one frame.
while IFS= read -r frame_type <&3; do
    case "$frame_type" in
        E)
            IFS= read -r cmd_id <&3 || break
            IFS= read -r timeout_sec <&3 || break
            IFS= read -r b64cmd <&3 || break

            # Decode the command to a temp script file rather than passing it
            # as `bash -c "$cmd"`. The argv form blows past Linux's ARG_MAX
            # (~128 KB) when upload_file/upload_dir send multi-MB base64 tar
            # payloads as a single command.
            script=$(mktemp)
            out=$(mktemp)
            err=$(mktemp)
            printf '%s' "$b64cmd" | base64 -d > "$script"
            timeout "$timeout_sec" bash "$script" >"$out" 2>"$err"
            rc=$?

            b64out=$(b64 <"$out")
            b64err=$(b64 <"$err")
            rm -f "$script" "$out" "$err"

            printf 'R\n%s\n%s\n%s\n%s\n' "$cmd_id" "$rc" "$b64out" "$b64err" >&3
            ;;
        P)
            printf 'Q\n' >&3
            ;;
        S)
            echo "harbor-runner: shutdown requested" >&2
            break
            ;;
        '')
            ;;  # blank line, ignore
        *)
            echo "harbor-runner: unknown frame type: $frame_type" >&2
            ;;
    esac
done

exec 3>&-
echo "harbor-runner: connection closed" >&2
