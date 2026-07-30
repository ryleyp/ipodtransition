#!/usr/bin/env python3
"""Find duplicate songs in a music folder.

Two kinds of duplicates get reported separately, because they need very
different handling:

1. Identical files - byte-for-byte the same audio AND the same artist,
   album, title and length. Only these are ever deletable.

2. Same song, different file - matching artist and title but a different
   album, length, encoding or bitrate. These are reported only, never
   deleted, because a differing album or length usually means a live,
   remixed or remastered version rather than a copy.

Deletions move files to the Trash, never erase them outright, so anything
removed by mistake can be put back.

Typical use after a transfer:

    # what's duplicated inside the transferred folder?
    ./find_duplicates.py ~/Music/"iPhone Transfer"

    # which transferred songs do I already have in my library?
    ./find_duplicates.py ~/Music/"iPhone Transfer" --against ~/Music/Music/Media.localized

    # move the byte-identical copies to the Trash (asks first)
    ./find_duplicates.py ~/Music/"iPhone Transfer" --delete
"""

import argparse
import hashlib
import re
import shutil
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
# Lengths within this many seconds of each other count as the same length.
DURATION_TOLERANCE = 2.0


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


def move_to_trash(path: Path):
    """Move a file to ~/.Trash rather than erasing it."""
    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    target = trash / path.name
    counter = 1
    while target.exists():
        target = trash / f"{path.stem} ({counter}){path.suffix}"
        counter += 1
    shutil.move(str(path), str(target))
    return target


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

    @property
    def identity(self):
        """Artist, album, title and rounded length, for the delete gate."""
        return (
            normalize(self.artist),
            normalize(self.album),
            normalize(self.title),
            None if self.duration is None else round(self.duration),
        )

    def describe(self):
        if self.size < 1_048_576:
            parts = [f"{self.size / 1024:.0f} KB"]
        else:
            parts = [f"{self.size / 1_048_576:.1f} MB"]
        if self.bitrate:
            parts.append(f"{round(self.bitrate / 1000)} kbps")
        if self.duration:
            parts.append(
                f"{int(self.duration // 60)}:{int(self.duration % 60):02d}"
            )
        if self.album:
            parts.append(self.album)
        return ", ".join(parts)


def metadata_agrees(group):
    """True only if every track agrees on artist, album, title and length.

    A byte-identical group always passes. The check exists so that anything
    which reached a group some other way - a hash collision, or the same
    file reached by two different paths - can never be deleted silently.
    """
    # Compare real tag values only. Files with no tags at all are not in
    # disagreement - identical bytes already prove they are the same audio.
    artists = {normalize(t.artist) for t in group}
    albums = {normalize(t.album) for t in group}
    titles = {normalize(t.title) for t in group}
    if len(artists) > 1 or len(albums) > 1 or len(titles) > 1:
        return False
    lengths = [t.duration for t in group if t.duration is not None]
    if lengths and max(lengths) - min(lengths) > DURATION_TOLERANCE:
        return False
    return True


def scan(folder: Path, label, seen):
    """Return Track objects for audio files under folder, skipping any file
    already seen in an earlier scan (the same file must never be compared
    against itself)."""
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")
    tracks = []
    duplicated_paths = 0
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            key = path.resolve()
        except OSError:
            key = path.absolute()
        if key in seen:
            duplicated_paths += 1
            continue
        seen.add(key)
        tracks.append(Track(path))
    print(f"Scanned {len(tracks)} audio files in {label}: {folder}")
    if duplicated_paths:
        print(f"  (ignored {duplicated_paths} files already covered by "
              f"another scan)")
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


# Verdicts for a transferred song matched against a library song, ordered
# from strongest to weakest evidence that they are the same thing.
IDENTICAL = "identical file"
SAME_RECORDING = "same recording"
DIFFERENT_VERSION = "different version"


def classify_match(transfer, library):
    """Say how strongly a transferred track matches a library track."""
    if transfer.size == library.size:
        if transfer.hash is None:
            transfer.hash = file_hash(transfer.path)
        if library.hash is None:
            library.hash = file_hash(library.path)
        if transfer.hash == library.hash:
            return IDENTICAL
    # Same song re-encoded: artist, album and title must agree, and the
    # lengths must be within DURATION_TOLERANCE of each other.
    if metadata_agrees([transfer, library]):
        return SAME_RECORDING
    return DIFFERENT_VERSION


RANK = {IDENTICAL: 0, SAME_RECORDING: 1, DIFFERENT_VERSION: 2}


