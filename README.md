# mdev

Linux local VM environment manager. Requires Python 3.11+, `cp`, and a separately
provided executable VM launcher. It does not build an OS, manage SDKs, download
packages, or run feature-specific tests.

```sh
mdev create default --image out/artifacts/disk.img --runner scripts/runner.sh
mdev list
mdev run default
mdev remove default --yes
```

The default store is `out/develop/` relative to the current directory. Use
`mdev --directory /path/to/store list` from elsewhere. Each VM owns a descriptor,
private disk, firmware state and logs. The launcher receives `MDEV_IMAGE` and
`MDEV_RUNTIME_DIR`; it must use the private image, preserve that directory, run
in the foreground, and clean up emulator processes on exit. mdev never writes
the input image. Launchers are trusted executable code.

By default the private disk is refreshed on **every boot**. Guest disk changes
are intentionally discarded on the next launch; the CLI prints this explicitly.
Firmware variables and logs stay in the VM environment. A registered image path
may be rebuilt, and the next boot uses its new contents.

Use `create ... --persistent` for installer tests or guest data that must survive
reboots. If its input image changes, mdev refuses to reset a persistent disk:
create a differently named VM instead. Removal requires `--yes` and refuses a
running VM. Live locks prevent simultaneous runs and determine `list` status;
there is no PID-based kill operation. Interrupts are forwarded to the launcher's
process group. Different VMs can run simultaneously if their launchers support it.

`create --if-missing` is idempotent only when the complete configuration matches.
Local descriptor paths are machine-specific VM state, not portable build locks.

Tests: `python3 -m unittest discover -s tests -v`.
