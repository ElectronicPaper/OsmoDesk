#!/usr/bin/env bash
# Apply one mutation, run a test module, restore, prove the restore took.
#
#   tools/mutate.sh <file> <test.module> "<description>" "<old>" "<new>"
#
# Clearing __pycache__ is the point, not housekeeping. A `cp` restore rewrites
# the source but can leave the mutant's bytecode cached, and Python will run
# that: the suite then reports failures against code no longer on disk, which
# reads as a regression and is not one. That cost a confusing debug cycle.
#
# The anchor must appear exactly once. A mutation that lands somewhere other
# than intended proves nothing about the code you meant to test.
set -uo pipefail

FILE="$1"; MODULE="$2"; DESC="$3"; OLD="$4"; NEW="$5"
PY=./.venv/Scripts/python.exe
BAK="$(mktemp)"
cp "$FILE" "$BAK"

clear_cache() {
    find . -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null
    return 0
}
restore() { cp "$BAK" "$FILE"; rm -f "$BAK"; clear_cache; }
trap restore EXIT

if ! "$PY" - "$FILE" "$OLD" "$NEW" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text(encoding="utf-8")
n = s.count(sys.argv[2])
if n != 1:
    sys.exit(f"anchor appears {n} times, expected exactly 1")
p.write_text(s.replace(sys.argv[2], sys.argv[3], 1), encoding="utf-8")
PY
then
    printf "  %-44s -> ANCHOR PROBLEM\n" "$DESC"
    exit 1
fi

clear_cache
OUT=$("$PY" -m unittest "$MODULE" 2>&1 | tail -2 | tr '\n' ' ')
case "$OUT" in
    *FAILED*) VERDICT="caught" ;;
    *OK*)     VERDICT="SURVIVED -- the code is not covered" ;;
    *)        VERDICT="unclear: $OUT" ;;
esac
printf "  %-44s -> %s\n" "$DESC" "$VERDICT"
