#!/usr/bin/python3
#
# volume.py - Volume.* dispatcher for the rbd-vol SMAPIv3 volume plugin.
#
# Per-VDI RBD ops over the ceph-mgr dashboard REST backend. Native snap/clone/
# rollback; no libcow, no software COW. image == VDI uuid; snapshot volume key
# is "<base_image>@<snap_uuid>". The per-method executables are symlinks here.

import os
import sys
import uuid

import xapi.storage.api.v5.volume
from xapi.storage import log

import srmeta
import gcjob
import rbdvol_lib as lib
from rbd_backend import RbdBackendError

CLONE_SNAP_PREFIX = "xcp-clonebase-"


def _gen_uuid():
    return str(uuid.uuid4())


def _features(meta):
    f = meta["dconf"].get(
        "rbd_features",
        "layering,exclusive-lock,object-map,fast-diff,deep-flatten")
    return [x.strip() for x in f.split(",") if x.strip()]


def _flatten_children(be, pool, ns, base_image, snap_name=None):
    """Detach any CoW clones hanging off base_image's snapshot(s) before the snap
    can be removed. (Synchronous; the SMAPIv1 driver backgrounds this -- a later
    optimisation to port here.)"""
    try:
        info = be.image_info(pool, base_image, namespace=ns)
    except RbdBackendError:
        return
    for snap in info.get("snapshots", []) or []:
        if snap_name is not None and snap.get("name") != snap_name:
            continue
        for child in snap.get("children", []) or []:
            cimg = child.get("image_name")
            if cimg:
                be.flatten(child.get("pool_name", pool), cimg,
                           namespace=child.get("namespace", ns))


def _remove_snap(be, pool, ns, base_image, snap_name):
    _flatten_children(be, pool, ns, base_image, snap_name)
    try:
        be.snap_set_protected(pool, base_image, snap_name, False, namespace=ns)
    except RbdBackendError:
        pass
    be.snap_remove(pool, base_image, snap_name, namespace=ns)


def _snap_has_children(be, pool, ns, base_image, snap_name):
    try:
        info = be.image_info(pool, base_image, namespace=ns)
    except RbdBackendError:
        return False
    for s in info.get("snapshots", []) or []:
        if s.get("name") == snap_name:
            return bool(s.get("children"))
    return False


def _rmw(be, pool, ns, base, mutate):
    """Read-modify-write the base image's rbd image-meta dict."""
    info = be.image_info(pool, base, namespace=ns)
    m = dict(info.get("metadata") or {})
    mutate(m)
    be.image_meta_set(pool, base, m, int(info.get("size", 0)), namespace=ns)


