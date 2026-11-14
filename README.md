# mdev

Linux local VM environment manager. Requires Python 3.11+, `cp`, and an executable
VM launcher. mdev does not build an OS, manage SDKs, download packages, or run
feature-specific guest tests.

## Everyday use

```sh
mdev create default --image out/artifacts/disk.img --runner scripts/runner.sh
mdev list
mdev doctor default
mdev run default                  # Foreground, console also recorded
```

For a background VM:

```sh
mdev start default
mdev show default
mdev logs default --follow
mdev stop default
```

`start` waits for a live supervisor, **not** for guest OS boot completion. Startup
errors and subsequent backend failures remain in logs and `show`'s last-run
record. `run` returns the launcher's actual exit code (128 + signal when killed).
`list --json` and `show NAME --json` provide machine-readable state. Current lock
state and the historical result are separate: a stale record never establishes
that a VM is running.

The default store is `out/develop/` relative to the current directory. Use
`mdev --directory /path/to/store list` from elsewhere. Each VM contains:

```text
vm.json                  # Configuration (machine-local absolute paths)
state.json               # Last launch, timestamps, exit code, stop reason
console.log              # Retained launcher stdout/stderr from every boot
supervisor.log           # Background preparation and supervisor diagnostics
runtime/
  disk.img               # Private writable guest disk
  image.sha256           # Base-image identity for persistent disks
  ...                    # Firmware variables and backend-specific logs
```

Use `logs NAME --lines 200` for console history or `--stream supervisor` for
background-launch errors. `--follow` finishes after the VM stops. Console history
is appended rather than overwritten. Log files are not rotated automatically;
remove a stopped VM when its retained state is no longer needed.

## Guest disk lifetime

By default the private disk is refreshed on **every boot**. Guest disk changes
are discarded on the next launch; the CLI prints this explicitly. Firmware
variables and backend runtime state are preserved until reset. The input image
is never passed to the launcher as a writable disk. Fresh preparation uses
reflinks/sparse copying and a source-change check, avoiding full multi-GB hashing.

For installer tests or guest data that must survive reboots:

```sh
mdev create installer-test --image path/to/image.img --runner scripts/runner.sh --persistent
mdev run installer-test
```

Persistent disks retain writes. If the base image contents change, mdev refuses
to reuse or reset them silently. To deliberately discard the guest disk and
firmware and optionally change the input image:

```sh
mdev reset installer-test --image path/to/new-image.img --yes
```

`reset` retains top-level console/supervisor logs and the last result; backend
logs inside `runtime/` are removed with the resettable runtime. `remove NAME
--yes` deletes the entire stopped environment. Both operations refuse live VMs.

## Configuration

```sh
mdev configure default --env DEBUG_QEMU_GUI=n
mdev configure default --unset-env DEBUG_QEMU_GUI
mdev configure default --runner /path/to/another-launcher --working-directory /path/to/work
```

The launcher path, image path, working directory, persistence mode and explicit
environment are recorded in `vm.json`. Settings may be changed only while stopped.
Use `--persistent` or `--fresh` to change disk lifetime **after resetting any existing
disk**. Changing an already prepared persistent base image also requires `reset`.
`create --if-missing` is idempotent only when its complete configuration matches.

Backend settings are generic environment variables (`--env KEY=VALUE`, accepted
by `create` and `configure`). `MDEV_*` variables are reserved. The launcher's
working directory defaults to its parent directory. Explicit settings override
the inherited host environment; remaining environment variables are inherited,
so VM descriptors are local configuration, not reproducible build locks.

`doctor` checks descriptor validity, the input image, executable launcher, working
directory and control connectivity when running. It does not certify a bootable
image or inspect a launcher's product-specific prerequisites.

## Launcher contract

The launcher receives `MDEV_IMAGE` (the private raw disk) and `MDEV_RUNTIME_DIR`
(the VM's runtime directory). It must preserve that directory, use the supplied
disk, remain in the foreground and keep its emulator/helpers in its process group.
It must not daemonize them into other sessions or close the inherited lifecycle
lock before its emulator exits. Launchers are **trusted local executable code**,
not sandboxed packages. mdev never runs scripts found inside an input image.

mdev captures stdout/stderr and supervises the whole launcher process group.
Interrupts are forwarded; helpers left behind by an exited launcher are terminated.
The leader remains unreaped until group cleanup so its process-group number cannot
be reused by an unrelated process during cleanup.

`stop` asks the live supervisor to send SIGTERM, waits up to 10 seconds, then uses
SIGKILL if necessary. Change the grace period with `--timeout`; `--force` requests
SIGKILL immediately. **This terminates the emulator, not a guest OS shutdown**;
shut down from inside the guest first when filesystem consistency matters.

Control uses a per-user Linux abstract Unix socket with kernel peer-credential
checks. It supports status/stop only. No command signals a PID loaded from a
state file. Per-VM locks prevent duplicate launches, configuration changes and
removal while active. The lock is also inherited by the launcher. If a supervisor
is forcibly killed while a launcher survives, commands fail safely rather than
signal a recorded PID; inspect the local processes manually. Abstract sockets
work with long store paths and disappear automatically on close.

## mochiOS integration

`mmake bootstrap` installs the checkout-backed command. `mmake run` builds its
image using the current OS graph and starts a named environment via mdev;
`mmake run-existing` skips the build. Existing VM settings are preserved. The
mochiOS launcher owns Secure Boot, hardware and display policy; mdev does not.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests cover lifecycle races, locking, persistent/fresh disks, authenticated
control, stale-PID safety, forced stops, descendant cleanup, backend failures,
logs, configuration and a real QEMU/TCG launch when QEMU is installed. They use
private tiny test images, never physical disks. Unix socket access is required.
