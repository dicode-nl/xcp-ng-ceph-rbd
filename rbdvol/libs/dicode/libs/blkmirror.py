#!/usr/bin/python3
#
# blkmirror.py - SXM SOURCE for the blkback (raw krbd) serve mode, via an
# iterative rbd-diff pre-copy.
#
# The blkback datapath hands the guest the raw /dev/rbdN through kernel blkback,
# so there is NO userspace hop to tee live writes (unlike the qemu mode's
# blockdev-mirror). To migrate such a disk off the SR we instead do a dirty-block
# pre-copy, exactly like a pre-copy live migration:
#
#   1. snapshot the (live, still-being-written) source image, diff it against the
#      previous snapshot (rbd fast-diff / object-map via the backend's image_diff,
#      the same machinery CBT uses), and copy only the changed 4 MiB objects to
#      the destination;
#   2. repeat -- each pass ships the delta accumulated since the last snapshot --
#      until a pass falls under THRESHOLD bytes (converged) or MAX_ITERS passes;
#   3. the FINAL, small delta is copied in the guest-PAUSED cutover window (our
#      Datapath.detach on the mirror dp, measured to run guest-paused and BEFORE
#      the destination resumes), so no write is missed.
#
# The destination is reached the same way the qemu mode reaches it: xapi's
# send_start spawns an nbd proxy at a unix socket and hands us
# `remote = nbd+unix:///<export>?socket=<proxy_sock>`; we wire that export to a
# local /dev/nbdX with xapi's nbd_client_manager.py and pwrite the changed
# objects into it. Reads come from a read-only krbd map of each SOURCE snapshot
# (a distinct /dev/rbdN, independent of the guest's live base map).
#
# The heavy copy loop runs in a detached worker (rbd_blkmir.py); this module is
# the control side imported by the datapath: mirror() starts it, stat() reports
# progress, finalize() does the paused final delta + teardown, cancel() cleans up
# a failed/aborted migration.
#
# python3, stdlib only (+ dicode.libs).

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse

try:
    from xapi.storage import log as _xlog
except Exception:  # pragma: no cover
    _xlog = None

_LOGFILE = "/var/log/rbd-blkmir.log"


