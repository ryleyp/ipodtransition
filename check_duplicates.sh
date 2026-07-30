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

# Does a folder contain at least one audio file? An empty "Media" folder is
# a decoy — the real library is usually elsewhere when that happens.
has_audio() {
    [[ -d "$1" ]] || return 1
    local hit
    # Command substitution rather than a pipe: find exits non-zero when it
    # hits an unreadable subfolder, which `set -o pipefail` would treat as
    # "no audio here" and skip an otherwise valid library.
    hit="$(find "$1" -type f \( -iname '*.mp3' -o -iname '*.m4a' \
        -o -iname '*.m4p' -o -iname '*.aac' -o -iname '*.aiff' \
        -o -iname '*.aif' -o -iname '*.wav' -o -iname '*.flac' \) \
        -print -quit 2>/dev/null || true)"
    [[ -n "$hit" ]]
}

# Print the Music/iTunes media folder, or nothing if it can't be found.
find_library() {
    local candidates=(
        # Music app (Catalina and later)
        "$HOME/Music/Music/Media/Music"
        "$HOME/Music/Music/Media"
        # iTunes (Mojave and earlier)
        "$HOME/Music/iTunes/iTunes Media/Music"
        "$HOME/Music/iTunes/iTunes Media"
    )
    # Libraries with a non-default name, e.g. ~/Music/My Library/Media.
    local extra
    for extra in "$HOME/Music"/*/Media/Music "$HOME/Music"/*/Media \
                 "$HOME/Music"/*/"iTunes Media/Music" \
                 "$HOME/Music"/*/"iTunes Media"; do
        [[ -d "$extra" ]] && candidates+=("$extra")
    done

    local dir
    # Prefer a folder that actually holds music.
    for dir in "${candidates[@]}"; do
        if has_audio "$dir"; then
            printf '%s\n' "$dir"
            return 0
        fi
    done
    # Last resort: anything named Media/iTunes Media under ~/Music.
    while IFS= read -r dir; do
        if has_audio "$dir"; then
            printf '%s\n' "$dir"
            return 0
        fi
    done < <(find "$HOME/Music" -maxdepth 3 -type d \
                \( -name 'Media' -o -name 'iTunes Media' \) 2>/dev/null)
    return 1
}

# --against-library is a shorthand for "wherever my Music library lives".
ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--against-library" ]]; then
        LIBRARY="$(find_library || true)"
        if [[ -z "$LIBRARY" ]]; then
            echo "Could not find a Music library containing audio files." >&2
            echo >&2
            echo "Looked in ~/Music for the Music app's Media folder and" >&2
            echo "iTunes' 'iTunes Media' folder. Yours may be on an external" >&2
            echo "drive, or you may not have imported anything yet." >&2
            echo >&2
            echo "To find the real path: open Music, then Music > Settings >" >&2
            echo "Files. The 'Music Media folder location' is shown there." >&2
            echo "Then pass it explicitly (you can drag the folder from" >&2
            echo "Finder into Terminal to paste its path):" >&2
            echo >&2
            echo "  ./check_duplicates.sh ~/Music/'iPhone Transfer' \\" >&2
            echo "      --against '/path/to/your/Media'" >&2
            echo >&2
            echo "Or just check the transferred folder on its own:" >&2
            echo "  ./check_duplicates.sh" >&2
            exit 1
        fi
        echo "Using Music library: $LIBRARY"
        ARGS+=("--against" "$LIBRARY")
    else
        ARGS+=("$arg")
    fi
done

# Guard the expansion: an empty array trips `set -u` on macOS's bash 3.2.
if [[ ${#ARGS[@]} -gt 0 ]]; then
    exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/find_duplicates.py" "${ARGS[@]}"
else
    exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/find_duplicates.py"
fi
