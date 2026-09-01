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

from dicode.libs import srmeta
from dicode.libs import gcjob
from dicode.libs import cbtlog
from dicode.libs import rbdvol_lib as lib
from dicode.libs.rbd_backend import RbdBackendError

CLONE_SNAP_PREFIX = "xcp-clonebase-"


def _gen_uuid():
    return str(uuid.uuid4())


def _features(meta):
    return lib.resolve_features(meta["dconf"].get("rbd_features"))


def _remove_snap(be, pool, ns, base_image, snap_name):
    # Only ever called for a snapshot with NO children -- destroy() hands a snap
    # that still has CoW clones to the background GC (trash-rename + rbd_gc.py),
    # which flattens the children first. So here we just unprotect + remove.
    try:
        be.snap_set_protected(pool, base_image, snap_name, False, namespace=ns)
    except RbdBackendError:
        pass
    be.snap_remove(pool, base_image, snap_name, namespace=ns)


def _force_release(dbg, pool, ns, image):
    """Best-effort LOCAL release before deleting a base image. A failed/aborted SXM
    leaves the receive-side qemu-nbd (blknbd) -- and the krbd map it sits on --
    still holding the dest leaf, so the control-plane remove hits 'RBD image is
    busy [errno 16]' and the image + qemu-nbd + map leak. Stop any per-image NBD
    server WE run and unmap krbd here first, dropping the RBD watchers so the
    delete succeeds. A no-op for a normally-detached VDI (nothing running, not
    mapped); only releases holders on THIS host."""
    from dicode.libs import blknbd, qsd, rbd_sysfs
    for stop in (blknbd.stop, qsd.stop):
        try:
            stop(dbg, image)
        except Exception as e:
            log.debug("%s: force_release stop %s: %s" % (dbg, image, e))
    try:
        if rbd_sysfs.find_device(pool, image, namespace=ns):
            rbd_sysfs.unmap_image(pool, image, force=True, namespace=ns)
    except Exception as e:
        log.debug("%s: force_release unmap %s: %s" % (dbg, image, e))


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


