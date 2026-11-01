"""Who holds files open under a mount point: `lsof +D` (T-011, T-050). Read-only, best-effort.

Asked only to explain a volume that would not unmount — diskutil names the process
("dissented by PID N"), lsof names the files. Its output is shown as it is, never parsed
into a decision: lsof exits 1 both when nothing is open and on some errors, and `+D`
walks the whole tree, so it is the report's detail, not a check.
"""

from __future__ import annotations

from core.probe.command import run_read_only_answer

LSOF = ("/usr/sbin/lsof", "-n", "-P", "+D")


def read_holders(mount_point: str) -> str:
    """lsof's lines for MOUNT_POINT; "" when it lists nothing or cannot run."""
    try:
        _, out, _ = run_read_only_answer([*LSOF, mount_point])
    except OSError:
        return ""
    return out.decode(errors="replace").strip()
