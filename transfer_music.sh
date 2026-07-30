#!/bin/bash
# One-command iPhone -> Mac music transfer.
#
# Usage:
#   ./transfer_music.sh                     # saves to ~/Music/iPhone Transfer
#   ./transfer_music.sh ~/Desktop/MyMusic   # saves somewhere else
#
# Sets up a private Python environment on first run (no system changes),
# then pulls the music off the USB-connected iPhone and organizes it into
# Artist/Album folders.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This script is meant to run on a Mac." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. macOS will offer to install the Command Line" >&2
    echo "Tools — accept that, then rerun this script. (Trigger it with:" >&2
    echo "  xcode-select --install)" >&2
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "Python 3.9 or newer is required (found $(python3 -V 2>&1))." >&2
    echo "Install a current Python from https://www.python.org/downloads/" >&2
    echo "or with Homebrew: brew install python3" >&2
    exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "First run: setting up a private Python environment..."
    python3 -m venv "$VENV_DIR"
fi

if ! "$VENV_DIR/bin/python" -c "import pymobiledevice3, mutagen" >/dev/null 2>&1; then
    echo "Installing required libraries (pymobiledevice3, mutagen)..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet pymobiledevice3 mutagen
fi

echo
echo "Plug in your iPhone with a USB cable and unlock it."
echo "If the phone shows a 'Trust This Computer?' dialog, tap Trust."
echo

exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/iphone_music_transfer.py" "$@"
