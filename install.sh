#!/usr/bin/env bash
# install.sh — install the repo AI bridge into THIS repo.
#
#   ./install.sh                # install into the repo root
#   ./install.sh --dry-run      # show what would change, touch nothing
#
# Safe by design: never overwrites an existing file unless you pass --force,
# never writes a real secret, never deletes anything.
set -uo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$(cd "${1:-$SRC}" 2>/dev/null && pwd || pwd)"
DRY=0; FORCE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --force) FORCE=1 ;;
    -*) ;;
    *) DEST="$(cd "$arg" && pwd)" ;;
  esac
done

echo "installing repo AI bridge"
echo "  from: $SRC"
echo "  into: $DEST"
[ "$DRY" = "1" ] && echo "  mode: DRY RUN (nothing will be written)"
echo "----------------------------------------------------------"

mkdir -p "$DEST/bridge" "$DEST/scripts" "$DEST/.state/sessions"

install_file() {
  local rel="$1" mode="${2:-644}"
  local s="$SRC/$rel" d="$DEST/$rel"
  if [ ! -f "$s" ]; then echo "  SKIP  $rel (not in kit)"; return; fi
  if [ -f "$d" ] && [ "$FORCE" = "0" ]; then
    echo "  KEEP  $rel (exists — use --force to overwrite)"; return
  fi
  if [ "$DRY" = "1" ]; then echo "  WOULD write $rel"; return; fi
  cp "$s" "$d" && chmod "$mode" "$d" && echo "  ok    $rel"
}

install_file bridge/providers.py 644
install_file scripts/ask.py 755
install_file scripts/bootstrap.sh 755
install_file scripts/verify_bridge.sh 755
install_file AGENTS.md 644
install_file .env.example 644

# .gitignore: append rather than replace — never clobber the user's rules
if [ "$DRY" = "0" ]; then
  touch "$DEST/.gitignore"
  for pat in ".env" ".state/"; do
    grep -qxF "$pat" "$DEST/.gitignore" 2>/dev/null || echo "$pat" >> "$DEST/.gitignore"
  done
  echo "  ok    .gitignore (appended .env, .state/)"
fi

# README: never overwrite. Offer a copy if the repo has none.
if [ ! -f "$DEST/README.md" ]; then
  install_file README.md 644
else
  echo "  KEEP  README.md (you already have one — append the 'Model bridge' section from AGENTS.md)"
fi

echo "----------------------------------------------------------"
if [ "$DRY" = "1" ]; then
  echo "DRY RUN complete. Re-run without --dry-run to apply."
  exit 0
fi
echo "installed. Next:"
echo "  cp .env.example .env && chmod 600 .env    # then add ONE key"
echo "  ./scripts/bootstrap.sh"
echo "  ./scripts/verify_bridge.sh"
