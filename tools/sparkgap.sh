#!/bin/bash
# Runs the SparkGap instances on this machine; "sparkgap.sh help" lists the commands.
set -u
R=${SPARKGAP_ROOT:-$HOME/sparkgap}
CODE=$R/code                  # the clone every receiver runs from
SERVER_WAIT_S=15  # longest the server takes to start or stop
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then BOLD=$'\e[1m' GREEN=$'\e[32m' CYAN=$'\e[36m' YELLOW=$'\e[33m' RED=$'\e[31m' BLUE=$'\e[34m' OFF=$'\e[0m'; else BOLD= GREEN= CYAN= YELLOW= RED= BLUE= OFF=; fi

web_address() {  # SparkGap Web's address and join code, from server.json
  python3 - "$R/server.json" "$BOLD$YELLOW" "$OFF" <<'PY' 2>/dev/null || echo "  (run sparkgap.sh setup first)"
import json, sys
c = json.load(open(sys.argv[1])); hi, off = sys.argv[2:4]
code = str(c.get("join_code", "")).upper().replace("-", "")
host, port = c.get("bind", "127.0.0.1"), c.get("port", 7320)
web = "https" if c.get("tls_cert") else "http"
print(f"  {web}://{host}:{port}/  (join code {hi}{code[:3]}-{code[3:]}{off})")
PY
}
usage() {
  cat <<EOF
sparkgap.sh: SparkGap on this machine. Day to day, use SparkGap Web:
$(web_address)

${BLUE}Commands$OFF
  ${BOLD}setup$OFF      set up or check this machine: code, server, radio, spotter ID, grid
  ${BOLD}start$OFF      start SparkGap (the server, and with it SparkGap Web)
  ${BOLD}stop$OFF       stop SparkGap
  ${BOLD}status$OFF     each radio and its receivers

${BLUE}Key terms$OFF
  radio     the nickname set on the radio in SmartSDR (Dallas)
  station   SparkGap's connection to the radio (SparkGap-Dallas)
  node      the RBN spotter ID and grid its spots go out under
  feeder    what sends those spots to RBN
  receiver  one band's skimmer, <radio>-<DAX-IQ channel> (Dallas-1)

Run folder: $R   Receivers: ${ALL:-none}
EOF
}
bad() { usage >&2; exit 2; }

die() { echo "sparkgap.sh: $*" >&2; exit 2; }

ALL=$([ -f "$R/instances" ] && awk '!/^#/ && NF {printf "%s ", $1}' "$R/instances")
[ -f "$R/instances" ] || [[ "${1:-}" =~ ^(|setup|help|-h|--help)$ ]] \
  || die "no instances file in $R (sparkgap.sh setup makes one; set SPARKGAP_ROOT for another root)"

field() { awk -v n="$1" -v f="$2" '$1 == n {print $f}' "$R/instances"; }
dir() { local d; d=$(field "$1" 2); [ -n "$d" ] || die "unknown instance $1 (instances: $ALL)"; echo "$d"; }
pid() { local p; p=$(cat "$R/logs/$1.pid" 2>/dev/null) && kill -0 "$p" 2>/dev/null && echo "$p"; }

server_status() {  # the machine's server (sparkgap_server.py): running or not, its address, join code and web page
  local p
  [ -f "$R/server.json" ] || { echo "${BOLD}server$OFF: not set up (sparkgap.sh setup writes server.json)"; return; }
  p=$(server_pid) || p=
  python3 - "$R/server.json" "$p" "$BOLD" "$GREEN" "$RED" "$YELLOW" "$OFF" <<'PY'
import json, sys
c = json.load(open(sys.argv[1])); pid, B, G, R, Y, O = sys.argv[2:8]
code = str(c.get("join_code", "")).upper().replace("-", "")
show = f"{code[:3]}-{code[3:]}" if len(code) == 6 else code
host, port = c.get("bind", "127.0.0.1"), c.get("port", 7320)
web = "https" if c.get("tls_cert") else "http"
state = f"{G}running{O}, pid {pid}" if pid else f"{R}stopped{O}"
label = f"\033[34mserver{O}" if B else "server"
print(f"{label}: {state}, {host}:{port}, join code {B}{Y}{show}{O}, browser {web}://{host}:{port}/")
PY
}

