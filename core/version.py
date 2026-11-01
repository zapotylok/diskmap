"""The version of diskmap: the date of the release, YYYY.MMDD (D52).

The date says how fresh a build is, which a person reading it cares about; it never repeats
and never goes down. Two numbers with a dot, so a Python package and an app bundle take it as
it is. A second release on the same day adds .2, .3, ... One place: the page of
`diskmap ui` (its About box), `diskmap --version` and the journal (§S9) read it from here.
"""

VERSION = "2026.1101.3"

# The macOS versions (major) diskmap has been tested on: where the planner's predictions of
# what diskutil does were measured on the rig (D21, D26, D48). Elsewhere it runs, and every
# plan says first that this macOS is not among them (K19). A version joins after the rig
# tests pass on it (./test.sh --rig).
TESTED_MACOS = ("26",)
