#!/usr/bin/python3
#
# rbd_gc.py - detached background garbage-collector for the rbd-vol SMAPIv3 plugin.
#
# Volume.destroy cannot remove an RBD snapshot/image while a CoW clone still
# depends on it: the child must be *flattened* first (copies every shared object,
# potentially many GB). To keep destroy snappy we do NOT flatten inline. Instead
# destroy renames the doomed object to a non-UUID "xcp-trash-<uuid>" name (so
# SR.ls ignores it) and spawns this worker (setsid-detached) with a job file.
# Here we flatten the children then purge the trashed object. If we die, the trash
# object lingers (scan-invisible, harmless); SR.ls re-spawns us for any stale job.
#
# Invocation:  rbd_gc.py <job.json>
#   {"dconf":{...}, "pool":..., "namespace":..., "kind":"snap", "image":<base>, "snap":<xcp-trash-...>}
#   {"dconf":{...}, "pool":..., "namespace":..., "kind":"image", "image":<xcp-trash-...>}
#
# Stdlib only, python3.

import json
import os
import sys
import time

try:
    from xapi.storage import log
    def _log(msg):
        log.debug("[rbd-vol gc] " + msg)
except Exception:  # pragma: no cover
    def _log(msg):
        sys.stderr.write("[rbd-vol gc] " + msg + "\n")

from rbd_backend import make_backend, RbdBackendError

FLATTEN_RETRIES = 4
RETRY_SLEEP = 10


def _flatten_children_of(be, pool, ns, image, snap_filter=None):
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
                    if e.not_found:
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
    except Exception as e:
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
