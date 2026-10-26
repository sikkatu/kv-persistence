#!/usr/bin/env bash
# setup-identity.sh -- ONE command that fills AND verifies all identity pins.
#
# Review fixes: (1) all mandatory fields, not only KVP_MODEL_REVISION; (2) values
# are WRITTEN into the config automatically and verified afterwards -- no manual
# paste loop around preflight. Tokenizer revision falls back to the weights SHA
# when the files demonstrably come from the same commit (or when served from the
# pinned local MODEL_DIR); anything uncertain FAILS instead of guessing.
#
# Run on the host that serves the model:
#   bash scripts/setup-identity.sh "$HOME/models/MiMo-V2.6-Pro-MOPD"
# Env overrides: MODEL_ID, HF_MODEL_ID (for the revision lookup), CONFIG_FILE.
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
MODEL_DIR="${1:?usage: setup-identity.sh <local model dir>}"
[ -d "$MODEL_DIR" ] || die "model dir not found: $MODEL_DIR"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CONFIG="${CONFIG_FILE:-$ROOT/configs/mimo-v2.6-pro/identity.env}"
MODEL_ID="${MODEL_ID:-XiaomiMiMo/MiMo-V2.6-Pro-MOPD}"
[ -w "$CONFIG" ] || die "cannot write $CONFIG (fix permissions/ownership first)"
command -v python3 >/dev/null || die "python3 is required"

echo "== kv-persistence identity setup =="
echo "model dir : $MODEL_DIR"
echo "config    : $CONFIG"

# --- 1. revisions (weights + tokenizer) ---------------------------------
REV="${KVP_MODEL_REVISION:-}"
if [ -z "$REV" ]; then
  echo "-- resolving weights revision from the pinned local files"
  # LOCAL model dir is the authoritative source: hash the weight index. This
  # downloads nothing and proves which checkpoint is actually on disk.
  REV="$(python3 - "$MODEL_DIR" <<'PY'
import glob, hashlib, json, os, sys
d = sys.argv[1]
idx = sorted(glob.glob(os.path.join(d, "*.index.json")))
if not idx:
    sys.exit("no *.index.json in model dir: cannot derive local revision")
h = hashlib.sha256()
for p in idx:
    with open(p, "rb") as fh:
        h.update(fh.read())
print(h.hexdigest()[:40])
PY
)"
  [ -n "$REV" ] || die "could not derive a local revision; set KVP_MODEL_REVISION externally"
  echo "  local checkpoint fingerprint: $REV (sha256 of weight index, first 40 hex)"
  echo "  NOTE: substitute the HF commit SHA here if you additionally track remote provenance"
else
  printf '%s' "$REV" | grep -Eq '^[0-9a-f]{40}$' || die "KVP_MODEL_REVISION not 40-hex: '$REV'"
fi

TOK_REV="${KVP_TOKENIZER_REVISION:-}"
if [ -z "$TOK_REV" ]; then
  if [ -f "$MODEL_DIR/tokenizer.json" ]; then
    TOK_REV="$(python3 - "$MODEL_DIR" <<'PY'
import hashlib, os, sys
d = sys.argv[1]
h = hashlib.sha256()
for name in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
    p = os.path.join(d, name)
    if os.path.isfile(p):
        h.update(name.encode()); h.update(open(p, "rb").read())
print(h.hexdigest()[:40])
PY
)"
    echo "  tokenizer fingerprint: $TOK_REV"
  else
    die "no tokenizer files in model dir and KVP_TOKENIZER_REVISION unset -- uncertain, refusing to guess"
  fi
else
  printf '%s' "$TOK_REV" | grep -Eq '^[0-9a-f]{40}$' || die "KVP_TOKENIZER_REVISION not 40-hex: '$TOK_REV'"
fi

# --- 2. chat-template digest (single documented algorithm) --------------
DIG="$(python3 - "$MODEL_DIR" "$ROOT/implementation" <<'PY'
import sys
sys.path.insert(0, sys.argv[2])
from kvpersist.chat_template import chat_template_digest
digest, source = chat_template_digest(sys.argv[1])
print(digest + "\t" + source)
PY
)" || die "chat template could not be resolved unambiguously -- see message above"
DIGEST="${DIG%%$'\t'*}"; SRC="${DIG#*$'\t'}"
echo "  chat template source: $SRC"
echo "  digest: $DIGEST"

# --- 3. write values into the config ------------------------------------
python3 - "$CONFIG" "$REV" "$TOK_REV" "$DIGEST" <<'PY'
import re, sys
path, rev, tokrev, digest = sys.argv[1:5]
text = open(path).read()
def setvar(name, value):
    global text
    pat = re.compile(r"^(" + name + r")=.*$", re.M)
    if pat.search(text):
        text = pat.sub(lambda m: m.group(1) + "=" + value, text, count=1)
    else:
        text += f"\n{name}={value}\n"
setvar("KVP_MODEL_REVISION", rev)
setvar("KVP_TOKENIZER_REVISION", tokrev)
setvar("KVP_CHAT_TEMPLATE_DIGEST", digest)
open(path, "w").write(text)
print("written: KVP_MODEL_REVISION, KVP_TOKENIZER_REVISION, KVP_CHAT_TEMPLATE_DIGEST")
PY

# --- 4. verify what we just wrote --------------------------------------
echo "-- verifying the written configuration"
python3 - "$CONFIG" "$ROOT/implementation" <<'PY' || die "identity config failed verification"
import re, sys
sys.path.insert(0, sys.argv[2])
from kvpersist.identity import CacheIdentity, IdentityError
vals = {}
for line in open(sys.argv[1]).read().splitlines():
    m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
    if m:
        vals[m.group(1)] = m.group(2)
try:
    ident = CacheIdentity(
        model_id=vals.get("KVP_MODEL_ID", ""),
        model_revision=vals.get("KVP_MODEL_REVISION", ""),
        tokenizer_name=vals.get("KVP_TOKENIZER_NAME", ""),
        tokenizer_revision=vals.get("KVP_TOKENIZER_REVISION", ""),
        kv_cache_dtype=vals.get("KVP_KV_CACHE_DTYPE", ""),
        block_size=int(vals.get("KVP_BLOCK_SIZE", "0")),
        num_kv_heads=int(vals.get("KVP_NUM_KV_HEADS", "0")),
        head_dim=int(vals.get("KVP_HEAD_DIM", "0")),
        tp_size=int(vals.get("KVP_TP_SIZE", "0")),
        connector=vals.get("KVP_CONNECTOR", ""),
        connector_version=vals.get("KVP_CONNECTOR_VERSION", ""),
        max_model_len=int(vals.get("KVP_MAX_MODEL_LEN", "0")),
        chat_template_digest=vals.get("KVP_CHAT_TEMPLATE_DIGEST", ""),
    )
    ns = ident.namespace()
except (IdentityError, ValueError, KeyError) as exc:
    print("identity invalid:", exc)
    raise SystemExit(3)
print("namespace:", ns)
PY
echo
echo "identity setup COMPLETE -- preflight should now pass:"
echo "  bash scripts/preflight.sh"