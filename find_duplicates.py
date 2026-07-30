#!/usr/bin/env python3
"""Find duplicate songs in a music folder.

Two kinds of duplicates get reported separately, because they need very
different handling:

1. Identical files - byte-for-byte the same audio. These are always safe to
   collapse down to one copy, so --delete will do it for you.

2. Same song, different file - matching artist and title but a different
   encoding, bitrate, or tag set. Only you can say whether these are real
   duplicates or a studio/live/remix pair, so they are only ever reported.

Typical use after a transfer:

    # what's duplicated inside the transferred folder?
    ./find_duplicates.py ~/Music/"iPhone Transfer"

    # which transferred songs do I already have in my library?
    ./find_duplicates.py ~/Music/"iPhone Transfer" --against ~/Music/Music/Media

    # delete the byte-identical copies (asks first)
    ./find_duplicates.py ~/Music/"iPhone Transfer" --delete
"""

import argparse
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import mutagen
except ImportError:
    sys.exit(
        "mutagen is not installed. Run this via ./check_duplicates.sh, "
        "or: pip install mutagen"
    )

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".m4p", ".aac", ".aif", ".aiff",
    ".wav", ".alac", ".flac", ".caf",
}
PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
WHITESPACE = re.compile(r"\s+")
# Trailing " (2)" that dedupe passes and file managers like to append.
COPY_SUFFIX = re.compile(r"\s*\((\d+)\)$")


def plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def normalize(text):
    """Loosely normalize a tag value for comparison."""
    text = (text or "").strip().lower()
    text = COPY_SUFFIX.sub("", text)
    text = PUNCTUATION.sub(" ", text)
    return WHITESPACE.sub(" ", text).strip()


