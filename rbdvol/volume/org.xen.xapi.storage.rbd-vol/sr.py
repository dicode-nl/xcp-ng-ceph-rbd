#!/usr/bin/python3
#
# sr.py - SR.* dispatcher for the rbd-vol SMAPIv3 volume plugin.
#
# SR lifecycle over the ceph-mgr dashboard REST backend. namespace == SR uuid.
# The per-method executables (SR.create, SR.attach, ...) are symlinks here.

import os
import sys

import xapi.storage.api.v5.volume
from xapi.storage import log

import srmeta
import gcjob
import rbdvol_lib as lib
from rbd_backend import RbdBackendError

REQUIRED = ("pool", "mon_host", "key", "api_url")


class Implementation(xapi.storage.api.v5.volume.SR_skeleton):

    def probe(self, dbg, configuration):
        # Report whether the pool/namespace is reachable; no SR list kept.
        result = {"srs": [], "uris": []}
        try:
            lib.make_backend(configuration).pool_stats(configuration["pool"])
        except Exception as e:
            log.debug("%s: SR.probe: pool unreachable: %s" % (dbg, e))
        return result

    def create(self, dbg, sr_uuid, configuration, name, description):
        log.debug("%s: SR.create %s" % (dbg, sr_uuid))
        for k in REQUIRED:
            if k not in configuration:
                raise Exception("device-config missing required key: %s" % k)
        be = lib.make_backend(configuration)
        pool = configuration["pool"]
        ns = configuration.get("namespace") or sr_uuid
        try:
            be.pool_stats(pool)          # pool reachable?
            be.namespace_create(pool, ns)
        except RbdBackendError as e:
            raise Exception("cannot init pool %s ns %s: %s" % (pool, ns, e))
        # Stamp identity into the config so SR.attach (which gets no sr_uuid) can
        # recover it; xapi persists what we return as the SR's device-config.
        configuration["sr_uuid"] = sr_uuid
        configuration["namespace"] = ns
        return configuration

    def attach(self, dbg, configuration):
        sr_uuid = configuration.get("sr_uuid")
        if not sr_uuid:
            raise Exception("device-config missing sr_uuid (SR not created by this driver?)")
        log.debug("%s: SR.attach %s" % (dbg, sr_uuid))
        try:
            lib.make_backend(configuration).pool_stats(configuration["pool"])
        except RbdBackendError as e:
            raise Exception("SR unavailable: %s" % e)
        meta = {"sr_uuid": sr_uuid,
                "name": configuration.get("name", ""),
                "description": configuration.get("description", ""),
                "dconf": dict(configuration)}
        return srmeta.write(sr_uuid, meta)

    def detach(self, dbg, sr):
        log.debug("%s: SR.detach %s" % (dbg, sr))
        srmeta.remove(sr)

    def destroy(self, dbg, sr):
        log.debug("%s: SR.destroy %s" % (dbg, sr))
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        if ns == meta["sr_uuid"]:      # only remove a namespace we own
            try:
                imgs = be.list_images(pool, namespace=ns)
                if imgs:
                    raise Exception("SR not empty: %d images" % len(imgs))
                be.namespace_remove(pool, ns)
            except RbdBackendError as e:
                log.debug("%s: SR.destroy: namespace cleanup skipped: %s" % (dbg, e))
        srmeta.remove(sr)

    def stat(self, dbg, sr):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, _ = lib.pool_ns(meta)
        try:
            s = be.pool_stats(pool)
        except RbdBackendError:
            s = {"total": 0, "free": 0}
        return {
            "sr": sr,
            "uuid": meta["sr_uuid"],
            "name": meta.get("name", ""),
            "description": meta.get("description", ""),
            "total_space": int(s.get("total", 0)),
            "free_space": int(s.get("free", 0)),
            "datasources": [],
            "clustered": True,        # RBD is shared/cluster storage
            "health": ["Healthy", ""],
        }

    def ls(self, dbg, sr):
        meta = srmeta.read(sr)
        be = lib.backend(meta)
        pool, ns = lib.pool_ns(meta)
        sr_uuid = meta["sr_uuid"]
        dconf = meta["dconf"]
        gc_targets = gcjob.sweep()     # retry dead GC jobs; learn queued targets
        out = []
        try:
            images = be.list_images(pool, namespace=ns)
        except RbdBackendError as e:
            raise Exception("SR.ls failed: %s" % e)
        for info in images:
            name = info.get("name")
            # A trashed image mid-GC: never a VDI. Backstop: re-queue if no job.
            if name and name.startswith(gcjob.TRASH_PREFIX):
                if ("image", name, None) not in gc_targets:
                    gcjob.spawn(dconf, pool, ns, "image", name)
                    gc_targets.add(("image", name, None))
                continue
            if not name or not lib.UUID_RE.match(name):
                continue          # skip non-VDI images (clonebase etc.)
            size = int(info.get("size", 0))
            used = int(info.get("disk_usage", 0) or 0)
            md = info.get("metadata")
            cbt = lib.cbt_is_on(md)  # base + its snapshots share the CBT flag
            bname, bdesc, bkeys = lib.meta_view(md, None, name)
            out.append(lib.volume_dict(sr_uuid, name, name, bname, bdesc, size,
                                       physical_utilisation=used, read_write=True,
                                       keys=bkeys, cbt_enabled=cbt))
            for snap in info.get("snapshots", []) or []:
                sname = snap.get("name", "")
                if sname.startswith(gcjob.TRASH_PREFIX):   # trashed snap mid-GC
                    if ("snap", name, sname) not in gc_targets:
                        gcjob.spawn(dconf, pool, ns, "snap", name, snap=sname)
                        gc_targets.add(("snap", name, sname))
                    continue
                if not lib.UUID_RE.match(sname):
                    continue      # skip internal clonebase snaps
                key = lib.snap_key(name, sname)
                ssize = int(snap.get("size", size) or size)
                sn, sd, sk = lib.meta_view(md, sname, sname)
                out.append(lib.volume_dict(sr_uuid, key, sname, sn, sd, ssize,
                                           read_write=False, keys=sk, cbt_enabled=cbt))
        return out

    def set_name(self, dbg, sr, new_name):
        meta = srmeta.read(sr)
        meta["name"] = new_name
        srmeta.write(meta["sr_uuid"], meta)

    def set_description(self, dbg, sr, new_description):
        meta = srmeta.read(sr)
        meta["description"] = new_description
        srmeta.write(meta["sr_uuid"], meta)


if __name__ == "__main__":
    log.log_call_argv()
    cmd = xapi.storage.api.v5.volume.SR_commandline(Implementation())
    base = os.path.basename(sys.argv[0])
    dispatch = {
        "SR.probe": cmd.probe, "SR.create": cmd.create, "SR.attach": cmd.attach,
        "SR.detach": cmd.detach, "SR.destroy": cmd.destroy, "SR.stat": cmd.stat,
        "SR.ls": cmd.ls, "SR.set_name": cmd.set_name,
        "SR.set_description": cmd.set_description,
    }
    if base in dispatch:
        dispatch[base]()
    else:
        raise xapi.storage.api.v5.volume.Unimplemented(base)