server_pid() {  # the server's pid while it is a sparkgap_server.py for this root
  local p cmd
  p=$(cat "$R/logs/server.pid" 2>/dev/null) && cmd=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null) || return 1
  [[ $cmd == *sparkgap_server.py* ]] && { [[ $cmd != *--root* ]] || [[ $cmd == *"--root $R "* ]]; } && echo "$p"
}

server() {  # server start|stop: the machine's server (sparkgap_server.py), run from the code clone
  local p top py log
  case $1 in
    start)
      if p=$(server_pid); then echo "server: already running (pid $p)"; return; fi
      top=$(source_clone 2>/dev/null); [ -n "$top" ] || die "no code/ clone (run sparkgap.sh setup)"
      py=$R/.venv/bin/python; [ -x "$py" ] || py=$(command -v python3)
      log="$R/logs/server_$(date -u +%Y%m%d_%H%M%SZ).log"; mkdir -p "$R/logs"
      (cd "$top" && exec setsid "$py" sparkgap_server.py --root "$R" > "$log" 2>&1 < /dev/null &)
      for _ in $(seq 1 "$SERVER_WAIT_S"); do
        sleep 1; p=$(server_pid) && { echo "server: running (pid $p), log $log"; return; }
      done
      echo "server: failed to start, see $log"; return 1 ;;
    stop)
      p=$(server_pid) || { echo "server: not running"; return; }
      kill -INT "$p"
      for _ in $(seq 1 "$SERVER_WAIT_S"); do kill -0 "$p" 2>/dev/null || { echo "server: stopped (its instances keep running)"; return; }; sleep 1; done
      echo "server: still running after ${SERVER_WAIT_S} s (pid $p)"; return 1 ;;
  esac
}

RUNNING=$R/logs/running   # the receivers running at the last stop, started again by start
start_all() {  # the server, then the receivers that ran at the last stop
  local names
  server_pid > /dev/null || server start || exit 1
  names=$(cat "$RUNNING" 2>/dev/null)
  if [ -z "$names" ]; then echo "no receivers to start again: start them in SparkGap Web"; return; fi
  ctl start $names && rm -f "$RUNNING"
}
stop_all() {  # every running receiver (remembered for start), its station and feeder, then the server
  local i names=
  for i in $ALL; do pid "$i" > /dev/null && names="$names $i"; done
  if [ -n "$names" ]; then
    echo "${names# }" > "$RUNNING"
    server_pid > /dev/null || die "receivers run but the server does not: sparkgap.sh start, then stop"
    ctl stop $names || exit 1
  fi
  server stop
}
ctl() {  # the server's command line client: every start, stop and change goes through the server
  local top; top=$(source_clone 2>/dev/null); [ -n "$top" ] || die "no code/ clone (run sparkgap.sh setup)"
  python3 "$top/sparkgap_ctl.py" --root "$R" "$@"
  local rc=$?; [ $rc -eq 2 ] && echo "sparkgap.sh: start SparkGap with: sparkgap.sh start" >&2
  return $rc
}
start() { ctl start "$1"; }
stop() { ctl stop "$1"; }

clone_of() {  # the clone, on a branch, that a code folder shares its repository with
  git -C "$1" worktree list --porcelain | awk '/^worktree / {w = $2} /^branch / {print w; exit}'
}