def file_hash(path):
    """SHA-256 of the whole file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def first(tags, key):
    values = tags.get(key) if tags else None
    return str(values[0]) if values else None


class Track:
    """One audio file plus the bits of metadata we compare on."""

    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size
        self.hash = None  # filled in lazily, it is the expensive part
        try:
            audio = mutagen.File(path, easy=True)
        except Exception:  # noqa: BLE001 - unreadable means "no usable tags"
            audio = None
        tags = audio.tags if audio else None
        self.artist = first(tags, "albumartist") or first(tags, "artist")
        self.title = first(tags, "title")
        self.album = first(tags, "album")
        info = getattr(audio, "info", None)
        self.duration = getattr(info, "length", None)
        self.bitrate = getattr(info, "bitrate", None)

    @property
    def song_key(self):
        """Key identifying the same song across different encodings."""
        artist = normalize(self.artist)
        # Fall back to the filename when a file carries no title tag, so
        # untagged files still cluster with their own copies.
        title = normalize(self.title) or normalize(self.path.stem)
        if not artist and not title:
            return None
        return (artist, title)

    def describe(self):
        if self.size < 1_048_576:
            parts = [f"{self.size / 1024:.0f} KB"]
        else:
            parts = [f"{self.size / 1_048_576:.1f} MB"]
        if self.bitrate:
            parts.append(f"{round(self.bitrate / 1000)} kbps")
        if self.duration:
            parts.append(f"{int(self.duration // 60)}:{int(self.duration % 60):02d}")
        if self.album:
            parts.append(self.album)
        return ", ".join(parts)


def scan(folder: Path, label):
    """Return Track objects for every audio file under folder."""
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")
    tracks = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            tracks.append(Track(path))
    print(f"Scanned {len(tracks)} audio files in {label}: {folder}")
    return tracks


def group_identical(tracks):
    """Group tracks that are byte-for-byte identical.

    Files are hashed only when another file shares their exact size, which
    keeps a big library from being read end to end for no reason.
    """
    by_size = defaultdict(list)
    for track in tracks:
        by_size[track.size].append(track)

    groups = defaultdict(list)
    for size, candidates in by_size.items():
        if len(candidates) < 2:
            continue
        for track in candidates:
            if track.hash is None:
                track.hash = file_hash(track.path)
            groups[(size, track.hash)].append(track)
    return [g for g in groups.values() if len(g) > 1]


def group_same_song(tracks, skip):
    """Group tracks that look like the same song in a different file."""
    groups = defaultdict(list)
    for track in tracks:
        if track.path in skip:
            continue
        key = track.song_key
        if key:
            groups[key].append(track)
    return [g for g in groups.values() if len(g) > 1]


def show(track, root, marker=" "):
    try:
        shown = track.path.relative_to(root)
    except ValueError:
        shown = track.path
    print(f"    {marker} {shown}")
    print(f"        {track.describe()}")


def main():
    parser = argparse.ArgumentParser(
        description="Find duplicate songs in a music folder.",
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default=str(Path.home() / "Music" / "iPhone Transfer"),
        help="Folder to check (default: ~/Music/iPhone Transfer)",
    )
    parser.add_argument(
        "--against",
        metavar="LIBRARY",
        help="Also compare against an existing library folder. Only copies "
             "inside FOLDER are ever offered for deletion.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete byte-identical duplicates after confirming. Never "
             "touches 'same song, different file' matches.",
    )
    args = parser.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    tracks = scan(folder, "folder")
    library_root = None
    if args.against:
        library_root = Path(args.against).expanduser().resolve()
        if library_root == folder:
            sys.exit("--against must point at a different folder.")
        tracks += scan(library_root, "library")
    if not tracks:
        sys.exit("No audio files found.")

    def in_scope(track):
        """True if deleting this file is allowed (inside FOLDER only)."""
        return track.path.is_relative_to(folder)

    def keep_rank(track):
        """Sort key picking the best copy to keep (lowest sorts first)."""
        return (
            in_scope(track),  # a copy already in the library wins
            bool(COPY_SUFFIX.search(track.path.stem)),  # avoid "name (2)"
            len(str(track.path)),  # prefer the simpler path
            str(track.path),
        )

    print()
    identical = group_identical(tracks)
    deletable = []
    protected = False  # duplicates found only inside the --against library
    if identical:
        print(f"== Identical files ({plural(len(identical), 'set')}) "
              f"==============================")
        for group in sorted(identical, key=lambda g: str(g[0].path)):
            group.sort(key=keep_rank)
            keeper, extras = group[0], group[1:]
            print(f"\n  {keeper.title or keeper.path.stem} "
                  f"— {keeper.artist or 'Unknown Artist'}")
            show(keeper, folder, "KEEP  ")
            for extra in extras:
                allowed = in_scope(extra)
                show(extra, folder, "DUPE  " if allowed else "dupe* ")
                if allowed:
                    deletable.append(extra)
                else:
                    protected = True
        wasted = sum(t.size for t in deletable)
        copies = "copy" if len(deletable) == 1 else "copies"
        print(f"\n  {len(deletable)} deletable {copies}, "
              f"{wasted / 1_048_576:.1f} MB reclaimable.")
        if protected:
            print("  (* outside the checked folder — not offered "
                  "for deletion)")
    else:
        print("== Identical files ==============================")
        print("  None found.")

    already_grouped = {t.path for group in identical for t in group}
    same_song = group_same_song(tracks, skip=already_grouped)
    print()
    if same_song:
        print(f"== Same song, different file "
              f"({plural(len(same_song), 'set')}) — review these yourself ==")
        for group in sorted(same_song, key=lambda g: str(g[0].path)):
            group.sort(key=lambda t: -(t.bitrate or 0))
            print(f"\n  {group[0].title or group[0].path.stem} "
                  f"— {group[0].artist or 'Unknown Artist'}")
            for track in group:
                show(track, folder)
        print("\n  These are NOT deleted automatically: different lengths "
              "or albums\n  usually mean a live, remix, or remastered "
              "version rather than a copy.")
    else:
        print("== Same song, different file ==============================")
        print("  None found.")

    if not args.delete:
        if deletable:
            print(f"\nRerun with --delete to remove the "
                  f"{len(deletable)} identical copies shown above.")
        return

    if not deletable:
        print("\nNothing to delete.")
        return
    print(f"\nAbout to delete {len(deletable)} byte-identical files "
          f"(one copy of each song is kept).")
    answer = input("Type 'yes' to continue: ").strip().lower()
    if answer != "yes":
        print("Cancelled. Nothing was deleted.")
        return
    removed = 0
    for track in deletable:
        try:
            track.path.unlink()
            removed += 1
        except OSError as err:
            print(f"  ! Could not delete {track.path}: {err}", file=sys.stderr)
    print(f"Deleted {removed} duplicate files.")

    # Clean up any album/artist folders left empty by the deletions.
    for path in sorted(folder.rglob("*"), key=lambda p: -len(p.parts)):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


if __name__ == "__main__":
    main()
