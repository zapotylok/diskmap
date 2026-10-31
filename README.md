<p align="center">
  <img src="assets/icon.svg" width="96" height="96" alt="">
</p>

<h1 align="center">diskmap</h1>

<p align="center">
  A partition map and layout planner for external drives on macOS.<br>
  Every change is a <code>diskutil</code> command you see before it runs.
</p>

---

## Why

Disk Utility can split a drive into several partitions only for APFS, HFS+, exFAT and FAT.
`diskutil` in Terminal can do much more, including NTFS and ext4 through third-party
formatters, but it draws no picture, previews nothing, and expects you to keep its size
syntax, the order of operations and their consequences in your head.

diskmap closes that gap. It shows the partitions of a drive as a map, lets you lay them
out, and turns the layout into a plan: a list of ordinary commands, each marked as
reversible, irreversible or destroying a volume. You read the plan, then apply it — or
copy it and run it by hand. The commands are the feature, not an implementation detail.

## What it does

- **Show** every partition of every disk: offsets, sizes, file systems, labels, free space.
- **Repartition** an external drive, **add** partitions after the last one or into a gap,
  **shrink** or **grow** an APFS or HFS+ partition in place.
- **Format** with what macOS provides (APFS, HFS+, exFAT, FAT32) and with third-party
  tools when they are installed: NTFS (ntfsprogs or Paragon), ext4 (e2fsprogs or Paragon).
  `diskmap doctor` tells apart "a format can be created" from "a volume of it then mounts".
- **Back up and restore** the partition map before every write; a journal records each run.
- **Mount and unmount** partitions.
- Works from the command line (`diskmap show | plan | apply | doctor | …`) and from a local
  web page (`diskmap ui`) that draws the map and opens the plan in Terminal.

## How it keeps your disks safe

- **Plan → Preview → Apply.** Nothing is written without a plan you have seen.
  The preview can be pasted into Terminal and does the same thing.
- **Read-only by default.** Writing is a separate command that runs under `sudo`
  in Terminal and asks you to type the disk's name — and, when a volume is destroyed, its label.
- **Internal and startup disks are never changed.** They are shown, read-only, with the
  reason. A disk can be changed only when it is positively known to be external.
- **The partition map is backed up before every change** — the map, not the files on it.
  Back up anything you cannot lose before you change a drive.
- **No code of its own writes to a disk.** Every change is made by tools that come with
  macOS — `diskutil` above all — or by the formatter a file system needs.
- **No network.** diskmap sends nothing anywhere. `diskmap ui` listens on `127.0.0.1` only.

## Status

diskmap is in active development and has not had a public release yet. Releases will
appear on the [Releases](../../releases) page of this repository; the source code will be
published here later.

It is developed and tested on macOS 26 on Apple silicon.

## Versions

A version is the date of its release: `2026.1031` is October 31, 2026. A second release on
the same day adds `.2`. You can tell at a glance how fresh a build is.

## Feedback and support

Questions, bugs and ideas: [open an issue](../../issues) or write to
[lexx.vit@gmail.com](mailto:lexx.vit@gmail.com?subject=diskmap).

diskmap is free. If it saved you some time, you can
[buy me a coffee](https://buymeacoffee.com/VitaliZapatylak).

## Authors

Vitali Zapatylak and Claude (Anthropic).

## License

[MIT](LICENSE). diskmap comes with no warranty: changing a partition map can destroy data.
