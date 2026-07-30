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
import asyncio
import inspect
import posixpath
import re
import shutil
import stat as statmod
import sys
from pathlib import Path

try:
    from pymobiledevice3.exceptions import (
        AfcFileNotFoundError,
        NoDeviceConnectedError,
        PasswordRequiredError,
        UserDeniedPairingError,
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


async def resolve(value):
    """Return `value`, awaiting it first if it is awaitable.

    pymobiledevice3 10.x is fully async while 4.x was synchronous. Awaiting
    only when needed keeps this script working against either one.
    """
    if inspect.isawaitable(value):
        return await value
    return value


async def connect():
    """Return a connected AFC service for the first USB-connected iPhone."""
    try:
        lockdown = await resolve(create_using_usbmux())
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
    except UserDeniedPairingError:
        sys.exit(
            "The iPhone refused the connection ('Don't Trust' was tapped). "
            "Unplug it, plug it back in, and tap 'Trust' — then rerun."
        )
    print(f"Connected to: {lockdown.display_name or 'iPhone'} "
          f"(iOS {lockdown.product_version})")

    afc = AfcService(lockdown)
    # 10.x requires an explicit async connect; 4.x connected on construction.
    if hasattr(afc, "connect"):
        await resolve(afc.connect())
    return afc


async def iter_remote_files(afc, path):
    """Yield full paths of every regular file under `path` on the phone."""
    try:
        entries = await resolve(afc.listdir(path))
    except Exception as err:  # noqa: BLE001 - surface any AFC failure per-dir
        print(f"  ! Could not list {path}: {err}", file=sys.stderr)
        return
    for name in entries:
        if name in (".", ".."):
            continue
        full = posixpath.join(path, name)
        try:
            info = await resolve(afc.os_stat(full))
        except Exception as err:  # noqa: BLE001
            print(f"  ! Could not stat {full}: {err}", file=sys.stderr)
            continue
        if statmod.S_ISDIR(info.st_mode):
            async for sub in iter_remote_files(afc, full):
                yield sub
        elif statmod.S_ISREG(info.st_mode):
            yield full


async def find_audio_files(afc):
    """Return every audio file path under the phone's music folder."""
    try:
        found = [
            remote
            async for remote in iter_remote_files(afc, MUSIC_DIR)
            if posixpath.splitext(remote)[1].lower() in AUDIO_EXTENSIONS
        ]
    except AfcFileNotFoundError:
        sys.exit(
            f"The folder {MUSIC_DIR} does not exist on this phone, which "
            "means no music has been synced to it from a computer."
        )
    return found


async def pull_music(afc, remote_files, raw_dir: Path):
    """Copy each remote audio file into raw_dir. Returns (pulled, skipped)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    pulled = 0
    skipped = 0
    total = len(remote_files)
    for index, remote in enumerate(remote_files, start=1):
        # Flatten F00/XXXX.m4a -> F00_XXXX.m4a so raw names stay unique.
        rel = posixpath.relpath(remote, MUSIC_DIR).replace("/", "_")
        local = raw_dir / rel
        if local.exists() and local.stat().st_size > 0:
            skipped += 1
            continue
        # Write to a .part file first so an interrupted run never leaves a
        # truncated file that a later run would mistake for a finished copy.
        partial = local.with_suffix(local.suffix + ".part")
        try:
            partial.write_bytes(await resolve(afc.get_file_contents(remote)))
            partial.replace(local)
        except Exception as err:  # noqa: BLE001
            print(f"  ! Failed to copy {remote}: {err}", file=sys.stderr)
            partial.unlink(missing_ok=True)
            continue
        pulled += 1
        if pulled % 25 == 0 or index == total:
            print(f"  ...{index}/{total} files")
    return pulled, skipped


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


async def transfer(dest: Path, keep_raw: bool):
    raw_dir = dest / "_raw"
    afc = await connect()
    try:
        print("Looking for music on the phone...")
        remote_files = await find_audio_files(afc)
        if not remote_files:
            sys.exit(
                "No music files were found on the phone. Only music synced "
                "from a computer lives in this folder — Apple Music "
                "streaming downloads are DRM-protected and cannot be copied."
            )
        print(f"Found {len(remote_files)} audio files. Copying "
              f"(this can take a while)...")
        pulled, skipped = await pull_music(afc, remote_files, raw_dir)
    finally:
        if hasattr(afc, "close"):
            try:
                await resolve(afc.close())
            except Exception:  # noqa: BLE001 - nothing useful to do on exit
                pass

    if skipped:
        print(f"  ({skipped} files were already downloaded — skipped)")
    print(f"Organizing {pulled + skipped} files by artist and album...")
    organized, untagged = organize(raw_dir, dest)
    if not keep_raw:
        shutil.rmtree(raw_dir, ignore_errors=True)

    print(f"\nDone. {organized} tracks are in: {dest}")
    if untagged:
        print(f"  {untagged} files had no readable tags — look for them "
              f"under 'Unknown Artist/Unknown Album'.")
    print("To get them into the Music app: open Music, then "
          "File > Import... and pick that folder.")


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

    try:
        asyncio.run(transfer(dest, args.keep_raw))
    except KeyboardInterrupt:
        sys.exit("\nStopped. Rerun to pick up where this left off.")


if __name__ == "__main__":
    main()