UPSTREAM=https://github.com/hotairfred/SparkGap.git
radio_field() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$1" "$2" 2>/dev/null; }
setup() {  # setup: the run folder, server, radio and node settings, asking only what is missing; rerun any time
  local repo br out rc addr radio r top kind="" name="" files=()
  if [ ! -d "$CODE/.git" ] || [ ! -f "$R/instances" ]; then
    read -r -p "Repository to clone [$UPSTREAM]: " repo
    read -r -p "Branch [main]: " br
    init "${repo:-$UPSTREAM}" "${br:-main}" || exit 2
  fi
  top=$(source_clone) || die "no code/ clone"
  server_pid > /dev/null || server start || die "the server did not start"
  shopt -s nullglob; files=("$R"/config/*.radio.json); shopt -u nullglob
  until [[ $kind == flex || $kind == hpsdr ]]; do read -r -p "Radio type (flex or hpsdr): " kind; done
  [ "$kind" = hpsdr ] && read -r -p "Its name, if not set up yet (Enter: as found): " name
  [ ${#files[@]} -eq 1 ] && [ "$(radio_field "${files[0]}" type)" = "$kind" ] && addr=$(radio_field "${files[0]}" address)   # asked directly: works where broadcasts do not
  out=$(ctl discover "$kind" ${addr:+"$addr"} ${name:+"name=$name"}); rc=$?; echo "$out"
  while [ $rc -ne 0 ] || [[ $out == "no radio heard"* ]]; do
    [ $rc -eq 2 ] && exit 2   # no server
    read -r -p "Radio address (Enter to stop): " addr
    [ -n "$addr" ] || die "no radio found; run sparkgap.sh setup again and give its address"
    out=$(ctl discover "$kind" "$addr" ${name:+"name=$name"}); rc=$?; echo "$out"
  done
  shopt -s nullglob; files=("$R"/config/*.radio.json); shopt -u nullglob
  [ ${#files[@]} -gt 0 ] || die "no radio file was written"
  radio=$(basename "${files[0]}" .radio.json)
  [ -f "$R/config/${out%%:*}.radio.json" ] && radio=${out%%:*}   # the radio just found, not the first file
  if [ ${#files[@]} -gt 1 ]; then
    echo "Radios: $(for f in "${files[@]}"; do basename "$f" .radio.json; done | tr '\n' ' ')"
    read -r -p "Which radio [$radio]: " r; radio=${r:-$radio}
    [ -f "$R/config/$radio.radio.json" ] || die "no radio file for $radio"
  fi
  local said r rx mode
  said=$(mktemp) || die "no temporary file"
  python3 "$top/tools/setup_node.py" "$R" "$radio" | tee "$said"; [ "${PIPESTATUS[0]}" -eq 0 ] || { rm -f "$said"; exit 2; }
  rx=$(sed -n 's/^restart: //p' "$said"); r=$(sed -n 's/^restart-feeder: //p' "$said"); rm -f "$said"
  [ -z "$rx" ] || { ctl stop $rx && ctl start $rx; }   # what the answer above agreed to
  if [ -n "$r" ]; then mode=$(radio_field "$R/config/$radio.radio.json" rbn_upload); ctl rbn "$r" "${mode:-dry}"; fi
  ctl status | awk -v r=" $radio ━━" '/^━━ /{p = index($0, r) > 0} p'   # the radio as status shows it
  echo "add receivers in SparkGap Web:"; server_status
}
init() {  # init <repository> <branch>: what is missing, nothing replaced
  local repo=$1 br=$2
  mkdir -p "$R/config" "$R/logs" "$R/rec" || die "cannot make $R"
  if [ -d "$CODE/.git" ]; then echo "$CODE: already there"
  elif [ -e "$CODE" ]; then die "$CODE exists but is not a git clone"
  elif [ -f "$R/instances" ] && [ -n "$ALL" ]; then die "instances already run here without code/"
  else git clone -q --branch "$br" "$repo" "$CODE" || die "clone of $repo ($br) failed"; echo "$CODE: cloned $br"
  fi
  [ -f "$R/instances" ] || { echo "# name code-dir config python" > "$R/instances"; echo "$R/instances: made"; }
}



source_clone() {  # the clone on a branch that every code folder follows: code/, or in an older root the first instance's
  if [ -d "$CODE/.git" ]; then echo "$CODE"; else clone_of "$(dir "${ALL%% *}")"; fi
}






[ $# -gt 0 ] || { usage; exit 0; }
cmd=$1; shift
for a in "$cmd" "$@"; do [[ $a == help || $a == -h || $a == --help ]] && { usage; exit 0; }; done
case $cmd in
  status) [ $# -eq 0 ] || bad; server_status; echo; ctl status ;;
  start) if [ $# -eq 0 ]; then start_all; else ctl start "$@"; fi ;;
  stop) if [ $# -eq 0 ]; then stop_all; else ctl stop "$@"; fi ;;
  setup) [ $# -eq 0 ] || bad; setup ;;
  *) echo "sparkgap.sh: unknown command '$cmd'" >&2; echo >&2; bad ;;
esac