def _log(msg):
    if _xlog is not None:
        try:
            _xlog.debug("[rbd-vol blkmir] " + msg)
        except Exception:
            pass
    try:                                             # durable trace (SMlog debug
        import time as _t                            # is not always captured)
        with open(_LOGFILE, "a") as f:
            f.write("%s [%d] %s\n" % (_t.strftime("%H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass

from dicode.libs import rbd_sysfs
from dicode.libs.rbd_backend import make_backend

STATE_ROOT = "/run/rbd-blkmir"
NBD_CLIENT = "/usr/sbin/nbd-client"
HANDLE_PREFIX = "blkmir:"

# Convergence policy (overridable per-SR via device-config).
DEF_THRESHOLD = 256 * 1024 * 1024    # a pass under this many bytes => converged
DEF_MAX_ITERS = 8                    # ... or give up converging after this many
SNAP_PREFIX = "xcp-sxm-"             # source snapshots we create + reap
COPY_CHUNK = 4 * 1024 * 1024         # pread/pwrite granularity


# ---------------- state helpers ----------------

def _dir(image):
    return os.path.join(STATE_ROOT, image)


def _job_path(image):
    return os.path.join(_dir(image), "job.json")


def _status_path(image):
    return os.path.join(_dir(image), "status.json")


def _stop_path(image):
    return os.path.join(_dir(image), "stop")


def active(image):
    """True if a mirror (running or converged, not yet finalized) exists."""
    return os.path.exists(_job_path(image))


def read_status(image):
    try:
        with open(_status_path(image)) as f:
            return json.load(f)
    except Exception:
        return {}


def write_status(image, **kw):
    """Atomically merge kw into the status file."""
    st = read_status(image)
    st.update(kw)
    tmp = _status_path(image) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, _status_path(image))
    return st


def _read_job(image):
    with open(_job_path(image)) as f:
        return json.load(f)


# ---------------- NBD wiring to the destination ----------------

def _parse_remote(remote):
    """remote = nbd+unix:///<export>?socket=<proxy_sock> -> (export, sock)."""
    u = urllib.parse.urlparse(remote)
    export = u.path.lstrip("/")
    sock = urllib.parse.parse_qs(u.query).get("socket", [""])[0]
    if not export or not sock:
        raise Exception("blkmir: cannot parse remote %r" % remote)
    return export, sock


def _nbd_connect(export, sock):
    """Wire the destination nbd export to a free /dev/nbdX; return its path.

    Uses nbd-client DIRECTLY, NOT xapi's nbd_client_manager.py: the manager
    serialises every connect on a single global lock, and the SXM receive side
    ALSO connects through it -- so our source connect (holding the lock while its
    handshake waits for the dest to be wired) would deadlock the dest connect
    (blocked on the lock). We do plain raw pwrites, so we don't need the
    manager's /var/run/nonpersistent/nbd/<N> info file (that's only for the
    vhd-tool copy path).

    We let nbd-client pick the device via NETLINK (no device arg): the kernel
    hands us a genuinely-free device, avoiding the races of choosing one
    ourselves (a device can pass `nbd-client -check` yet still be a bound-but-
    dead zombie the kernel then refuses with "already in use"). xapi's send_start
    nbd proxy is ONE-SHOT (accepts a single connection, then HTTP-forwards it to
    the destination), so we connect exactly ONCE; `-timeout 90` waits out the
    latency of the destination wiring up behind the proxy.

    NOT `-persist`: this is a transient transport, torn down at finalize. With
    -persist, once xapi kills the proxy at the end of the migration nbd-client
    spins reconnecting to the dead proxy and our `nbd-client -d` can't cleanly
    disconnect it -> the device lingers bound-but-dead (a zombie). Without it the
    disconnect is clean and no device leaks."""
    if not os.path.exists("/dev/nbd0"):
        subprocess.call(["/usr/sbin/modprobe", "nbd", "nbds_max=64"])
    cmd = [NBD_CLIENT, "-unix", sock, "-timeout", "90", "-name", export]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=120)
    out = (p.stdout or b"").decode(errors="replace").strip()
    _log("nbd-client rc=%d out=%r" % (p.returncode, out))
    m = re.search(r"/dev/nbd\d+", out)
    if p.returncode != 0 or not m:
        raise Exception("blkmir: nbd-client -> %s failed: rc=%d: %s"
                        % (export, p.returncode, out))
    dev = m.group(0)
    try:
        with open("/sys/block/%s/queue/scheduler" % os.path.basename(dev),
                  "w") as f:
            f.write("none")
    except Exception:
        pass
    return dev


def _nbd_disconnect(dev):
    if not dev:
        return
    try:
        p = subprocess.run([NBD_CLIENT, "-d", dev], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=30)
        _log("nbd disconnect %s rc=%d out=%r" % (
            dev, p.returncode, (p.stdout or b"").decode(errors="replace").strip()))
    except Exception as e:
        _log("nbd disconnect %s warning: %s" % (dev, e))


# ---------------- the copy primitive (shared by worker + finalize) ----------------

def copy_delta(dconf, pool, ns, image, from_snap, to_snap, nbd_dev):
    """Copy the objects that changed between two SOURCE snapshots to the dest
    /dev/nbdX. Reads from a read-only krbd map of `to_snap` (a point-in-time,
    consistent view), writes the same offsets on the destination. Returns the
    number of bytes copied. from_snap=None => full copy (all allocated objects)."""
    be = make_backend(dconf)
    diffs = be.image_diff(pool, image, from_snap, to_snap,
                          whole_object=True, namespace=ns) or []
    extents = [(int(d["offset"]), int(d["length"]))
               for d in diffs if d.get("exists")]
    if not extents:
        return 0
    src = rbd_sysfs.map_image(
        dconf["mon_host"], dconf.get("user", "admin"), dconf["key"],
        pool, image, snap=to_snap, ms_mode=dconf.get("ms_mode", "prefer-crc"),
        read_only=True, namespace=ns)
    total = 0
    sfd = os.open(src, os.O_RDONLY)
    dfd = os.open(nbd_dev, os.O_WRONLY)
    try:
        for off, length in extents:
            pos, end = off, off + length
            while pos < end:
                n = min(COPY_CHUNK, end - pos)
                buf = os.pread(sfd, n, pos)
                if not buf:
                    break
                os.pwrite(dfd, buf, pos)
                pos += len(buf)
                total += len(buf)
    finally:
        os.close(sfd)
        try:
            os.fsync(dfd)
        except OSError:
            pass
        os.close(dfd)
        try:
            rbd_sysfs.unmap_image(pool, image, snap=to_snap, namespace=ns)
        except Exception as e:
            _log("unmap snap %s@%s warning: %s" % (image, to_snap, e))
    return total


# ---------------- control API (called from datapath.py) ----------------

def mirror(dbg, dconf, pool, ns, image, remote):
    """SXM source: wire up the destination NBD, spawn the pre-copy worker, return
    a "blkmir:<image>" handle. Raises on setup failure (xapi then aborts)."""
    export, sock = _parse_remote(remote)
    os.makedirs(_dir(image), exist_ok=True)
    os.chmod(STATE_ROOT, 0o700)
    nbd_dev = _nbd_connect(export, sock)
    job = {
        "dconf": dconf, "pool": pool, "ns": ns, "image": image,
        "remote": remote, "export": export, "sock": sock, "nbd_dev": nbd_dev,
        "threshold": int(dconf.get("sxm_threshold") or DEF_THRESHOLD),
        "max_iters": int(dconf.get("sxm_max_iters") or DEF_MAX_ITERS),
    }
    with open(_job_path(image), "w") as f:
        json.dump(job, f)
    write_status(image, phase="starting", progress=0.0, complete=False,
                 failed=False, baseline_snap=None, iters=0, pid=None)
    try:
        os.unlink(_stop_path(image))
    except OSError:
        pass
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "rbd_blkmir.py")
    with open(os.path.join(_dir(image), "worker.log"), "ab") as lf:
        p = subprocess.Popen([sys.executable or "python3", worker, image],
                             stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                             close_fds=True, start_new_session=True)
    write_status(image, pid=p.pid)
    _log("%s: mirror %s started (nbd_dev=%s export=%s worker=%d)"
         % (dbg, image, nbd_dev, export, p.pid))
    # xapi's DATA.mirror expects a tagged MirrorV1/CopyV1 variant, like the qemu
    # mode; DATA.stat is then invoked with operation = ["MirrorV1", <key>].
    return ["MirrorV1", HANDLE_PREFIX + image]


