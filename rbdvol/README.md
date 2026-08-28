# rbdvol — Ceph RBD Storage Repository for XCP-ng (SMAPIv3)

A **SMAPIv3** storage plugin presenting each VDI as a native Ceph RADOS Block
Device — the modern, forward-looking sibling of the SMAPIv1 driver in
[`../rbdsr`](../rbdsr). Same data model and Ceph datapath (aes256k / msgr2-secure
krbd), reworked onto the xapi-storage volume/datapath plugin split. **python3,
standalone (no libcow).** SR type: **`rbd-vol`**.

Functionally at parity with the SMAPIv1 driver (full VDI lifecycle, snapshot /
clone / native revert, async flatten-on-delete GC, VDI metadata, boots a VM
root-off-it) **plus** an optional tapdisk datapath mode that SMAPIv1 lacks.

## Two components

SMAPIv3 splits storage into a **volume plugin** (manages the SR + volumes) and a
**datapath** (turns a volume into a block device for a domain). They are separate
plugins/processes and are packaged as separate RPMs.

### `volume/org.xen.xapi.storage.rbd-vol/` — the volume plugin
**Pluggable control plane** (`device-config:backend=`): the ceph-mgr **dashboard
REST API** (`rest`, the default — needs no ceph userspace on dom0) or a **local
`librbd` backend** (`local` — uses the `python3-rbd` / `python3-rados` bindings,
for hosts that *do* have a working ceph userspace; no dashboard required). The
data path (krbd map) is unchanged either way.

| file | role |
|------|------|
| `plugin.py`      | `Plugin.Query` / `diagnostics` (identity + advertised features) |
| `sr.py`          | `SR.*` (create/attach/detach/destroy/stat/ls/probe/set_name/set_description) |
| `volume.py`      | `Volume.*` (create/destroy/snapshot/clone/revert/resize/stat/set/unset/set_name/set_description) |
| `srmeta.py`      | per-host SR metadata store (`/var/run/rbd-vol/<sr>/meta.json`) — stashes the device-config so per-volume calls (which only get the SR handle) and the datapath can recover pool/namespace/cephx key; keeps secrets out of URIs |
| `gcjob.py` + `rbd_gc.py` | async flatten-on-delete GC (trash-rename + detached worker + sweep-on-scan) |
| `rbdvol_lib.py`  | shared helpers (URI scheme, volume dicts, key parsing, image-meta names) |
| `rbd_backend.py` | control-plane backends — `RestBackend` (dashboard REST) and `LocalBackend` (librbd/librados bindings, lazily imported); `make_backend()` selects on `backend=` |

The per-method executables (`SR.create`, `Volume.snapshot`, …) are symlinks to the
dispatcher modules; they switch on `argv[0]`.

### `datapath/rbd/` — the datapath (URI scheme `rbd`)

| file | role |
|------|------|
| `plugin.py`   | `Plugin.Query` / `diagnostics` |
| `datapath.py` | `Datapath.attach/detach/activate/…` — maps the image via krbd and returns the backend |
| `rbd_sysfs.py`| the krbd map/unmap datapath (vendored from `../rbdsr/src`) |
| `srmeta.py`   | reads the SR metadata store for mon/cephx config |

**Two serving modes** (a *parameter*, not two plugins — xenopsd picks from the
returned `implementations`):
- **blkback** (default) → `['XenDisk', {backend_type: 'vbd', params: '/dev/rbdN'}]`
  — the raw kernel device via blkback, tapdisk-less, max performance.
- **tapdisk** → `tap-ctl create -a aio:/dev/rbdN` → `/dev/xen/blktap-2/tapdevN`
  returned as `backend_type: 'vbd3'` — the "accepted" xcp-ng path; unlocks CBT
  (`tap-ctl -C`) and SXM mirror (`tap-ctl -2`) at the cost of a userspace hop.

Selected by SR `device-config:datapath=blkback|tapdisk`, or per-VDI/testing with a
URI `?dp=tapdisk`. Driven from python3 via `tap-ctl` (the `xapi.storage.libs.tapdisk`
lib ships only for python2 on 8.3).

## Data model & device-config

Namespace == SR uuid, image == VDI uuid, snapshot volume key == `<base>@<snap_uuid>`
(same as the SMAPIv1 driver). `device-config`: `pool`, `mon_host`, `user`, `key`
(cephx aes256k — always the datapath, and the control plane too when
`backend=local`), `ms_mode` (default: prefer-crc — crc where the cluster allows
it, else secure), `rbd_features` (preset `performance` (default) | `compat`, or
an explicit comma-list), `namespace` (default: SR uuid), `datapath`
(blkback|tapdisk), and the control-plane selector `backend` (`rest` (default) |
`local`) with its per-backend keys:

- **`backend=rest`** (default): `api_url`, `api_user`, `api_secret`,
  `api_tls_verify` — the ceph-mgr dashboard. No ceph userspace needed.
- **`backend=local`**: reuses `mon_host` / `user` / `key` (or point at an
  existing config with `ceph_conf=<path>`). Requires `python3-rbd` /
  `python3-rados` on the host.

## Build & install

Two noarch RPMs, built in the el7 container (matching the XCP-ng
`xapi-storage-{volume,datapath}-*` naming convention):

```sh
# regenerate the source tarball (volume/*.py + datapath/*.py under a
# dicode-xapi-storage-rbd-<ver>/ prefix), then:
podman run --rm -v "$PWD/_rpm":/work:z ghcr.io/xcp-ng/xcp-ng-build-env:8.3 \
  bash -lc 'rpmbuild --define "_topdir /work" -bb /work/SPECS/dicode-xapi-storage-rbd.spec'
# -> dicode-xapi-storage-volume-rbd-*.rpm  +  dicode-xapi-storage-datapath-rbd-*.rpm
```

```sh
rpm -ivh dicode-xapi-storage-volume-rbd-*.rpm dicode-xapi-storage-datapath-rbd-*.rpm
xe-toolstack-restart          # registers SR type 'rbd-vol'
xe sr-create type=rbd-vol name-label=... shared=true device-config:pool=rbd ...
```

Unlike SMAPIv1, no `sm-plugins` whitelist edit is needed — SMAPIv3 plugins are
discovered by directory.

## Status / not done

Done + validated on XCP-ng 8.3: SR + full VDI lifecycle, async GC, image-meta,
both datapath modes, and a full root-on-`rbd-vol` VM boot. Not done (optional):
CBT (`Volume.list_changed_blocks` via `rbd diff` or `tap-ctl -C`) and the `Data`
plugin (`copy`/`mirror`) for SXM. See `../rbdsr` for the SMAPIv1 lineage.