def compare_to_library(folder_tracks, library_tracks, folder, library_root):
    """Report which transferred songs already exist in the library.

    Matching is by artist and title first, then each candidate pair is
    classified. Returns the transfer-side tracks that are safely
    redundant, keyed by verdict.
    """
    index = defaultdict(list)
    for track in library_tracks:
        key = track.song_key
        if key:
            index[key].append(track)

    matched = []  # (transfer_track, library_track, verdict)
    for track in folder_tracks:
        candidates = index.get(track.song_key or (), [])
        if not candidates:
            continue
        best = min(
            ((c, classify_match(track, c)) for c in candidates),
            key=lambda pair: RANK[pair[1]],
        )
        matched.append((track, best[0], best[1]))

    print("== Already in your library ==============================")
    if not matched:
        print("  Nothing in the transferred folder matches a song in your")
        print("  library by artist and title.")
        return {}

    by_verdict = defaultdict(list)
    for track, library_track, verdict in matched:
        by_verdict[verdict].append((track, library_track))

    headings = {
        IDENTICAL: "Identical files — same bytes, definitely the same song",
        SAME_RECORDING: "Same artist, album, title and length — a re-encode "
                        "of the same song",
        DIFFERENT_VERSION: "Same artist and title, but album or length "
                           "differs — likely a DIFFERENT version",
    }
    for verdict in (IDENTICAL, SAME_RECORDING, DIFFERENT_VERSION):
        entries = by_verdict.get(verdict)
        if not entries:
            continue
        print(f"\n  -- {headings[verdict]} ({len(entries)}) --")
        for track, library_track in entries:
            print(f"\n  {track.title or track.path.stem} "
                  f"— {track.artist or 'Unknown Artist'}")
            show(track, folder, "TRANSFER")
            show(library_track, library_root, "LIBRARY ")

    redundant = len(by_verdict.get(IDENTICAL, [])) + \
        len(by_verdict.get(SAME_RECORDING, []))
    versions = len(by_verdict.get(DIFFERENT_VERSION, []))
    print(f"\n  {len(matched)} of {len(folder_tracks)} transferred songs "
          f"match something in your library.")
    print(f"  {redundant} {'is' if redundant == 1 else 'are'} safely "
          f"redundant; {versions} "
          f"{'looks' if versions == 1 else 'look'} like a different version "
          f"and {'is' if versions == 1 else 'are'} kept.")
    return by_verdict


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
        help="Move byte-identical duplicates to the Trash after confirming. "
             "Never touches 'same song, different file' matches.",
    )
    parser.add_argument(
        "--delete-already-in-library",
        action="store_true",
        help="With --against: also move transferred songs to the Trash when "
             "the library already has the same artist, album, title and "
             "length, even if the file itself differs. Songs whose album or "
             "length differs are always kept.",
    )
    args = parser.parse_args()
    if args.delete_already_in_library and not args.against:
        sys.exit("--delete-already-in-library needs --against "
                 "(or --against-library) to compare with.")

    folder = Path(args.folder).expanduser().resolve()
    library_root = None
    if args.against:
        library_root = Path(args.against).expanduser().resolve()
        # An overlapping pair would make every file its own "duplicate".
        if library_root == folder:
            sys.exit("--against must point at a different folder.")
        if folder.is_relative_to(library_root):
            sys.exit(
                f"Refusing to run: the folder being checked\n  {folder}\n"
                f"is inside the library folder\n  {library_root}\n"
                "Every file would be compared against itself. Point "
                "--against at your Music library's media folder, not at a "
                "parent of the folder you are checking."
            )
        if library_root.is_relative_to(folder):
            sys.exit(
                f"Refusing to run: the library folder\n  {library_root}\n"
                f"is inside the folder being checked\n  {folder}\n"
                "Every file would be compared against itself. Point "
                "--against at a separate folder."
            )

    seen = set()
    folder_tracks = scan(folder, "folder", seen)
    library_tracks = scan(library_root, "library", seen) if library_root else []
    tracks = folder_tracks + library_tracks
    if not tracks:
        sys.exit("No audio files found.")

    library_matches = {}
    if library_root:
        print()
        library_matches = compare_to_library(
            folder_tracks, library_tracks, folder, library_root
        )
        print()
        print("== Duplicates within the transferred folder itself ========")

    def keep_rank(track):
        """Sort key picking the best copy to keep (lowest sorts first)."""
        return (
            bool(COPY_SUFFIX.search(track.path.stem)),  # avoid "name (2)"
            len(str(track.path)),  # prefer the simpler path
            str(track.path),
        )

    # These sections only ever look inside the transferred folder; anything
    # shared with the library is covered by the section above.
    print()
    identical = group_identical(folder_tracks)
    deletable = []
    withheld = []  # identical bytes but disagreeing metadata
    shown_sets = 0
    if identical:
        print("== Identical files ==============================")
        for group in sorted(identical, key=lambda g: str(g[0].path)):
            if not metadata_agrees(group):
                withheld.append(group)
                continue
            group.sort(key=keep_rank)
            keeper, extras = group[0], group[1:]
            shown_sets += 1
            print(f"\n  {keeper.title or keeper.path.stem} "
                  f"— {keeper.artist or 'Unknown Artist'}")
            show(keeper, folder, "KEEP  ")
            keeper_path = keeper.path.resolve()
            for extra in extras:
                # Belt and braces: never delete the file we just kept.
                if extra.path.resolve() == keeper_path:
                    continue
                show(extra, folder, "DUPE  ")
                deletable.append(extra)
        if shown_sets == 0:
            print("  None found.")
        else:
            wasted = sum(t.size for t in deletable)
            copies = "copy" if len(deletable) == 1 else "copies"
            print(f"\n  {shown_sets} sets, {len(deletable)} deletable "
                  f"{copies}, {wasted / 1_048_576:.1f} MB reclaimable.")
    else:
        print("== Identical files ==============================")
        print("  None found.")

    if withheld:
        print(f"\n  {len(withheld)} sets had identical audio but "
              f"disagreeing artist/album/title/length —")
        print("  left alone rather than deleted. Review them by hand:")
        for group in withheld:
            print()
            for track in group:
                show(track, folder)

    already_grouped = {t.path for group in identical for t in group}
    same_song = group_same_song(folder_tracks, skip=already_grouped)
    print()
    print("== Same song, different file ==============================")
    if same_song:
        for group in sorted(same_song, key=lambda g: str(g[0].path)):
            group.sort(key=lambda t: -(t.bitrate or 0))
            identities = {t.identity for t in group}
            verdict = ("looks like the same recording" if len(identities) == 1
                       else "album/length differ — probably NOT a duplicate")
            print(f"\n  {group[0].title or group[0].path.stem} "
                  f"— {group[0].artist or 'Unknown Artist'}")
            print(f"    ({verdict})")
            for track in group:
                show(track, folder)
        print("\n  None of these are deleted, with or without --delete: a "
              "differing\n  album or length usually means a live, remix or "
              "remastered version.")
    else:
        print("  None found.")

    # Songs the library already has, if the caller opted into removing them.
    redundant = []
    if args.delete_already_in_library:
        for verdict in (IDENTICAL, SAME_RECORDING):
            for track, _library_track in library_matches.get(verdict, []):
                redundant.append(track)

    queued = deletable + [t for t in redundant if t not in deletable]

    if not (args.delete or args.delete_already_in_library):
        hints = []
        if deletable:
            hints.append(f"--delete moves the {len(deletable)} identical "
                         f"copies above to the Trash")
        safe_matches = (len(library_matches.get(IDENTICAL, []))
                        + len(library_matches.get(SAME_RECORDING, [])))
        if safe_matches:
            songs = "song" if safe_matches == 1 else "songs"
            hints.append(f"--delete-already-in-library moves the "
                         f"{safe_matches} {songs} your library already has "
                         f"to the Trash")
        if hints:
            print("\nNothing has been changed. To act on this report:")
            for hint in hints:
                print(f"  {hint}")
        return

    if not queued:
        print("\nNothing to delete.")
        return
    print(f"\nAbout to move {len(queued)} files to the Trash "
          f"(nothing is erased permanently).")
    if deletable:
        print(f"  {len(deletable)} duplicated inside the transferred folder "
              f"(one copy of each is kept)")
    if redundant:
        print(f"  {len(redundant)} already in your music library")
    answer = input("Type 'yes' to continue: ").strip().lower()
    if answer != "yes":
        print("Cancelled. Nothing was moved.")
        return
    removed = 0
    for track in queued:
        try:
            move_to_trash(track.path)
            removed += 1
        except OSError as err:
            print(f"  ! Could not move {track.path} to the Trash: {err}",
                  file=sys.stderr)
    print(f"Moved {removed} duplicate files to the Trash. "
          f"Recover them from there if this was not what you wanted.")

    # Clean up any album/artist folders left empty by the move.
    for path in sorted(folder.rglob("*"), key=lambda p: -len(p.parts)):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


if __name__ == "__main__":
    main()
