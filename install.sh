#!/usr/bin/env bash
# Install the databricks plugin into Hermes.
#
#   ./install.sh              copy plugin/ into $HERMES_HOME/plugins/databricks
#   ./install.sh --symlink    symlink instead (for development on this repo)
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/databricks"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/plugin"
MODE="copy"

if [ "${1:-}" = "--symlink" ]; then
  MODE="symlink"
fi

if [ ! -d "$SRC_DIR" ]; then
  echo "ERROR: $SRC_DIR not found — run this from the repo root." >&2
  exit 1
fi

echo "Installing databricks plugin ($MODE) into $PLUGIN_DIR"

# Refuse to clobber an existing install silently; back it up instead.
if [ -e "$PLUGIN_DIR" ] || [ -L "$PLUGIN_DIR" ]; then
  BACKUP="$PLUGIN_DIR.bak.$(date +%Y%m%d-%H%M%S)"
  mv "$PLUGIN_DIR" "$BACKUP"
  echo "  existing install moved to $BACKUP"
fi

mkdir -p "$(dirname "$PLUGIN_DIR")"

if [ "$MODE" = "symlink" ]; then
  ln -s "$SRC_DIR" "$PLUGIN_DIR"
else
  mkdir -p "$PLUGIN_DIR"
  # Copy source + bundled skills, but never stale bytecode.
  (cd "$SRC_DIR" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - .) \
    | (cd "$PLUGIN_DIR" && tar -xf -)
fi

echo ""
echo "Plugin installed to: $PLUGIN_DIR"
echo ""

# The Databricks CLI is what actually holds the credential, so say so plainly.
if command -v databricks >/dev/null 2>&1; then
  echo "Databricks CLI: $(command -v databricks) ($(databricks version 2>/dev/null || echo 'version unknown'))"
else
  echo "NOTE: the 'databricks' CLI is not on PATH."
  echo "      Install it: https://docs.databricks.com/dev-tools/cli/install.html"
  echo "      (or configure a workspace with --host and a static token instead)"
fi

echo ""
echo "Next steps:"
echo "  1. hermes plugins enable databricks"
echo "  2. databricks auth login --host https://<your-workspace-url>"
echo "  3. hermes databricks status"
echo ""
echo "With a logged-in CLI, no Hermes config is needed — the CLI's default"
echo "profile is adopted automatically. To pin a specific profile:"
echo "  hermes databricks profiles"
echo "  hermes databricks setup --profile <name> --default"
echo ""
echo "Bundled skills (load with skill_view):"
echo "  databricks:databricks                    entry point"
echo "  databricks:databricks-workspace-setup    auth + multi-workspace"
echo "  databricks:databricks-sql-authoring      dialect + cost control"
echo "  databricks:databricks-bulk-export        big results to disk"
echo "  databricks:databricks-troubleshooting    symptom -> cause"
echo ""
echo "Done. Restart Hermes for the plugin to take effect."
