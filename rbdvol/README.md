# rbdvol — Ceph RBD Storage Repository for XCP-ng (SMAPIv3)

A **SMAPIv3** storage plugin presenting each VDI as a native Ceph RADOS Block
Device — the modern, forward-looking sibling of the SMAPIv1 driver in
[`../rbdsr`](../rbdsr). Same data model and Ceph datapath (aes256k / msgr2-secure
krbd), reworked onto the xapi-storage volume/datapath plugin split. **python3,
standalone (no libcow).** SR type: **`rbd-vol`**.

Full VDI lifecycle (create / snapshot / clone / native revert / resize, async
flatten-on-delete GC, VDI metadata, boots a VM root-off-it), **CBT**, and **live
storage migration (SXM)** — intra-host *and* cross-host, validated md5-identical.

## Two components

SMAPIv3 splits storage into a **volume plugin** (manages the SR + volumes) and a
**datapath** (turns a volume into a block device for a domain). Both import a
shared **`dicode.libs`** python package (installed to dom0 site-packages), so the
driver ships as **three** RPMs: `dicode-xapi-storage-libs-rbd` (the shared libs)
plus the thin `-volume-rbd` and `-datapath-rbd` that Require it.

### `volume/org.xen.xapi.storage.rbd-vol/` — the volume plugin
**Pluggable control plane** (`device-config:backend=`): the ceph-mgr **dashboard
REST API** (`rest`, the default — needs no ceph userspace on dom0) or a **local
`librbd` backend** (`local` — uses the `python3-rbd` / `python3-rados` bindings,
for hosts that *do* have a working ceph userspace; no dashboard required). The
data path (krbd map) is unchanged either way.

- `plugin.py` — `Plugin.Query` / `diagnostics`; advertises `VDI_CREATE/DESTROY/
  RESIZE/SNAPSHOT/CLONE`, **`VDI_CONFIG_CBT`**, **`VDI_MIRROR`**.
- `sr.py` — `SR.*` (create/attach/detach/destroy/stat/ls/probe/set_name/
  set_description). `SR.stat` reports **per-SR** usage (this SR's namespace
  allocation), not the shared pool-wide figure.
- `volume.py` — `Volume.*` (create/destroy/snapshot/clone/revert/resize/stat/set/
  unset/set_name/set_description + `enable_cbt`/`disable_cbt`/`list_changed_blocks`
  /`data_destroy`).

### `datapath/rbd/` — the datapath (URI scheme `rbd`)
- `plugin.py` — `Plugin.Query` / `diagnostics`; advertises **`VDI_MIRROR_IN`**.
- `datapath.py` — `Datapath.*` + `DATA.*` (get_nbd_server/mirror/stat). Maps the
  image via krbd, then dispatches to the serve-mode helper in `dicode.libs`.

### `libs/dicode/libs/` — the shared package (`import dicode.libs.…`)
- `srmeta.py` — per-host SR metadata store (stashes device-config so per-volume
  calls and the datapath recover pool/namespace/cephx key; secrets out of URIs).
- `rbd_backend.py` — control-plane backends: `RestBackend` (dashboard REST) and
  `LocalBackend` (librbd/librados); `make_backend()` selects on `backend=`.
- `rbd_sysfs.py` — the krbd map/unmap datapath.
- `dp_blkback.py` / `dp_tapdisk.py` / `dp_qemu.py` — the three serve modes.
- `qsd.py` — qemu-storage-daemon control (qemu mode); `blknbd.py` — qemu-nbd over
  /dev/rbdN (blkback SXM-receive).
- `cbtlog.py` — tapdisk CBT log; `gcjob.py` + `rbd_gc.py` — async flatten GC;
  `rbdvol_lib.py` — shared helpers.

The per-method executables (`SR.create`, `Volume.snapshot`, `DATA.mirror`, …) are
symlinks to the dispatcher modules; they switch on `argv[0]`.

## Three serving modes

