# iPhone → Mac Music Transfer

Copies the music stored on your iPhone onto your Mac — including music that
was synced to the phone from a **different** computer, which Finder/iTunes
refuses to transfer back.

## How it works

Music synced from a computer is stored on the phone in a hidden folder
(`iTunes_Control/Music`) with scrambled file names. This tool reads that
folder directly over the USB cable (using the same file service iTunes uses
— no jailbreak needed), then reads each song's embedded metadata and files
everything neatly as:

```
Artist/
  Album/
    01 Song Title.m4a
```

## Usage

1. Plug your iPhone into your Mac with a USB cable and unlock it.
2. If the phone asks **"Trust This Computer?"**, tap **Trust**.
3. In Terminal, from this folder:

   ```bash
   ./transfer_music.sh
   ```

Your music lands in `~/Music/iPhone Transfer`, organized by artist and
album. To use a different folder:

```bash
./transfer_music.sh ~/Desktop/RecoveredMusic
```

Then in the Music app, choose **File → Import…** and pick that folder.

The first run installs two Python libraries into a private `.venv` folder
inside this project — nothing else on your Mac is touched. The transfer is
resumable: if it's interrupted, rerun it and already-copied files are
skipped.

## Checking for duplicates

After transferring, check for duplicate songs before or after importing:

```bash
# duplicates inside the transferred folder
./check_duplicates.sh

# songs you already have in your Music library
./check_duplicates.sh --against-library

# ...or point at the library folder yourself
./check_duplicates.sh ~/Music/"iPhone Transfer" --against /path/to/Media

# delete the byte-identical copies (asks for confirmation first)
./check_duplicates.sh --delete
```

The report is split into two sections, because they need different handling:

- **Identical files** — byte-for-byte the same audio **and** the same
  artist, album, title, and length. Only these are ever deletable. When
  comparing against your library, the library copy is always the one kept.
- **Same song, different file** — matching artist and title but a different
  album, length, encoding, or bitrate. These are **only reported, never
  deleted**, since a "duplicate" title is often a live, remix, or
  remastered version.

### Deletion safety rules

- **Nothing is erased.** `--delete` moves files to the **Trash**, so
  anything removed by mistake can be put back.
- **Differing artist, album, title, or length means "not a duplicate."**
  Even byte-identical files are left alone if their tags disagree. Lengths
  within 2 seconds count as matching, to allow for encoder rounding.
- **Overlapping folders are refused.** If the `--against` folder contains
  the folder being checked (or vice versa), the tool stops instead of
  running — otherwise every file would be compared against itself.
- The same file reached by two different paths is only ever counted once.

Files are only hashed when another file shares their exact byte size, so a
large library isn't read end to end unnecessarily.

`--against-library` locates your library automatically, covering the Music
app (`~/Music/Music/Media`), older iTunes installs
(`~/Music/iTunes/iTunes Media`), macOS's localized folder names
(`Media.localized`), and non-default library names — and it skips empty
folders that only look like a library.

If it guesses wrong or your library is on an external drive, find the path
under **Music → Settings → Files** ("Music Media folder location") and pass
it yourself. Either spelling works:

```bash
./check_duplicates.sh ~/Music/"iPhone Transfer" --against '/path/to/Media'
./check_duplicates.sh ~/Music/"iPhone Transfer" --against-library '/path/to/Media'
```

The Music app also has a built-in check: **File → Library → Show Duplicate
Items**. It matches on name and artist only, so it flags live and remixed
versions as duplicates too — hold **Option** and use **Show Exact Duplicate
Items** to also match on album and length.

## Requirements

- macOS with `python3` 3.9 or newer (if missing, macOS prompts you to
  install the Command Line Tools — accept, then rerun)
- A USB cable and your iPhone passcode (to unlock and trust the Mac)

Works with both the modern async `pymobiledevice3` (10.x) and the older
synchronous 4.x releases.

## Limitations

- **Apple Music streaming downloads can't be copied.** They're
  DRM-protected and stored separately. This tool recovers music that was
  synced from a computer or purchased on the iTunes Store.
- Old iTunes Store purchases with DRM (`.m4p` files) will copy over, but
  only play on a computer signed in to the Apple ID that bought them.
