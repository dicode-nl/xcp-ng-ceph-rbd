"""
gc.py - async flatten-on-delete helper for the rbd-vol volume plugin.

Volume.destroy renames an object with CoW children to "xcp-trash-<uuid>" (SR.ls
then ignores it) and calls spawn() to hand the flatten+purge to a detached
rbd_gc.py worker, so destroy returns at once. SR.ls calls sweep() to re-launch
crashed/rebooted jobs and (backstop) re-queue any stranded trash object.

python3, stdlib only.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

try:
    from xapi.storage import log
    def _log(msg):
        log.debug("[rbd-vol gc] " + msg)
except Exception:  # pragma: no cover
    def _log(msg):
        sys.stderr.write("[rbd-vol gc] " + msg + "\n")

GC_SPOOL = "/var/lib/rbdvol/gc"
GC_STALE_AGE = 600            # s; a job file older than this => worker likely died
TRASH_PREFIX = "xcp-trash-"


def _worker():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "rbd_gc.py")


def _launch(job_path):
    with open(os.devnull, "wb") as devnull:
        subprocess.Popen(
            [sys.executable or "python3", _worker(), job_path],
            stdin=devnull, stdout=devnull, stderr=devnull,
            close_fds=True, start_new_session=True)


def spawn(dconf, pool, namespace, kind, image, snap=None):
    """Queue + launch a detached GC job (flatten children + purge trashed object)."""
    try:
        os.makedirs(GC_SPOOL, exist_ok=True)
        os.chmod(GC_SPOOL, 0o700)
    except OSError:
        pass
    job = {"dconf": dconf, "pool": pool, "namespace": namespace,
           "kind": kind, "image": image}
    if snap is not None:
        job["snap"] = snap
    fd, path = tempfile.mkstemp(prefix="gc-", suffix=".json", dir=GC_SPOOL)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(job, f)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        _launch(path)
    except Exception as e:
        _log("worker launch failed (%s); scan will retry: %s" % (path, e))
    _log("queued GC kind=%s img=%s snap=%s job=%s" % (kind, image, snap, path))


def sweep():
    """Re-launch stale job files; return the set of (kind,image,snap) already
    queued so SR.ls won't double-queue a trashed object."""
    targets = set()
    try:
        files = os.listdir(GC_SPOOL)
    except OSError:
        return targets
    now = time.time()
    for fn in files:
        if not fn.endswith(".json"):
            continue
        path = os.path.join(GC_SPOOL, fn)
        try:
            with open(path) as f:
                job = json.load(f)
        except (IOError, OSError, ValueError):
            continue
        targets.add((job.get("kind"), job.get("image"), job.get("snap")))
        try:
            stale = (now - os.path.getmtime(path)) >= GC_STALE_AGE
        except OSError:
            stale = False
        if stale:
            try:
                os.utime(path, None)
                _launch(path)
                _log("re-spawned stale GC job %s" % path)
            except Exception as e:
                _log("could not re-spawn %s: %s" % (path, e))
    return targets
