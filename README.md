# xcp-ng-ceph-rbd

A native **Ceph RBD storage stack for XCP-ng 8.3** — the data path is the
in-kernel Ceph client, so no `rbd`/`ceph` userspace is required on dom0. Three
components:

1. **`kernel-modules/`** — the backported in-kernel Ceph client (`libceph.ko` +
   `rbd.ko`) plus an in-kernel Kerberos5 crypto module (`krb5.ko`), giving the
   XCP-ng 8.3 dom0 kernel (Linux `4.19.0+1`) **msgr2 secure mode** and the
   **aes256k** CephX key type required by Ceph 20.2.4 (Tentacle). Ships as three
   RPMs (`rbd-module-alt`, `libceph-module-alt`, `krb5-module`).

2. **`rbdsr/`** — a **SMAPIv1** Storage Repository driver (`RBDSR.py`) presenting
   each VDI as a native RBD image. Data path = the kernel modules above, mapped
   directly via `/sys/bus/rbd`; control plane = the ceph-mgr **dashboard REST
   API**. Ships as the `sm-additional-rbd` RPM.

3. **`rbdvol/`** — a **SMAPIv3** plugin (SR type `rbd-vol`) with the same data
   model and datapath, reworked onto the xapi-storage volume/datapath split
   (python3). Adds an optional tapdisk datapath mode, and a **pluggable control
   plane**: the ceph-mgr **dashboard REST API** (default, needs no userspace) or
   an optional **local `librbd` backend** for hosts that do have a ceph
   userspace. Ships as `dicode-xapi-storage-volume-rbd` +
   `dicode-xapi-storage-datapath-rbd`.

Runtime-validated end-to-end on XCP-ng 8.3 against a Ceph 20.2.4 cluster: full
VDI lifecycle, native `VDI.revert`, CoW clone with async flatten-on-delete, a
2-host pool with a shared SR, and compute live-migration (SMAPIv1); the SMAPIv3
plugin reaches functional parity incl. a full root-on-`rbd-vol` VM boot.

## Layout

```
kernel-modules/
  patches/        upstream diffs (0001-0004)
  src/            buildable module sources: libceph/ rbd/ krb5/ (+ vendored include/)
  rpm/            rbd-module-alt.spec (builds all 3 kmod RPMs) + smoketests/
rbdsr/            SMAPIv1 driver
  src/            RBDSR.py + rbd_backend.py + rbd_sysfs.py + rbd_gc.py
  rpm/            sm-additional-rbd.spec
rbdvol/           SMAPIv3 plugin (SR type rbd-vol)
  volume/org.xen.xapi.storage.rbd-vol/   volume plugin (SR + Volume + GC + metadata)
  datapath/rbd/                          datapath (blkback vbd / tapdisk vbd3)
  rpm/            dicode-xapi-storage-rbd.spec (-> volume-rbd + datapath-rbd)
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
- `rbdvol/` — **LGPL-2.1** (see `rbdvol/README.md` for details).

No credentials are stored in this repo: all Ceph/dashboard secrets are supplied
at runtime through `xe` SR `device-config`.