A *parameter* (`device-config:datapath=`, or per-VDI `?dp=`), not separate
plugins — the underlying access is always native krbd (`/dev/rbdN`):

- **blkback** (default) → raw `/dev/rbdN` via kernel blkback (`backend_type vbd`),
  tapdisk-less, max performance. Can be an SXM **destination** (a qemu-nbd over
  /dev/rbdN, started only during a receive); **not** an SXM source.
- **tapdisk** → `tap-ctl create aio:/dev/rbdN` (`backend_type vbd3`) — the
  "accepted" xcp-ng path; unlocks CBT via `cbtlog`.
- **qemu** → a per-VDI **qemu-storage-daemon** exports /dev/rbdN over NBD, wired to
  /dev/nbdX and served by blkback. Its `blockdev-mirror` does a race-free full copy
  + live tee — the SXM **source** engine (`DATA.mirror`), so a qemu-mode disk can be
  live-migrated (intra- or cross-host) onto any rbd-vol SR.

## Data model & device-config

Namespace == SR uuid, image == VDI uuid, snapshot volume key == `<base>@<snap_uuid>`
(same as the SMAPIv1 driver). `device-config`: `pool`, `mon_host`, `user`, `key`
(cephx aes256k — always the datapath, and the control plane too when
`backend=local`), `ms_mode` (default: prefer-crc), `rbd_features` (preset
`performance` (default) | `compat`, or an explicit comma-list), `namespace`
(default: SR uuid), `datapath` (`blkback` (default) | `tapdisk` | `qemu`), and the
control-plane selector `backend` (`rest` (default) | `local`):

- **`backend=rest`** (default): `api_url`, `api_user`, `api_secret`,
  `api_tls_verify` — the ceph-mgr dashboard. No ceph userspace needed.
- **`backend=local`**: reuses `mon_host` / `user` / `key` (or `ceph_conf=<path>`).
  Requires `python3-rbd` / `python3-rados` on the host.

Optional runtime deps are **not** hard RPM requirements (el7 rpm has no weak deps);
the code fails with a clear error if a mode is used without its tool: `qemu-dp` +
`nbd` for the qemu mode / SXM, `python3-rados` + `python3-rbd` for `backend=local`.

## Build & install

Three noarch RPMs, built in the el7 container. Tar `volume/`, `datapath/`, `libs/`
under a `dicode-xapi-storage-rbd-<ver>/` prefix as `SOURCES/`, then:

```sh
podman run --rm --user root -v "$PWD/_rpm":/root/rpmbuild:z \
  ghcr.io/xcp-ng/xcp-ng-build-env:8.3 \
  bash -lc 'rpmbuild -ba /root/rpmbuild/SPECS/dicode-xapi-storage-rbd.spec'
# -> dicode-xapi-storage-{libs,volume,datapath}-rbd-*.rpm
```

```sh
rpm -ivh dicode-xapi-storage-libs-rbd-*.rpm \
         dicode-xapi-storage-volume-rbd-*.rpm \
         dicode-xapi-storage-datapath-rbd-*.rpm
xe-toolstack-restart          # registers SR type 'rbd-vol' (on every pool host;
                              # VDI_MIRROR/VDI_MIRROR_IN promote once all hosts
                              # have re-registered)
xe sr-create type=rbd-vol name-label=... shared=true device-config:pool=rbd ...
```

Unlike SMAPIv1, no `sm-plugins` whitelist edit is needed — SMAPIv3 plugins are
discovered by directory.

## Status

Done + validated on XCP-ng 8.3: SR + full VDI lifecycle, async GC, image-meta, all
three datapath modes, a full root-on-`rbd-vol` VM boot, CBT, and live storage
migration (SXM) — intra-host and **cross-host** (VM node->node while a qemu-mode
disk moves to another rbd-vol SR), byte-for-byte verified. `xe vdi-copy`/offline
move onto a qemu-mode SR works too. See `../rbdsr` for the SMAPIv1 lineage.
