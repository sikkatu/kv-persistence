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
# Env overrides: CONFIG_FILE (target config to write), KVP_MODEL_REVISION (to pin
# the upstream commit SHA instead of a local file fingerprint),
# KVP_FINGERPRINT_MODE (content|fast).
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
MODEL_DIR="${1:?usage: setup-identity.sh <local model dir>}"
[ -d "$MODEL_DIR" ] || die "model dir not found: $MODEL_DIR"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# Site values go to identity.env.local (gitignored) so the tracked identity.env
# is never modified -- keeping `git pull` conflict-free and the tree clean.
# CONFIG_FILE still allows explicit targeting when a rebuild of the tracked
# file is genuinely intended.
CONFIG="${CONFIG_FILE:-$ROOT/configs/mimo-v2.6-pro/identity.env.local}"
if [ ! -f "$CONFIG" ]; then
  cp "$ROOT/configs/mimo-v2.6-pro/identity.env" "$CONFIG" || die "cannot seed $CONFIG from identity.env"
  echo "seeded $CONFIG from the shipped identity.env template"
fi
[ -w "$CONFIG" ] || die "cannot write $CONFIG (fix permissions/ownership first)"
command -v python3 >/dev/null || die "python3 is required"

echo "== kv-persistence identity setup =="
echo "model dir : $MODEL_DIR"
echo "config    : $CONFIG"

# --- 1. revisions (weights + tokenizer) ---------------------------------
# Two distinct kinds of identity, NOT interchangeable:
#   * HF commit SHA  -- the true upstream revision, when you track remote provenance
#     (set KVP_MODEL_REVISION before running this script)
#   * local checkpoint fingerprint -- a deterministic digest of the LOCAL files,
#     covering everything that defines the served weights: config.json, every
#     *.index.json, AND the sizes + names of every shard the index declares
#     (so a changed shard with an unchanged index still rotates the fingerprint).
# The local fingerprint is deliberately labelled "local fingerprint", not a commit SHA.
REV="${KVP_MODEL_REVISION:-}"
if [ -z "$REV" ]; then
  echo "-- resolving LOCAL CHECKPOINT FINGERPRINT (content-hashed) from files on disk"
  # Modes (KVP_FINGERPRINT_MODE):
  #   content (default) - SHA-256 of every shard's BYTES + config.json + indexes.
  #     Catches any byte-level weight swap (name and size alone cannot).
  #     Shard hashes are cached in <model_dir>/.kvpersist-shard-cache.json so a
  #     rerun after the first is cheap; any changed mtime/size re-hashes.
  #   fast - old behaviour: shard name + size only. NOT safe against a
  #     content swap that preserves both; only for disposable experiments.
  FPMODE="${KVP_FINGERPRINT_MODE:-content}"
  case "$FPMODE" in
    content|fast) ;;
    *) die "KVP_FINGERPRINT_MODE must be 'content' or 'fast', got '$FPMODE'" ;;
  esac
  if [ "$FPMODE" = "fast" ]; then
    echo "  WARNING: KVP_FINGERPRINT_MODE=fast does NOT hash shard CONTENT -- a changed"
    echo "  shard with the same name and size is invisible to this fingerprint. This mode"
    echo "  is only appropriate for throwaway experiments, never production."
  fi
  REV="$(python3 - "$MODEL_DIR" "$FPMODE" <<'PY'
import glob, hashlib, json, os, sys
d, mode = sys.argv[1], sys.argv[2]
cache_path = os.path.join(d, ".kvpersist-shard-cache.json")
cache = {}
if os.path.isfile(cache_path):
    try:
        cache = json.load(open(cache_path))
    except Exception:
        cache = {}
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()
def feed_text(b: bytes):
    h.update(b); h.update(b"\0")
def feed(path, label):
    h.update(label.encode()); h.update(b"\0")
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                h.update(chunk)
    h.update(b"\0")
h = hashlib.sha256()
feed(os.path.join(d, "config.json"), "config.json")
indexes = sorted(glob.glob(os.path.join(d, "*.index.json")))
if not indexes:
    sys.exit("no *.index.json in model dir: cannot derive local fingerprint")
new_cache = {}
shard_seen = 0
for idx in indexes:
    feed(idx, os.path.basename(idx))
    try:
        with open(idx, "r", encoding="utf-8") as fh:
            wm = json.load(fh).get("weight_map", {})
    except Exception as exc:
        sys.exit(f"unreadable index {idx}: {exc}")
    shards = sorted(set(wm.values()))
    total_shards = len(shards)
    for i, shard in enumerate(shards, 1):
        sp = os.path.join(d, shard)
        size = os.path.getsize(sp) if os.path.isfile(sp) else -1
        if mode != "fast":
            # Review 4A: print progress so the operator knows this is not hung --
            # hashing 4-8 GB shards takes minutes on first run.
            print(f"hashing shard {shard} ... ({i}/{total_shards}, {size/(1<<30):.1f} GB)", file=sys.stderr, flush=True)
        mtime = int(os.path.getmtime(sp)) if os.path.isfile(sp) else -1
        if mode == "fast":
            feed_text(f"{shard}\0{size}\0".encode())
            continue
        # content mode: hash shard bytes, cached by (size, mtime)
        key = f"{shard}:{size}:{mtime}"
        cached = cache.get(key)
        if cached:
            shard_hash = cached
        else:
            if size < 0:
                sys.exit(f"shard declared in index but missing on disk: {shard}")
            shard_hash = sha256_file(sp)
            new_cache[key] = shard_hash
        feed_text(f"{shard}\0{shard_hash}\0".encode())
        shard_seen += 1
# persist cache entries (merge both caches, tolerate unlistable dirs)
merged = dict(cache)
merged.update(new_cache)
try:
    with open(cache_path, "w") as fh:
        json.dump(merged, fh)
except Exception:
    pass
print(h.hexdigest()[:40])
PY
)" || die "cannot derive local checkpoint fingerprint from $MODEL_DIR"
  [ -n "$REV" ] || die "empty local fingerprint; set KVP_MODEL_REVISION externally"
  echo "  local checkpoint fingerprint: $REV"
  echo "  NOTE: this is a FILE fingerprint, not an HF commit SHA. If you track"
  echo "  remote provenance, set KVP_MODEL_REVISION to the real commit SHA instead."
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
        text = pat.sub(lambda m: m.group(1) + '="' + value.replace('"', '\\"') + '"', text, count=1)
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