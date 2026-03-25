#!/bin/bash
# Backup investigation databases using SQLite's .backup command
# This is safe even while databases are being written to (handles WAL mode correctly)
#
# Usage: ./tools/backup.sh [label]
# Example: ./tools/backup.sh post-wave7
#          ./tools/backup.sh  (uses timestamp)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="$PROJECT_DIR/backups"

LABEL="${1:-$(date +%Y%m%d-%H%M%S)}"
DEST="$BACKUP_DIR/$LABEL"

mkdir -p "$DEST"

echo "Backing up databases to: $DEST"

# investigation.db — the irreplaceable one
echo -n "  investigation.db... "
sqlite3 "$PROJECT_DIR/investigation.db" ".backup '$DEST/investigation.db'"
SIZE=$(ls -lh "$DEST/investigation.db" | awk '{print $5}')
echo "$SIZE"

# registry.db — can be rebuilt but slow
echo -n "  registry.db... "
sqlite3 "$PROJECT_DIR/registry.db" ".backup '$DEST/registry.db'"
SIZE=$(ls -lh "$DEST/registry.db" | awk '{print $5}')
echo "$SIZE"

# lmsband — large but now has FTS5 index + parsed financials
echo -n "  lmsband_epstein_files.db... "
sqlite3 "$PROJECT_DIR/datasets/lmsband_epstein_files.db" ".backup '$DEST/lmsband_epstein_files.db'"
SIZE=$(ls -lh "$DEST/lmsband_epstein_files.db" | awk '{print $5}')
echo "$SIZE"

# Stats
echo ""
echo "Backup complete: $DEST"
TOTAL=$(du -sh "$DEST" | awk '{print $1}')
echo "Total size: $TOTAL"

# List existing backups
echo ""
echo "All backups:"
for d in "$BACKUP_DIR"/*/; do
    [ -d "$d" ] || continue
    SIZE=$(du -sh "$d" | awk '{print $1}')
    NAME=$(basename "$d")
    echo "  $NAME  ($SIZE)"
done
