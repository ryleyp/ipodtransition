#!/bin/bash
# Check a music folder for duplicate songs.
#
# Usage:
#   ./check_duplicates.sh                          # checks ~/Music/iPhone Transfer
#   ./check_duplicates.sh ~/Desktop/MyMusic        # checks another folder
#   ./check_duplicates.sh --against-library        # compare vs your Music library
#   ./check_duplicates.sh --delete                 # remove identical copies
#
# Reuses the same private Python environment as transfer_music.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Run ./transfer_music.sh first." >&2
    exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "Setting up a private Python environment..."
    python3 -m venv "$VENV_DIR"
fi

if ! "$VENV_DIR/bin/python" -c "import mutagen" >/dev/null 2>&1; then
    echo "Installing required library (mutagen)..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet mutagen
fi

# --against-library is a shorthand for the default macOS Music media folder.
ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--against-library" ]]; then
        LIBRARY="$HOME/Music/Music/Media/Music"
        if [[ ! -d "$LIBRARY" ]]; then
            LIBRARY="$HOME/Music/Music/Media"
        fi
        if [[ ! -d "$LIBRARY" ]]; then
            echo "Could not find your Music library at ~/Music/Music/Media." >&2
            echo "Pass the folder explicitly instead:" >&2
            echo "  ./check_duplicates.sh <folder> --against <library folder>" >&2
            exit 1
        fi
        ARGS+=("--against" "$LIBRARY")
    else
        ARGS+=("$arg")
    fi
done

exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/find_duplicates.py" "${ARGS[@]}"