def stat(dbg, key):
    """Report the pre-copy status. 'complete' (converged or MAX_ITERS reached)
    tells xapi it may proceed to the memory migration + cutover; the worker keeps
    pre-copying to stay caught up until finalize. An absent/failed job => failed
    (never a silent, data-losing 'complete')."""
    image = key[len(HANDLE_PREFIX):] if key.startswith(HANDLE_PREFIX) else key
    # xapi requires progress to be a float (never None). state absent => the
    # mirror already finalized (finalize removes it AFTER complete was reported),
    # so report done rather than failed.
    if not active(image):
        return {"failed": False, "complete": True, "progress": 1.0}
    st = read_status(image)
    if st.get("failed"):
        return {"failed": True, "complete": False, "progress": 0.0}
    return {"failed": False, "complete": bool(st.get("complete")),
            "progress": float(st.get("progress") or 0.0)}


def _worker_pid(image):
    return (read_status(image) or {}).get("pid")


def _stop_worker(image, timeout=120):
    """Ask the worker to stop after its current pass and wait for it to exit."""
    open(_stop_path(image), "w").close()
    pid = _worker_pid(image)
    if not pid:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return                    # gone
        time.sleep(0.2)
    _log("worker %d for %s did not exit in %ds; killing" % (pid, image, timeout))
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def finalize(dbg, dconf, pool, ns, image):
    """Cutover (guest PAUSED): stop the pre-copy, ship the final delta since the
    last baseline snapshot, then tear everything down. Idempotent -- the first
    detach to see a converged, non-failed, un-finalized mirror runs it."""
    st = read_status(image)
    if st.get("finalized"):
        return
    write_status(image, finalized=True, phase="finalizing")
    job = _read_job(image)
    nbd_dev = job["nbd_dev"]
    baseline = st.get("baseline_snap")
    _stop_worker(image)
    final_snap = SNAP_PREFIX + "final-" + str(int(time.time()))
    be = make_backend(dconf)
    try:
        be.snap_create(pool, image, final_snap, namespace=ns)
        n = copy_delta(dconf, pool, ns, image, baseline, final_snap, nbd_dev)
        _log("%s: finalize %s copied final delta %d bytes (baseline=%s)"
             % (dbg, image, n, baseline))
    finally:
        for s in (final_snap, baseline):
            if s:
                try:
                    be.snap_remove(pool, image, s, namespace=ns)
                except Exception as e:
                    _log("finalize snap_remove %s@%s warning: %s"
                         % (image, s, e))
        _nbd_disconnect(nbd_dev)
        _rmstate(image)


def cancel(dbg, dconf, pool, ns, image):
    """Abort a running/failed migration: stop the worker, drop the baseline
    snapshot, disconnect the dest NBD, remove state. Best-effort/idempotent."""
    if not active(image):
        return
    st = read_status(image)
    _stop_worker(image, timeout=30)
    baseline = st.get("baseline_snap")
    try:
        job = _read_job(image)
        _nbd_disconnect(job.get("nbd_dev"))
    except Exception:
        pass
    if baseline:
        try:
            make_backend(dconf).snap_remove(pool, image, baseline, namespace=ns)
        except Exception as e:
            _log("cancel snap_remove %s@%s warning: %s" % (image, baseline, e))
    _rmstate(image)
    _log("%s: mirror %s cancelled" % (dbg, image))


def _rmstate(image):
    d = _dir(image)
    for name in ("job.json", "status.json", "status.json.tmp", "stop",
                 "worker.log"):
        try:
            os.unlink(os.path.join(d, name))
        except OSError:
            pass
    try:
        os.rmdir(d)
    except OSError:
        pass
