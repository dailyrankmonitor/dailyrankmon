#!/usr/bin/env bash
# One-shot setup for this repo on a fresh macOS machine. Safe to re-run.
#
#   ./setup.sh                 # clone browser-harness to ~/Developer/browser-harness
#   HARNESS_DIR=~/src/bh ./setup.sh
#
# Does four things: checks Chrome + python3, installs uv if absent, clones and
# registers browser-harness (`uv tool install -e .`), then verifies mmt_srp.py
# can locate it. Nothing here touches your everyday Chrome profile.
set -euo pipefail

HARNESS_DIR="${HARNESS_DIR:-$HOME/Developer/browser-harness}"
HARNESS_REPO="https://github.com/browser-use/browser-harness"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

echo "1/4 prerequisites"
[[ "$(uname)" == "Darwin" ]] || warn "written for macOS; Chrome path below will need adjusting"
[[ -x "$CHROME" ]] && ok "Google Chrome" || die "Google Chrome not found at: $CHROME — install it from https://www.google.com/chrome/"
command -v python3 >/dev/null || die "python3 not found — install Xcode command line tools (xcode-select --install) or python.org"
ok "python3 $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"
command -v git >/dev/null || die "git not found — run: xcode-select --install"

echo "2/4 uv (runs the browser-harness daemon)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || die "uv installed but not on PATH; open a new shell and re-run"
fi
ok "uv $(uv --version | awk '{print $2}')"

echo "3/4 browser-harness → $HARNESS_DIR"
if [[ -f "$HARNESS_DIR/admin.py" ]]; then
  ok "checkout already present (not updated — run 'git -C $HARNESS_DIR pull' yourself if you want the latest)"
else
  mkdir -p "$(dirname "$HARNESS_DIR")"
  if git clone --quiet "$HARNESS_REPO" "$HARNESS_DIR" 2>/tmp/bh_clone_err; then
    ok "cloned"
  else
    warn "clone skipped: $(tr -d '\n' </tmp/bh_clone_err | sed 's/^fatal: //')"
  fi
fi
# Editable install: the `browser-harness` command points at the checkout, so
# mmt_srp.py can find admin.py/helpers.py through it even if HARNESS_DIR is
# somewhere unusual.
(cd "$HARNESS_DIR" && uv tool install --quiet -e . 2>&1 | grep -v '^Installed\|already installed' || true)
(cd "$HARNESS_DIR" && uv sync --quiet)          # daemon deps, so the first run doesn't pay for it
export PATH="$HOME/.local/bin:$PATH"
command -v browser-harness >/dev/null && ok "browser-harness $(browser-harness --version 2>/dev/null || echo installed)" \
  || die "browser-harness command not on PATH after install — add ~/.local/bin to PATH (uv tool update-shell) and re-run"

echo "4/4 verify"
found="$(cd "$HERE/core" && BROWSER_HARNESS_DIR="$HARNESS_DIR" python3 -c 'import mmt_srp; print(mmt_srp.harness_status())')"
ok "core/mmt_srp.py → $found"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
  warn "~/.local/bin is not on your PATH in this shell; add it (uv tool update-shell) so 'browser-harness' works everywhere";;
esac
if [[ "$HARNESS_DIR" != "$HOME/Developer/browser-harness" ]]; then
  warn "non-default location: export BROWSER_HARNESS_DIR=$HARNESS_DIR (or rely on the browser-harness command lookup)"
fi

cat <<EOF

Done. Next:
  1. edit config/cities.csv, config/dxrn.csv, config/pax.csv
  2. cd $HERE && python3 mmt_batch.py 5        # small first run
  3. results appear under runs/mmt_srp_run_<timestamp>/

The first run launches a dedicated Chrome on port 9333 with profile /tmp/mmt-chrome.
EOF
