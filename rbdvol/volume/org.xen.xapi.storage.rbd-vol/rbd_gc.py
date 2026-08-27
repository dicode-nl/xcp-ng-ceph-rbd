#!/usr/bin/python3
#
# rbd_gc.py - detached background garbage-collector for the rbd-vol SMAPIv3 plugin.
#
# Volume.destroy renames an object with CoW children to "xcp-trash-<uuid>" (SR.ls
# ignores it) and spawns this worker, which flattens the children then purges the
# trashed object. If we die/defer, the trash object lingers (scan-invisible); SR.ls
# re-spawns us for stale jobs.
#
# Flatten policy: only flatten a child when that will actually FREE the parent
# snapshot -- i.e. the child has the `deep-flatten` feature (so its own snapshots
# get detached too) OR the child has no snapshots of its own. Otherwise a snapshot
# of the child still references the parent snap (no deep-flatten), so flattening
# now would merely duplicate data without freeing the parent: DEFER and retry once
# the child's snapshots are gone. Already-flat children are skipped (idempotent).
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


class Deferred(Exception):
    """Not an error: a child still pins the parent (no deep-flatten + it has its
    own snapshots); leave the job for a later retry once those snapshots go."""


def _flatten_one(be, cpool, cimage, cns, parent_image, snapname):
    for attempt in range(FLATTEN_RETRIES):
        try:
            be.flatten(cpool, cimage, namespace=cns)
            _log("flattened child %s (parent %s@%s)" % (cimage, parent_image, snapname))
            return True
        except RbdBackendError as e:
            if e.not_found:
                return True
            _log("flatten %s failed (try %d/%d): %s"
                 % (cimage, attempt + 1, FLATTEN_RETRIES, e))
            time.sleep(RETRY_SLEEP)
    return False


def _resolve_children(be, pool, ns, image, snap_filter=None):
    """Try to detach every CoW child of image's snapshot(s). Returns
    (all_resolved, deferred, failed)."""
    try:
        info = be.image_info(pool, image, namespace=ns)
    except RbdBackendError as e:
        if e.not_found:
            return True, 0, 0
        raise
    all_resolved, deferred, failed = True, 0, 0
    for snap in info.get("snapshots", []) or []:
        if snap_filter is not None and snap.get("name") != snap_filter:
            continue
        sname = snap.get("name")
        for child in snap.get("children", []) or []:
            cimage = child.get("image_name")
            if not cimage:
                continue
            cpool = child.get("pool_name", pool)
            cns = child.get("namespace", ns)
            try:
                cinfo = be.image_info(cpool, cimage, namespace=cns)
            except RbdBackendError as e:
                if e.not_found:
                    continue                 # child gone -> nothing pins us
                all_resolved = False
                failed += 1
                continue
            if not cinfo.get("parent"):
                continue                     # already flat (idempotent)
            feats = cinfo.get("features_name") or []
            has_snaps = bool(cinfo.get("snapshots"))
            if ("deep-flatten" in feats) or (not has_snaps):
                if not _flatten_one(be, cpool, cimage, cns, image, sname):
                    all_resolved = False
                    failed += 1
            else:
                # A snapshot of the child still references parent@snap and there is
                # no deep-flatten -> flattening now can't free the parent (and would
                # duplicate data). Wait for the child's snapshots to be removed.
                deferred += 1
                all_resolved = False
                _log("deferring %s: it has snapshots pinning %s@%s (no deep-flatten)"
                     % (cimage, image, sname))
    return all_resolved, deferred, failed


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
    be = make_backend(job["dconf"])
    pool = job["pool"]
    ns = job.get("namespace") or ""
    kind = job["kind"]
    image = job["image"]

    if kind == "snap":
        snap = job["snap"]
        ok, deferred, failed = _resolve_children(be, pool, ns, image, snap_filter=snap)
        if failed:
            raise RbdBackendError("%d child flatten(s) failed for %s@%s" % (failed, image, snap))
        if not ok:
            raise Deferred("%d child(ren) still pin %s@%s" % (deferred, image, snap))
        _purge_snap(be, pool, ns, image, snap)
        _log("purged snap %s@%s" % (image, snap))
    elif kind == "image":
        ok, deferred, failed = _resolve_children(be, pool, ns, image)
        if failed:
            raise RbdBackendError("%d child flatten(s) failed for %s" % (failed, image))
        if not ok:
            raise Deferred("%d child(ren) still pin %s" % (deferred, image))
        try:
            info = be.image_info(pool, image, namespace=ns)
        except RbdBackendError as e:
            info = None
            if not e.not_found:
                raise
        for snap in (info.get("snapshots", []) if info else []) or []:
            _purge_snap(be, pool, ns, image, snap.get("name"))
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
    except Deferred as e:
        _log("GC job %s deferred (will retry): %s" % (path, e))
        return 1
    except Exception as e:
        _log("GC job %s (%s %s) failed, leaving for retry: %s"
             % (path, job.get("kind"), job.get("image"), e))
        return 1
    try:
        os.unlink(path)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
