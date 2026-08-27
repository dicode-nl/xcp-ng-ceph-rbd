# rbdsr — Ceph RBD Storage Repository driver for XCP-ng 8.3 (SMAPIv1)

Presents each VDI as a native Ceph RADOS Block Device. **Data path** = the
in-kernel `rbd` client (the `kernel-modules/` stack: aes256k / msgr2-secure),
mapped directly via `/sys/bus/rbd` — no `rbd` binary, no `ceph-common`, no
tapdisk (raw `phy` VDIs plugged through kernel blkback). **Control plane** = the
ceph-mgr **dashboard REST API**. All config via `xe` `device-config`; the cephx
key is used only for the datapath. **Namespace == SR uuid, image == VDI uuid.**

## Files (`src/`)

| file             | role |
|------------------|------|
| `RBDSR.py`       | the SR/VDI driver (`DRIVER_TYPE=rbd`) |
| `rbd_backend.py` | control-plane: `RestBackend` (dashboard REST, JWT, version auto-negotiation) |
| `rbd_sysfs.py`   | datapath: map/unmap via `/sys/bus/rbd` (`secret=`, `_pool_ns=`, `ms_mode=`) |
| `rbd_gc.py`      | detached GC worker for async flatten-on-delete |

Lineage: adapted from Petr Bena's [CephRBDSR](https://github.com/benapetr/CephRBDSR)
(LGPL-2.1), with all `ceph`/`rbd` CLI use removed and reworked onto the ceph-mgr
dashboard REST control-plane + native krbd (`/sys/bus/rbd`) datapath.

## Capabilities

Full VDI lifecycle: CREATE/DELETE/ATTACH/DETACH, **SNAPSHOT**, **CLONE** (CoW +
lazy/async flatten), **VDI_REVERT** (native `rbd snap rollback`), RESIZE(_ONLINE),
INTRODUCE, GENERATE_CONFIG, RESET_ON_BOOT, THIN_PROVISIONING. No VDI_MIRROR (live
storage migration) — inherent to the tapdisk-less `phy` datapath; compute
live-migration on a shared SR works.

## device-config

`pool`, `mon_host`, `user`, `key` (cephx aes256k secret — datapath only),
`namespace` (default: SR uuid), `ms_mode` (default: prefer-crc — crc where the
cluster allows it, else secure; `secure` forces wire encryption), `rbd_features`
(preset `performance` (default) | `compat`, or an explicit comma-list),
`api_url` (dashboard), `api_user`, `api_secret` (dashboard account — NOT the
cephx key), `api_tls_verify`, `backend_kind` (default: vbd).

## Build the RPM

noarch, built in the el7 container. Regenerate the source tarball from `src/`
first (the spec's `Source0` is `%{name}-%{version}.tar.gz` extracting to
`sm-additional-rbd-<ver>/`):

```sh
mkdir -p _rpm/{SOURCES,SPECS} && cp rpm/sm-additional-rbd.spec _rpm/SPECS/
stage=$(mktemp -d)/sm-additional-rbd-2.0; mkdir -p "$stage"
cp src/*.py "$stage"/
tar -czf _rpm/SOURCES/sm-additional-rbd-2.0.tar.gz -C "$(dirname "$stage")" sm-additional-rbd-2.0
podman run --rm -v "$PWD/_rpm":/work:z ghcr.io/xcp-ng/xcp-ng-build-env:8.3 \
  bash -lc 'rpmbuild --define "_topdir /work" -bb /work/SPECS/sm-additional-rbd.spec'
```

## Install

```sh
rpm -ivh sm-additional-rbd-*.noarch.rpm    # requires: sm, rbd-module-alt, python3, kmod
xe-toolstack-restart                        # on EVERY pool host — see note
```

**Registration note:** XCP-ng 8.3 has no SR-driver auto-discovery — `sm-plugins=`
in `/etc/xapi.conf` is a required whitelist (verified: xapi only reads a driver
file when its type is listed; removing it leaves a stale, dead `sm-list` record).
The RPM's `%post` adds `rbd` to the whitelist (idempotent) and `%postun` removes
it on full erase; a `xe-toolstack-restart` per host is required to activate.
