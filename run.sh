#!/usr/bin/env bash
# Run experiments on the lab PC (the one with the Peltier hardware) from this machine over SSH.
#
#   ./run.sh check                         Pull the latest code and check the Arduino / power supply connections
#   ./run.sh start [TARGET] [MINUTES]      Push, pull on the lab PC and start main.py detached (default 19.5 °C, 30 min;
#                                          MINUTES=0 runs until stopped). The run keeps going if SSH disconnects.
#   ./run.sh log                           Follow the running experiment's console output (Ctrl+C only stops watching)
#   ./run.sh status                        Show whether an experiment is running
#   ./run.sh stop                          Stop the run cleanly (saves the data, switches the supply off)
#   ./run.sh fetch                         Copy the latest measurements folder (+ console.log) into logs/
#
# Needs the "labpc" host in ~/.ssh/config. The lab PC's SSH shell is Windows PowerShell.
set -euo pipefail

HOST=labpc
REPO='C:\Users\user\Documents\ShoebUnideb\Peltier_control'
PYTHON='C:\Users\user\.virtualenvs\Control-1X7AsKyA\Scripts\python.exe'
# PowerShell expression listing the python processes running main.py
FIND_RUN="Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*main.py*' }"

cd "$(dirname "$0")"

# UTF-8 both ways so the "°" in the output survives
remote() { ssh "$HOST" "[Console]::OutputEncoding=[Text.Encoding]::UTF8; \$env:PYTHONIOENCODING='utf-8'; $1"; }

# Succeeds if main.py is running on the lab PC; exits the script if the lab PC can't be reached
running() {
    local out
    out=$(remote "$FIND_RUN") || { echo "Can't reach the lab PC over SSH." >&2; exit 1; }
    [[ -n $out ]]
}

is_number() { [[ $1 =~ ^[0-9]+([.][0-9]+)?$ ]]; }

pull() {
    if [[ -n $(git status --porcelain -- '*.py') ]]; then
        echo "Warning: uncommitted changes in .py files are NOT sent to the lab PC (commit them first):"
        git status --short -- '*.py'
    fi
    git push --quiet origin HEAD
    remote "cd '$REPO'; git pull --ff-only --quiet 2>&1 | Out-Null; git log --oneline -1"
}

case "${1:-}" in
check)
    pull
    remote "cd '$REPO'; & '$PYTHON' -" <<'EOF'
from main import preflight_check
for c in preflight_check():
    print({True: 'OK  ', False: 'FAIL', None: 'WARN'}[c['ok']], c['name'] + ':', c['message'])
EOF
    ;;
start)
    target=${2:-19.5}
    minutes=${3:-30}
    is_number "$target" && is_number "$minutes" || { echo "Usage: $0 start [TARGET] [MINUTES]"; exit 1; }
    if running; then
        echo "An experiment is already running on the lab PC (./run.sh stop to end it)."
        exit 1
    fi
    pull
    args="--target $target"
    [[ $minutes != 0 ]] && args+=" --duration $minutes"
    # Started through WMI so it isn't tied to this SSH session; Agg because there is no display
    cmdline="cmd /c \"set MPLBACKEND=Agg&& set PYTHONIOENCODING=utf-8&& $PYTHON -u main.py $args > console.log 2>&1\""
    result=$(remote "(Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{CommandLine='$cmdline'; CurrentDirectory='$REPO'}).ReturnValue" | tr -d '\r')
    [[ $result == 0 ]] || { echo "Start failed (WMI return value: $result)"; exit 1; }
    echo "Started: target $target °C, $([[ $minutes == 0 ]] && echo 'until stopped' || echo "$minutes min")."
    echo "Watch it with ./run.sh log, end it early with ./run.sh stop."
    ;;
log)
    remote "Get-Content '$REPO\\console.log' -Encoding UTF8 -Tail 30 -Wait"
    ;;
status)
    if running; then
        echo "Running. Last output:"
        remote "Get-Content '$REPO\\console.log' -Encoding UTF8 -Tail 9"
    else
        echo "No experiment running."
    fi
    ;;
stop)
    remote "New-Item -ItemType File -Force '$REPO\\STOP' | Out-Null"
    echo "Stop requested; waiting for the run to save its data..."
    for _ in $(seq 60); do
        running || { remote "Get-Content '$REPO\\console.log' -Encoding UTF8 -Tail 4"; exit 0; }
        sleep 2
    done
    echo "Still running after 2 min; check ./run.sh log or the RDP session."
    ;;
fetch)
    folder=$(remote "Get-ChildItem '$REPO' -Directory -Filter 'measurements_*' | Sort-Object Name | Select-Object -Last 1 -ExpandProperty Name" | tr -d '\r')
    [[ -n $folder ]] || { echo "No measurements folder on the lab PC."; exit 1; }
    mkdir -p logs
    scp -rq "$HOST:Documents/ShoebUnideb/Peltier_control/$folder" logs/
    scp -q "$HOST:Documents/ShoebUnideb/Peltier_control/console.log" "logs/$folder/" || true
    echo "Copied to logs/$folder:"
    ls "logs/$folder"
    ;;
*)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
