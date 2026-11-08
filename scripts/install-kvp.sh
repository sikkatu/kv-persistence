#!/usr/bin/env bash
# install-kvp.sh -- put the `kvp` command on PATH (~/.local/bin) and remember
# the repo locations (round 63). Safe to rerun; no sudo needed.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BIN="$HOME/.local/bin"
CFG="$HOME/.config/kvp"
mkdir -p "$BIN" "$CFG"
# Symlink (not copy) so a `git pull` updates the installed command immediately.
ln -sfn "$HERE/kvp" "$BIN/kvp"
chmod 755 "$BIN/kvp"
printf '%s' "$ROOT" > "$CFG/kv_repo"
# remember the recipes clone if we can find it
for _r in "$HOME/sikkatu-recipes" "$HOME/recipes" "$ROOT/../recipes"; do
  [ -d "$_r/recipes/rtx-pro-6000" ] && { printf '%s' "$_r" > "$CFG/recipes_repo"; break; }
done
case ":$PATH:" in
  *":$BIN:"*) : ;;
  *) echo "NOTE: add $BIN to PATH (in ~/.bashrc): export PATH=\"$BIN:\$PATH\"" ;;
esac
echo "installed: $BIN/kvp"
echo "  kv-persistence: $(cat "$CFG/kv_repo")"
echo "  recipes:        $(cat "$CFG/recipes_repo" 2>/dev/null || echo '(not found yet)')"
echo "usage: kvp up | down | status | logs"
