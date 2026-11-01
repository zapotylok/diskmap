"""Read-only view of the system's disks.

Nothing in this package writes to a device (rules.md §S3). Commands it may run are
listed in core.probe.command.READ_ONLY; parsers refuse unfamiliar input (§S9).
"""
