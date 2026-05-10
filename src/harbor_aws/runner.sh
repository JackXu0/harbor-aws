#!/bin/bash
# runner.sh runs once per trial pod, for the whole pod's lifetime, holding
# one persistent TCP connection to the control pod. Inside it, a `bash <script>`
# subprocess is spawned per exec call. The script's main loop dispatches exec
# frames into those subprocesses and forwards results back.

set -u

: "${HARBOR_CONTROL_SERVICE_HOST:?missing HARBOR_CONTROL_SERVICE_HOST (kubelet should auto-inject)}"
: "${HARBOR_CONTROL_SERVICE_PORT:?missing HARBOR_CONTROL_SERVICE_PORT (kubelet should auto-inject)}"
: "${HARBOR_TRIAL_ID:?missing HARBOR_TRIAL_ID}"
: "${HARBOR_TRIAL_TOKEN:?missing HARBOR_TRIAL_TOKEN}"

# Encode any bytes as a single-line ASCII string
b64() {
    base64 | tr -d '\n'
}

# Establish TCP connection to the control pod
attempt=0
while ! exec 3<>/dev/tcp/"$HARBOR_CONTROL_SERVICE_HOST"/"$HARBOR_CONTROL_SERVICE_PORT" 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ $attempt -ge 30 ]; then
        echo "harbor-runner: failed to connect to $HARBOR_CONTROL_SERVICE_HOST:$HARBOR_CONTROL_SERVICE_PORT after $attempt attempts" >&2
        exit 1
    fi
    sleep 2
done

# Send auth frame to control pod
printf 'A\n%s\n%s\n' "$HARBOR_TRIAL_TOKEN" "$HARBOR_TRIAL_ID" >&3

# Read auth response
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
