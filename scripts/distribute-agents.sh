#!/bin/bash
# Distribute ai-memory/AGENTS.md → downstream project's AGENTS.md
# Usage: ./scripts/distribute-agents.sh <target-project-dir>

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <target-project-dir>" >&2
    exit 1
fi

TARGET="$1"
if [ ! -d "$TARGET" ]; then
    echo "ERROR: target dir not found: $TARGET" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(dirname "$SCRIPT_DIR")/ai-memory/AGENTS.md"

if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC not found. Run 'make dream' first." >&2
    exit 1
fi

cp "$SRC" "$TARGET/AGENTS.md"

# Claude Code convention: CLAUDE.md symlink
if [ ! -L "$TARGET/CLAUDE.md" ] || [ "$(readlink "$TARGET/CLAUDE.md")" != "AGENTS.md" ]; then
    ln -sfn AGENTS.md "$TARGET/CLAUDE.md"
fi

bytes=$(wc -c < "$TARGET/AGENTS.md")
echo "OK $TARGET/AGENTS.md (${bytes} bytes)"
