#!/usr/bin/env bash
# Fresh-clone acceptance for the smh package: install into a throwaway venv,
# scaffold a project, drive the whole journal loop through the CLI, assert the
# graph stays green. Exit 0 = the package is self-contained and the loop works.
#   ./acceptance.sh            (uses `python3`; set PY=... to override)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PY:-python3}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

echo "== venv + install"
"$PY" -m venv --system-site-packages venv
venv/bin/python -m pip install -q --no-build-isolation -e "$HERE" 2>&1 | grep -v "^$" | tail -3 || true
SMH="$WORK/venv/bin/smh"
[ -x "$SMH" ] || { echo "FAIL: console script not installed"; exit 1; }
unset SMH_PROJECT SMH_ROOT SMH_SESSION

echo "== init"
"$SMH" init demo --name demo | sed 's/^/   /'
cd demo
[ -f .claude/skills/journal-round/SKILL.md ] && echo "   skills bundled: $(ls .claude/skills | tr '\n' ' ')"

echo "== current project"
"$SMH" current | grep -q '^demo ' || { echo "FAIL: current project is not demo"; "$SMH" current; exit 1; }

echo "== loop"
"$SMH" graph --check | tail -1 | tee /dev/stderr | grep -q ' 0 warnings, 0 advisories' || { echo "FAIL: scaffold graph"; exit 1; }
"$SMH" mint "Acceptance hypothesis: X beats Y" --kind hypothesis --prediction "X lower loss" -y | tail -1
"$SMH" mint "Why does X win?" --kind question --link "refines H2" -y | tail -1
"$SMH" mint "Adopt X" --kind decision --rationale "because H2" --link "underpins H2" -y | tail -1
"$SMH" set-status H2 testing -y | tail -1
"$SMH" retract H1 --reason "example" -y | tail -1
"$SMH" graph --check | tail -1 | tee /dev/stderr | grep -q ' 0 warnings, 0 advisories' || { echo "FAIL: graph after writes"; exit 1; }
"$SMH" gate JOURNAL.md | tail -1 | tee /dev/stderr | grep -q 'PASS' || { echo "FAIL: fact gate"; exit 1; }
"$SMH" render | tail -1
[ -f Reports/demo-journal.html ] || { echo "FAIL: render wrote nothing"; exit 1; }

echo "== LocalRunner"
"$WORK/venv/bin/python" - <<'PY'
from smh.adapters.runner import LocalRunner, JobSpec
r = LocalRunner(log_dir="runs")
spec = JobSpec(id="acc-1", config_ref=None, cmd="echo hello-from-localrunner",
               tag="acc_echo", goal={"kind": "smoke", "claim": "H2", "text": "runner smoke"})
h = r.launch(spec, r.place(spec, r.capacity()))
res = r.result(h)
log = open(res.log_path).read().strip()
assert res.exit == 0 and log == "hello-from-localrunner", (res, log)
print(f"   LocalRunner: exit {res.exit}, log {res.log_path!r} -> {log!r}")
PY

echo "ACCEPTANCE PASS"
