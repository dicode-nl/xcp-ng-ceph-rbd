# xcp-ng-ceph-rbd

A native **Ceph RBD storage stack for XCP-ng 8.3** — no `rbd`/`ceph` userspace on
dom0. Two components:

1. **`kernel-modules/`** — the backported in-kernel Ceph client (`libceph.ko` +
   `rbd.ko`) plus an in-kernel Kerberos5 crypto module (`krb5.ko`), giving the
   XCP-ng 8.3 dom0 kernel (Linux `4.19.0+1`) **msgr2 secure mode** and the
   **aes256k** CephX key type required by Ceph 20.2.4 (Tentacle). Ships as three
   RPMs (`rbd-module-alt`, `libceph-module-alt`, `krb5-module`).

2. **`rbdsr/`** — a SMAPIv1 Storage Repository driver (`RBDSR.py`) presenting each
   VDI as a native RBD image. Data path = the kernel modules above, mapped
   directly via `/sys/bus/rbd`; control plane = the ceph-mgr **dashboard REST
   API**. Ships as the `sm-additional-rbd` RPM.

Runtime-validated end-to-end on XCP-ng 8.3 against a Ceph 20.2.4 cluster: full
VDI lifecycle, native `VDI.revert`, CoW clone with async flatten-on-delete, a
2-host pool with a shared SR, and compute live-migration.

## Layout

```
kernel-modules/
  patches/        upstream diffs (0001-0004)
  src/            buildable module sources: libceph/ rbd/ krb5/ (+ vendored include/)
  rpm/            rbd-module-alt.spec (builds all 3 kmod RPMs) + smoketests/
rbdsr/
  src/            RBDSR.py + rbd_backend.py + rbd_sysfs.py + rbd_gc.py
  rpm/            sm-additional-rbd.spec
```

## Building

Both parts build in the official **el7** XCP-ng build container
`ghcr.io/xcp-ng/xcp-ng-build-env:8.3` (dom0 is CentOS 7 / rpm 4.11). See each
component's `README.md`. The datapath kmod build additionally needs the
`4.19.0+1` kernel-devel headers (the `ceph-build` container).

## Licensing

- `kernel-modules/` — **GPL-2.0**, derived from the Linux kernel `net/ceph`,
  `drivers/block/rbd.c` and `crypto/krb5` (see `kernel-modules/README.md` for details).
- `rbdsr/` — **LGPL-2.1**, derived from Petr Bena's CephRBDSR (see
  `rbdsr/README.md` for details).

No credentials are stored in this repo: all Ceph/dashboard secrets are supplied
at runtime through `xe` SR `device-config`.