class Implementation(xapi.storage.api.v5.volume.Volume_skeleton):

    def create(self, dbg, sr, name, description, size, sharable):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        vdi_uuid = _gen_uuid()
        try:
            be.create(pool, vdi_uuid, int(size), _features(meta), namespace=ns)
        except RbdBackendError as e:
            raise Exception("VDI create failed: %s" % e)
        # Persist name/description as image-meta so they survive a re-scan.
        md = {}
        nk, dk, _ = lib.meta_names(None)
        if name:
            md[nk] = name
        if description:
            md[dk] = description
        if md:
            try:
                be.image_meta_set(pool, vdi_uuid, md, int(size), namespace=ns)
            except RbdBackendError as e:
                log.debug("%s: Volume.create meta set failed (non-fatal): %s" % (dbg, e))
        log.debug("%s: Volume.create %s size=%s" % (dbg, vdi_uuid, size))
        return lib.volume_dict(meta["sr_uuid"], vdi_uuid, vdi_uuid, name,
                               description, int(size), physical_utilisation=0,
                               read_write=True, sharable=sharable)

    def destroy(self, dbg, sr, key):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        dconf = meta["dconf"]
        base, snap = lib.split_key(key)
        try:
            if snap:                       # a snapshot volume
                if _snap_has_children(be, pool, ns, base, snap):
                    # Clone(s) hang off it: rename out of the UUID space (SR.ls
                    # ignores it) and let the background GC flatten + purge.
                    trash = gcjob.TRASH_PREFIX + _gen_uuid()
                    be.snap_rename(pool, base, snap, trash, namespace=ns)
                    gcjob.spawn(dconf, pool, ns, "snap", base, snap=trash)
                else:
                    _remove_snap(be, pool, ns, base, snap)
            else:                          # a base image
                try:
                    info = be.image_info(pool, base, namespace=ns)
                except RbdBackendError as e:
                    info = None
                    if not e.not_found:
                        raise
                if info:
                    snaps = info.get("snapshots", []) or []
                    real = [s for s in snaps if lib.UUID_RE.match(s.get("name") or "")]
                    if real:
                        raise Exception("VDI has %d snapshot(s); delete those first"
                                        % len(real))
                    if any(s.get("children") for s in snaps):
                        # a clonebase snap still has a CoW child -> trash + async GC
                        trash = gcjob.TRASH_PREFIX + _gen_uuid()
                        be.image_rename(pool, base, trash, namespace=ns)
                        gcjob.spawn(dconf, pool, ns, "image", trash)
                    else:
                        for s in snaps:    # childless internal clonebase snaps
                            _remove_snap(be, pool, ns, base, s.get("name"))
                        be.remove(pool, base, namespace=ns)
                else:
                    be.remove(pool, base, namespace=ns)
        except RbdBackendError as e:
            if not e.not_found:
                raise Exception("VDI destroy failed: %s" % e)
        log.debug("%s: Volume.destroy %s" % (dbg, key))

    def snapshot(self, dbg, sr, key):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        base, existing = lib.split_key(key)
        if existing:
            raise Exception("cannot snapshot a snapshot")
        snap_uuid = _gen_uuid()
        try:
            be.snap_create(pool, base, snap_uuid, namespace=ns)
            be.snap_set_protected(pool, base, snap_uuid, True, namespace=ns)
        except RbdBackendError as e:
            try:
                be.snap_set_protected(pool, base, snap_uuid, False, namespace=ns)
                be.snap_remove(pool, base, snap_uuid, namespace=ns)
            except RbdBackendError:
                pass
            raise Exception("VDI snapshot failed: %s" % e)
        size = self._size_of(be, pool, ns, base)
        return lib.volume_dict(meta["sr_uuid"], lib.snap_key(base, snap_uuid),
                               snap_uuid, snap_uuid, "", size, read_write=False)

    def clone(self, dbg, sr, key):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        base, snap = lib.split_key(key)
        clone_uuid = _gen_uuid()
        feats = _features(meta)
        transient = None
        try:
            if snap is None:               # clone of a live image -> transient snap
                snap = CLONE_SNAP_PREFIX + _gen_uuid()
                transient = snap
                be.snap_create(pool, base, snap, namespace=ns)
                be.snap_set_protected(pool, base, snap, True, namespace=ns)
            be.clone(pool, base, snap, pool, clone_uuid, features=feats,
                     namespace=ns, dst_namespace=ns)
        except RbdBackendError as e:
            if transient:
                try:
                    be.snap_set_protected(pool, base, transient, False, namespace=ns)
                    be.snap_remove(pool, base, transient, namespace=ns)
                except RbdBackendError:
                    pass
            raise Exception("VDI clone failed: %s" % e)
        size = self._size_of(be, pool, ns, base)
        return lib.volume_dict(meta["sr_uuid"], clone_uuid, clone_uuid,
                               clone_uuid, "", size, read_write=True)

    def revert(self, dbg, sr, snapshot, key):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        base, snap = lib.split_key(snapshot)
        if snap is None:
            raise Exception("revert target is not a snapshot: %s" % snapshot)
        try:
            be.snap_rollback(pool, base, snap, namespace=ns)
        except RbdBackendError as e:
            raise Exception("VDI revert failed: %s" % e)
        log.debug("%s: Volume.revert %s -> %s" % (dbg, key, snapshot))
        return self.stat(dbg, sr, base)

    def resize(self, dbg, sr, key, new_size):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        base, snap = lib.split_key(key)
        if snap:
            raise Exception("cannot resize a snapshot")
        try:
            be.resize(pool, base, int(new_size), namespace=ns)
        except RbdBackendError as e:
            raise Exception("VDI resize failed: %s" % e)

    def stat(self, dbg, sr, key):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        base, snap = lib.split_key(key)
        try:
            info = be.image_info(pool, base, namespace=ns)
        except RbdBackendError as e:
            raise Exception("VDI stat failed: %s" % e)
        md = info.get("metadata")
        if snap:
            s = next((x for x in info.get("snapshots", []) or []
                      if x.get("name") == snap), None)
            size = int((s or {}).get("size", info.get("size", 0)) or 0)
            name, desc, keys = lib.meta_view(md, snap, snap)
            return lib.volume_dict(meta["sr_uuid"], key, snap, name, desc, size,
                                   read_write=False, keys=keys)
        name, desc, keys = lib.meta_view(md, None, base)
        return lib.volume_dict(meta["sr_uuid"], base, base, name, desc,
                               int(info.get("size", 0)),
                               physical_utilisation=int(info.get("disk_usage", 0) or 0),
                               read_write=True, keys=keys)

    # --- custom keys / labels, backed by rbd image-meta (see rbdvol_lib) ---
    def _open(self, sr, key):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        base, snap = lib.split_key(key)
        return be, pool, ns, base, snap

    def set(self, dbg, sr, key, k, v):
        be, pool, ns, base, snap = self._open(sr, key)
        _, _, kp = lib.meta_names(snap)
        _rmw(be, pool, ns, base, lambda m: m.__setitem__(kp + k, v))
        log.debug("%s: Volume.set %s %s=%s" % (dbg, key, k, v))

    def unset(self, dbg, sr, key, k):
        be, pool, ns, base, snap = self._open(sr, key)
        _, _, kp = lib.meta_names(snap)
        _rmw(be, pool, ns, base, lambda m: m.pop(kp + k, None))
        log.debug("%s: Volume.unset %s %s" % (dbg, key, k))

    def set_name(self, dbg, sr, key, new_name):
        be, pool, ns, base, snap = self._open(sr, key)
        nk, _, _ = lib.meta_names(snap)
        _rmw(be, pool, ns, base, lambda m: m.__setitem__(nk, new_name))
        log.debug("%s: Volume.set_name %s -> %s" % (dbg, key, new_name))

    def set_description(self, dbg, sr, key, new_description):
        be, pool, ns, base, snap = self._open(sr, key)
        _, dk, _ = lib.meta_names(snap)
        _rmw(be, pool, ns, base, lambda m: m.__setitem__(dk, new_description))
        log.debug("%s: Volume.set_description %s" % (dbg, key))

    # --- helper ---
    @staticmethod
    def _size_of(be, pool, ns, image):
        try:
            return int(be.image_info(pool, image, namespace=ns).get("size", 0))
        except RbdBackendError:
            return 0


if __name__ == "__main__":
    log.log_call_argv()
    cmd = xapi.storage.api.v5.volume.Volume_commandline(Implementation())
    base = os.path.basename(sys.argv[0])
    dispatch = {
        "Volume.create": cmd.create, "Volume.destroy": cmd.destroy,
        "Volume.snapshot": cmd.snapshot, "Volume.clone": cmd.clone,
        "Volume.revert": cmd.revert, "Volume.resize": cmd.resize,
        "Volume.stat": cmd.stat, "Volume.set": cmd.set, "Volume.unset": cmd.unset,
        "Volume.set_name": cmd.set_name, "Volume.set_description": cmd.set_description,
    }
    if base in dispatch:
        dispatch[base]()
    else:
        raise xapi.storage.api.v5.volume.Unimplemented(base)