def _purge_snap_meta(be, pool, ns, base, snap):
    """Drop a destroyed snapshot's leftover 'snap.<uuid>.*' keys (name/desc/
    custom keys/cbt.*) from the base image meta. Best-effort: the snapshot's
    rbd object is already gone, so a failure here only leaves cosmetic keys."""
    prefix = lib.snap_meta_prefix(snap)

    def _drop(m):
        for k in [k for k in m if k.startswith(prefix)]:
            m.pop(k, None)

    try:
        _rmw(be, pool, ns, base, _drop)
    except RbdBackendError as e:
        log.debug("_purge_snap_meta %s@%s: %s" % (base, snap, e))


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
                # Tidy the snapshot's image-meta (keyed by its original uuid;
                # the GC only sees the trash name, so clean it here).
                _purge_snap_meta(be, pool, ns, base, snap)
                # tapdisk CBT: drop this snapshot's sealed cbtlog companion.
                try:
                    bmd = be.image_info(pool, base, namespace=ns).get("metadata") or {}
                    sealed = cbtlog.sealed_gen(bmd, snap)
                    if sealed:
                        cbtlog.remove_companion(be, dconf, pool, ns, base, sealed)
                        _rmw(be, pool, ns, base,
                             lambda m: m.pop(cbtlog.snap_key(snap), None))
                except RbdBackendError as e:
                    log.debug("%s: destroy: cbtlog companion cleanup: %s" % (dbg, e))
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
                    # base is going away: drop its cbtlog companions (live + any left).
                    md = info.get("metadata") or {}
                    if cbtlog.is_tapdisk(md):
                        cbtlog.remove_all_companions(be, dconf, pool, ns, base, md)
                    if any(s.get("children") for s in snaps):
                        # a clonebase snap still has a CoW child -> trash + async GC
                        trash = gcjob.TRASH_PREFIX + _gen_uuid()
                        be.image_rename(pool, base, trash, namespace=ns)
                        gcjob.spawn(dconf, pool, ns, "image", trash)
                    else:
                        for s in snaps:    # childless internal clonebase snaps
                            _remove_snap(be, pool, ns, base, s.get("name"))
                        _force_release(dbg, pool, ns, base)
                        be.remove(pool, base, namespace=ns)
                else:
                    _force_release(dbg, pool, ns, base)
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
        try:
            binfo = be.image_info(pool, base, namespace=ns)
        except RbdBackendError:
            binfo = {}
        size = int(binfo.get("size", 0) or 0)
        bmd = binfo.get("metadata") or {}
        cbt = lib.cbt_is_on(bmd)  # inherit base's CBT flag
        # Persist the snapshot's name+description (inherited from the base at
        # snapshot time, as xapi does) so a later re-scan keeps them instead of
        # renaming the VDI to its bare uuid. A later Volume.set_name overrides it.
        bnk, bdk, _ = lib.meta_names(None)          # vdi.name / vdi.desc
        snk, sdk, _ = lib.meta_names(snap_uuid)     # snap.<uuid>.name / .desc
        base_name = bmd.get(bnk)
        base_desc = bmd.get(bdk)
        if base_name or base_desc:
            def _seed(m):
                if base_name:
                    m[snk] = base_name
                if base_desc:
                    m[sdk] = base_desc
            try:
                _rmw(be, pool, ns, base, _seed)
            except RbdBackendError:
                pass
        # tapdisk CBT: seal the current live cbtlog to this snapshot, start a
        # fresh one (live-swaps the running tapdisk's -C via pause/unpause).
        if cbtlog.is_tapdisk(bmd):
            try:
                updates = cbtlog.rotate(be, meta["dconf"], pool, ns, base,
                                        snap_uuid, bmd, size)
                _rmw(be, pool, ns, base, lambda m: m.update(updates))
            except Exception as e:
                log.debug("%s: snapshot cbtlog rotate failed: %s" % (dbg, e))
        return lib.volume_dict(meta["sr_uuid"], lib.snap_key(base, snap_uuid),
                               snap_uuid, base_name or snap_uuid, base_desc or "",
                               size, read_write=False, cbt_enabled=cbt)

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
        cbt = lib.cbt_is_on(md)  # snapshots inherit the base image's CBT flag
        if snap:
            s = next((x for x in info.get("snapshots", []) or []
                      if x.get("name") == snap), None)
            size = int((s or {}).get("size", info.get("size", 0)) or 0)
            name, desc, keys = lib.meta_view(md, snap, snap)
            return lib.volume_dict(meta["sr_uuid"], key, snap, name, desc, size,
                                   read_write=False, keys=keys, cbt_enabled=cbt)
        name, desc, keys = lib.meta_view(md, None, base)
        return lib.volume_dict(meta["sr_uuid"], base, base, name, desc,
                               int(info.get("size", 0)),
                               physical_utilisation=int(info.get("disk_usage", 0) or 0),
                               read_write=True, keys=keys, cbt_enabled=cbt)

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

    # --- Changed Block Tracking ---
    # Two routes, chosen by the SR's datapath: (a) rbd-diff (needs fast-diff/
    # object-map + the ceph-mgr /diff endpoint), datapath-agnostic; (b) tapdisk
    # cbtlog (needs datapath=tapdisk, works on any ceph/compat image, no patch).
    @staticmethod
    def _cbt_key(snap):
        return "cbt.enabled" if snap is None else "snap.%s.cbt.enabled" % snap

    @staticmethod
    def _is_tapdisk_sr(dconf):
        return str(dconf.get("datapath", "")).lower() == "tapdisk"

    def enable_cbt(self, dbg, sr, key):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        dconf = meta["dconf"]
        base, snap = lib.split_key(key)
        if snap is None and self._is_tapdisk_sr(dconf):
            info = be.image_info(pool, base, namespace=ns)
            md = info.get("metadata") or {}
            if cbtlog.is_tapdisk(md):
                return                                  # already on
            size = int(info.get("size", 0) or 0)
            gen = cbtlog.create_companion(be, dconf, pool, ns, base, size)

            def _mark(m):
                m[cbtlog.K_ENABLED] = "1"
                m[cbtlog.K_MODE] = cbtlog.MODE_TAPDISK
                m[cbtlog.K_LIVE] = gen
            _rmw(be, pool, ns, base, _mark)
            try:                                        # if a VM is running, wire -C live
                cbtlog.inject_running(dconf, pool, ns, base, gen)
            except Exception as e:
                log.debug("%s: enable_cbt inject skipped: %s" % (dbg, e))
            log.debug("%s: Volume.enable_cbt %s (tapdisk cbtlog)" % (dbg, key))
            return
        # rbd-diff route
        feats = (be.image_info(pool, base, namespace=ns).get("features_name")
                 or [])
        if "fast-diff" not in feats:
            raise Exception("CBT requires fast-diff/object-map on the image "
                            "(rbd_features 'performance'), or a datapath=tapdisk "
                            "SR for the cbtlog route; this image is 'compat'")
        mk = self._cbt_key(snap)
        _rmw(be, pool, ns, base, lambda m: m.__setitem__(mk, "1"))
        log.debug("%s: Volume.enable_cbt %s (rbd-diff)" % (dbg, key))

    def disable_cbt(self, dbg, sr, key):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        dconf = meta["dconf"]
        base, snap = lib.split_key(key)
        if snap is None:
            md = be.image_info(pool, base, namespace=ns).get("metadata") or {}
            if cbtlog.is_tapdisk(md):
                cbtlog.remove_all_companions(be, dconf, pool, ns, base, md)

                def _clear(m):
                    for k in list(m):
                        if (k in (cbtlog.K_ENABLED, cbtlog.K_MODE, cbtlog.K_LIVE)
                                or k.startswith("cbt.snap.")):
                            m.pop(k, None)
                _rmw(be, pool, ns, base, _clear)
                log.debug("%s: Volume.disable_cbt %s (tapdisk cbtlog)" % (dbg, key))
                return
        mk = self._cbt_key(snap)
        _rmw(be, pool, ns, base, lambda m: m.pop(mk, None))
        log.debug("%s: Volume.disable_cbt %s" % (dbg, key))

    def data_destroy(self, dbg, sr, key):
        # "delete the snapshot's data but keep its CBT metadata". For RBD the
        # snapshot IS the anchor the NEXT diff is taken from, so we KEEP the rbd
        # snapshot and only record that xapi considers the data gone.
        be, pool, ns, base, snap = self._open(sr, key)
        if snap is None:
            raise Exception("data_destroy target is not a snapshot: %s" % key)
        mk = "snap.%s.cbt.data_destroyed" % snap
        _rmw(be, pool, ns, base, lambda m: m.__setitem__(mk, "1"))
        log.debug("%s: Volume.data_destroy %s (rbd snap kept as CBT anchor)"
                  % (dbg, key))

    def list_changed_blocks(self, dbg, sr, key, key2, offset, length):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        base, from_snap = lib.split_key(key)     # xapi: vdi_from (earlier)
        base2, to_snap = lib.split_key(key2)     # xapi: vdi_to   (later)
        if base != base2:
            raise Exception("list_changed_blocks across different images: %s vs %s"
                            % (base, base2))
        info = be.image_info(pool, base, namespace=ns)
        size = int(info.get("size", 0) or 0)
        off = int(offset or 0)
        # xapi passes length=-1 to mean "to the end of the image".
        length = -1 if length is None else int(length)
        ln = (size - off) if length < 0 else length
        ln = max(0, min(ln, size - off))
        md = info.get("metadata") or {}
        if cbtlog.is_tapdisk(md):
            bitmap = cbtlog.changed_bitmap(meta["dconf"], pool, ns, base, md,
                                           from_snap, to_snap, off, ln, size)
            log.debug("%s: Volume.list_changed_blocks %s..%s (tapdisk cbtlog)"
                      % (dbg, key, key2))
            return {"granularity": cbtlog.CBT_BLOCK, "bitmap": bitmap}
        whole = str(meta["dconf"].get("cbt_whole_object", "true")).lower() \
            in ("1", "true", "yes")
        try:
            diffs = be.image_diff(pool, base, from_snap, to_snap, offset=off,
                                  length=ln, whole_object=whole, namespace=ns)
        except RbdBackendError as e:
            if e.not_supported:
                raise xapi.storage.api.v5.volume.Unimplemented(
                    "Volume.list_changed_blocks: %s" % e)
            raise Exception("list_changed_blocks failed: %s" % e)
        log.debug("%s: Volume.list_changed_blocks %s..%s off=%s len=%s -> %d extents"
                  % (dbg, key, key2, off, ln, len(diffs)))
        return {"granularity": lib.CBT_BLOCK,
                "bitmap": lib.changed_bitmap(diffs, off, ln)}

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
        "Volume.enable_cbt": cmd.enable_cbt, "Volume.disable_cbt": cmd.disable_cbt,
        "Volume.data_destroy": cmd.data_destroy,
        "Volume.list_changed_blocks": cmd.list_changed_blocks,
    }
    if base in dispatch:
        dispatch[base]()
    else:
        raise xapi.storage.api.v5.volume.Unimplemented(base)
