#!/usr/bin/env bash
# print-recipe-image.sh -- extract the IMAGE digest from a recipe's tp4.env.example,
# whether the assignment is live (IMAGE=...) or a commented template (# IMAGE="...").
# Replaces the fragile `grep -m1 -E '^\s*#?\s*IMAGE=' | sed ...` shell one-liner.
#
# Usage:
#   bash scripts/print-recipe-image.sh [recipe-dir]
#   RECIPE_DIR=... bash scripts/print-recipe-image.sh
# Exit codes: 0 ok (digest printed), 1 not found, 2 bad input, 3 unpinned value.

recipe="${1:-${RECIPE_DIR:-}}"
if [ -z "$recipe" ]; then
  cat >&2 <<'EOF'
usage: print-recipe-image.sh <recipe-dir>
  or   RECIPE_DIR=... bash scripts/print-recipe-image.sh
EOF
  exit 2
fi
example="$recipe/tp4.env.example"
[ -f "$example" ] || { echo "ERROR: $example does not exist" >&2; exit 1; }

python3 - "$example" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
# Accept live or commented assignment; last occurrence wins (recipe may show a
# preferred default first and an override later).
pat = re.compile(r'^\s*#?\s*IMAGE\s*=\s*(.+?)\s*$', re.M)
matches = pat.findall(text)
if not matches:
    sys.stderr.write("ERROR: no IMAGE= assignment (live or commented) found in %s\n" % path)
    sys.exit(1)
val = matches[-1].split(" #")[0].strip().strip('"').strip("'")
digest_match = re.search(r'@sha256:([0-9a-f]{64})$', val)
if not digest_match:
    sys.stderr.write("ERROR: IMAGE=%s in %s is not digest-pinned (needs @sha256:<64 hex>)\n" % (val, path))
    sys.exit(3)
print(val)
PY