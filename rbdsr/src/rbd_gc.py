#!/usr/bin/python3
#
# rbd_gc.py - detached background garbage-collector for the Ceph RBD SR.
#
# VDI.delete cannot remove an RBD snapshot/image while a CoW clone still depends
# on it: the child must be *flattened* first, and flattening copies every shared
# object (potentially many GB, minutes of wall-clock). To keep delete() snappy we
# do NOT flatten inline. Instead delete() renames the doomed object to a non-UUID
# "xcp-trash-<uuid>" name (so SR.scan ignores it), db_forgets the VDI, and spawns
# this worker (setsid-detached) with a job file. Here we flatten the children and
# then purge the trashed object. If we die, the trash object simply lingers
# (scan-invisible, harmless); SR.scan re-spawns us for any stale job file.
#
# Invocation:  rbd_gc.py <job.json>
# Job schema:
#   {"dconf": {...SR device-config...}, "pool": "...", "namespace": "...",
#    "kind": "snap", "image": "<base>", "snap": "<xcp-trash-...>"}      # snap purge
#   {"dconf": {...}, "pool": "...", "namespace": "...",
#    "kind": "image", "image": "<xcp-trash-...>"}                        # image purge
#
# Stdlib only, python 3.6+.

import json
import os
import sys
import time

try:
    import util
    def _log(msg):
        util.SMlog("[rbd_gc] " + msg)
except Exception:  # pragma: no cover
    def _log(msg):
        sys.stderr.write("[rbd_gc] " + msg + "\n")

from rbd_backend import make_backend, RbdBackendError

FLATTEN_RETRIES = 4
RETRY_SLEEP = 10


def _flatten_children_of(be, pool, ns, image, snap_filter=None):
    """Flatten every CoW child hanging off image's snapshots (optionally only the
    snap named snap_filter). Retries transient failures; a persistently failing
    child is logged and skipped (the trash object then survives, scan-invisible)."""
    try:
        info = be.image_info(pool, image, namespace=ns)
    except RbdBackendError as e:
        if e.not_found:
            return True
        raise
    ok = True
    for snap in info.get('snapshots', []) or []:
        if snap_filter is not None and snap.get('name') != snap_filter:
            continue
        for child in snap.get('children', []) or []:
            cimage = child.get('image_name')
            if not cimage:
                continue
            cpool = child.get('pool_name', pool)
            cns = child.get('namespace', ns)
            done = False
            for attempt in range(FLATTEN_RETRIES):
                try:
                    be.flatten(cpool, cimage, namespace=cns)
                    _log("flattened child %s (parent %s@%s)" % (cimage, image, snap.get('name')))
                    done = True
                    break
                except RbdBackendError as e:
                    if e.not_found:  # child already gone
                        done = True
                        break
                    _log("flatten %s failed (try %d/%d): %s"
                         % (cimage, attempt + 1, FLATTEN_RETRIES, e))
                    time.sleep(RETRY_SLEEP)
            ok = ok and done
    return ok


def _purge_snap(be, pool, ns, image, snap):
    try:
        be.snap_set_protected(pool, image, snap, False, namespace=ns)
    except RbdBackendError:
        pass
    try:
        be.snap_remove(pool, image, snap, namespace=ns)
    except RbdBackendError as e:
        if not e.not_found:
            raise


def run(job):
    be = make_backend(job['dconf'])
    pool = job['pool']
    ns = job.get('namespace') or ""
    kind = job['kind']
    image = job['image']

    if kind == 'snap':
        snap = job['snap']
        if not _flatten_children_of(be, pool, ns, image, snap_filter=snap):
            raise RbdBackendError("not all children of %s@%s flattened" % (image, snap))
        _purge_snap(be, pool, ns, image, snap)
        _log("purged snap %s@%s" % (image, snap))
    elif kind == 'image':
        if not _flatten_children_of(be, pool, ns, image):
            raise RbdBackendError("not all children of %s flattened" % image)
        try:
            info = be.image_info(pool, image, namespace=ns)
        except RbdBackendError as e:
            info = None
            if not e.not_found:
                raise
        for snap in (info.get('snapshots', []) if info else []) or []:
            _purge_snap(be, pool, ns, image, snap.get('name'))
        try:
            be.remove(pool, image, namespace=ns)
        except RbdBackendError as e:
            if not e.not_found:
                raise
        _log("purged image %s" % image)
    else:
        raise ValueError("unknown GC job kind %r" % kind)


def main():
    if len(sys.argv) != 2:
        _log("usage: rbd_gc.py <job.json>")
        return 2
    path = sys.argv[1]
    try:
        with open(path) as f:
            job = json.load(f)
    except (IOError, OSError, ValueError) as e:
        _log("cannot read job %s: %s" % (path, e))
        return 1
    try:
        run(job)
    except Exception as e:  # keep the job file so scan() can retry us later
        _log("GC job %s (%s %s) failed, leaving for retry: %s"
             % (path, job.get('kind'), job.get('image'), e))
        return 1
    try:
        os.unlink(path)
    except OSError:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
