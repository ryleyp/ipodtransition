#!/usr/bin/env python3
"""Copy music off an iPhone onto this Mac over USB.

Music synced to an iPhone from another computer can't be synced back with
Finder/iTunes, but the audio files themselves still live on the phone under
/iTunes_Control/Music with scrambled names (F00/ABCD.m4a, ...). This script
pulls that folder over USB via the AFC file service (pymobiledevice3), then
reads each file's embedded tags (mutagen) and files it away as
Artist/Album/NN Title.ext in your destination folder.

Works on any music that was synced from a computer. Apple Music streaming
downloads are DRM-protected, stored elsewhere, and cannot be recovered this
way. Old iTunes Store ".m4p" purchases will copy over but only play on a
computer authorized for the purchasing Apple ID.
"""

import argparse
import posixpath
import re
import shutil
import stat as statmod
import sys
import tempfile
from pathlib import Path

try:
    from pymobiledevice3.exceptions import (
        NoDeviceConnectedError,
        PasswordRequiredError,
    )
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
except ImportError:
    sys.exit(
        "pymobiledevice3 is not installed. Run this via ./transfer_music.sh, "
        "or: pip install pymobiledevice3 mutagen"
    )

try:
    import mutagen
except ImportError:
    sys.exit(
        "mutagen is not installed. Run this via ./transfer_music.sh, "
        "or: pip install pymobiledevice3 mutagen"
    )

MUSIC_DIR = "/iTunes_Control/Music"
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".m4p", ".aac", ".aif", ".aiff",
    ".wav", ".alac", ".flac", ".caf",
}
# Characters that are unsafe in macOS/other filesystems' file names.
UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def connect():
    """Return an AFC connection to the first USB-connected iPhone."""
    try:
        lockdown = create_using_usbmux()
    except NoDeviceConnectedError:
        sys.exit(
            "No iPhone found. Plug it in with a USB cable, unlock it, and "
            "tap 'Trust' if a dialog appears on the phone — then rerun."
        )
    except PasswordRequiredError:
        sys.exit(
            "The iPhone is locked. Unlock it (and tap 'Trust This Computer' "
            "if asked), then rerun."
        )
    print(f"Connected to: {lockdown.display_name or 'iPhone'} "
          f"(iOS {lockdown.product_version})")
    return AfcService(lockdown)


def iter_remote_files(afc, path):
    """Yield full paths of every regular file under `path` on the phone."""
    try:
        entries = afc.listdir(path)
    except Exception as err:  # noqa: BLE001 - surface any AFC failure per-dir
        print(f"  ! Could not list {path}: {err}", file=sys.stderr)
        return
    for name in entries:
        if name in (".", ".."):
            continue
        full = posixpath.join(path, name)
        try:
            info = afc.os_stat(full)
        except Exception as err:  # noqa: BLE001
            print(f"  ! Could not stat {full}: {err}", file=sys.stderr)
            continue
        if statmod.S_ISDIR(info.st_mode):
            yield from iter_remote_files(afc, full)
        elif statmod.S_ISREG(info.st_mode):
            yield full


def pull_music(afc, raw_dir: Path):
    """Copy every audio file from the phone into raw_dir. Returns count."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    pulled = 0
    skipped = 0
    for remote in iter_remote_files(afc, MUSIC_DIR):
        ext = posixpath.splitext(remote)[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            continue
        # Flatten F00/XXXX.m4a -> F00_XXXX.m4a so raw names stay unique.
        rel = posixpath.relpath(remote, MUSIC_DIR).replace("/", "_")
        local = raw_dir / rel
        if local.exists() and local.stat().st_size > 0:
            skipped += 1
            continue
        try:
            local.write_bytes(afc.get_file_contents(remote))
        except Exception as err:  # noqa: BLE001
            print(f"  ! Failed to copy {remote}: {err}", file=sys.stderr)
            continue
        pulled += 1
        if pulled % 25 == 0:
            print(f"  ...{pulled} files copied")
    if skipped:
        print(f"  ({skipped} files already downloaded — skipped)")
    return pulled + skipped


def clean(name, fallback):
    name = UNSAFE_CHARS.sub("_", (name or "").strip()).strip(". ")
    return name or fallback


def first(tags, key):
    values = tags.get(key) if tags else None
    return str(values[0]) if values else None


def organize(raw_dir: Path, dest: Path):
    """File each pulled track as Artist/Album/NN Title.ext under dest."""
    organized = 0
    untagged = 0
    for path in sorted(raw_dir.iterdir()):
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            audio = mutagen.File(path, easy=True)
        except Exception:  # noqa: BLE001 - treat unreadable files as untagged
            audio = None
        tags = audio.tags if audio else None

        artist = clean(
            first(tags, "albumartist") or first(tags, "artist"),
            "Unknown Artist",
        )
        album = clean(first(tags, "album"), "Unknown Album")
        title = clean(first(tags, "title"), path.stem)
        track = first(tags, "tracknumber")
        if tags is None:
            untagged += 1

        prefix = ""
        if track:
            number = track.split("/")[0]
            if number.isdigit():
                prefix = f"{int(number):02d} "

        target_dir = dest / artist / album
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{prefix}{title}{path.suffix.lower()}"
        counter = 1
        while target.exists():
            if target.stat().st_size == path.stat().st_size:
                break  # same song already organized on a previous run
            target = target_dir / (
                f"{prefix}{title} ({counter}){path.suffix.lower()}"
            )
            counter += 1
        if not target.exists():
            shutil.move(str(path), target)
        else:
            path.unlink()
        organized += 1
    return organized, untagged


def main():
    parser = argparse.ArgumentParser(
        description="Copy music from a USB-connected iPhone to this Mac."
    )
    parser.add_argument(
        "dest",
        nargs="?",
        default=str(Path.home() / "Music" / "iPhone Transfer"),
        help="Destination folder (default: ~/Music/iPhone Transfer)",
    )
    parser.add_argument(
        "--keep-raw",
        action="store_true",
        help="Keep the raw scrambled-name copies instead of deleting them",
    )
    args = parser.parse_args()

    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    raw_dir = dest / "_raw"

    afc = connect()
    print("Copying music from the phone (this can take a while)...")
    total = pull_music(afc, raw_dir)
    if total == 0:
        sys.exit(
            "No music files were found on the phone. Only music synced from "
            "a computer lives in this folder — Apple Music streaming "
            "downloads are DRM-protected and cannot be copied."
        )
    print(f"Copied {total} audio files. Organizing by artist and album...")

    organized, untagged = organize(raw_dir, dest)
    if not args.keep_raw:
        shutil.rmtree(raw_dir, ignore_errors=True)

    print(f"\nDone. {organized} tracks are in: {dest}")
    if untagged:
        print(f"  {untagged} files had no readable tags — look for them "
              f"under 'Unknown Artist/Unknown Album'.")
    print("To get them into the Music app: open Music, then "
          "File > Import... and pick that folder.")


if __name__ == "__main__":
    main()
