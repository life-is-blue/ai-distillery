#!/bin/bash
# Distribute SOUL.md + MEMORY.md → downstream project AGENTS.md
# Usage: ./scripts/distribute-agents.sh <target-project-dir>
#
# Reads ai-memory/SOUL.md + ai-memory/MEMORY.md and generates a unified
# AGENTS.md in the target project. Idempotent — safe to run repeatedly.

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
MEMORY_DIR="$(dirname "$SCRIPT_DIR")/ai-memory"
SOUL="$MEMORY_DIR/SOUL.md"
MEMORY="$MEMORY_DIR/MEMORY.md"

if [ ! -f "$SOUL" ] || [ ! -f "$MEMORY" ]; then
    echo "ERROR: SOUL.md or MEMORY.md missing in $MEMORY_DIR" >&2
    exit 1
fi

AGENTS_PATH="$TARGET/AGENTS.md"
TMP="$(mktemp)"

{
    echo "# AGENTS.md"
    echo ""
    echo "> Auto-generated from ai-distillery on $(date +%Y-%m-%d). Do not edit by hand."
    echo "> Source: ai-memory/SOUL.md + ai-memory/MEMORY.md"
    echo "> Regenerate: \`ai-distillery/scripts/distribute-agents.sh $(basename "$TARGET")\`"
    echo ""
    echo "This file encodes your developer mental model and behavioral rules,"
    echo "distilled from all AI session logs. Agents should follow these rules"
    echo "when working in this repository."
    echo ""
    echo "---"
    echo ""
    # SOUL body (skip header + metadata, start from first ##)
    awk '/^## Identity/{found=1} found' "$SOUL"
    echo ""
    echo "---"
    echo ""
    # MEMORY body
    awk '/^## MUST/{found=1} found' "$MEMORY"
} > "$TMP"

# Atomic write
mv "$TMP" "$AGENTS_PATH"

# Symlink CLAUDE.md → AGENTS.md (Claude Code convention)
if [ ! -L "$TARGET/CLAUDE.md" ] || [ "$(readlink "$TARGET/CLAUDE.md")" != "AGENTS.md" ]; then
    ln -sfn AGENTS.md "$TARGET/CLAUDE.md"
fi

bytes=$(wc -c < "$AGENTS_PATH")
rules=$(grep -c '^- ' "$AGENTS_PATH" || true)
echo "OK $AGENTS_PATH (${bytes} bytes, ${rules} entries)"
