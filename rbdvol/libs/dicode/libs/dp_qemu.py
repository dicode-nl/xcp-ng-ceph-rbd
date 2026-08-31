#!/usr/bin/python3
#
# dp_qemu.py - the "qemu" serve mode for the rbd datapath.
#
# A per-VDI qemu-storage-daemon exports the mapped /dev/rbdN over a unix NBD
# socket; the kernel blkback then serves the guest a plain /dev/nbdX (so no
# qemu/qdisk guest-serving and no libxl-QMP involvement). Because ALL guest I/O
# flows through the storage-daemon's 'vol' node, DATA.mirror can drive the
# daemon's blockdev-mirror (full base copy + live write-tee, race-free) -- real
# live SXM for raw rbd. Storage-daemon control lives in dicode.libs.qsd.

import os
import time
import urllib.parse

from xapi.storage import log

from dicode.libs import qsd


def _export(image):
    return str(image).replace("-", "")


def attach(dbg, image, snap, dev):
    """Return the SMAPIv3 implementations for the qemu serve mode. BlockDevice
    wins in xenopsd's params_of_backend (params=/dev/nbdX, backend-kind=vbd ->
    blkback serves the guest); the Nbd impl is the SXM handle (receive_start3
    reads its exportname, and DATA.get_nbd_server hands xapi this same socket)."""
    read_only = snap is not None
    export = _export(image)
    sock = qsd.start(dbg, image, dev, export, read_only=read_only)
    nbddev = qsd.nbd_attach(dbg, image, export)
    log.debug("%s: attach %s -> qsd nbd %s over %s (blkback)"
              % (dbg, image, nbddev, dev))
    return [
        ["XenDisk", {"backend_type": "vbd", "params": nbddev, "extra": {}}],
        ["BlockDevice", {"path": nbddev}],
        ["Nbd", {"uri": "nbd:unix:%s:exportname=%s" % (sock, export)}],
    ]


def drain_mirror(dbg, image):
    """If an SXM mirror block-job is still running, drive it to a final sync +
    completion so the destination is fully in-sync BEFORE the storage-daemon is
    torn down. This runs from Datapath.detach, which (measured) fires while the
    guest is PAUSED and BEFORE the destination resumes -- so the residual drains
    with no new writes and no race with the resumed dest guest. Without this, the
    async mirror job would be hard-killed with the last writes still un-copied
    (silent data loss for an actively-writing guest). No-op if there is no job."""
    try:
        with qsd.Qmp(dbg, image) as q:
            jobs = q.cmd("query-block-jobs").get("return", [])
            job = next((j for j in jobs if j.get("type") == "mirror"), None)
            if not job:
                return
            jid = job.get("device") or job.get("id")
            for _ in range(600):                     # wait until 'ready' (~60s)
                jobs = q.cmd("query-block-jobs").get("return", [])
                m = next((j for j in jobs
                          if (j.get("device") or j.get("id")) == jid), None)
                if m is None:
                    return                           # gone -> already finished
                if m.get("ready"):
                    break
                time.sleep(0.1)
            # block-job-complete does a final synchronous drain then finishes;
            # the mirror's pivot is a harmless side-effect (the daemon is killed
            # right after and /dev/rbdN is untouched).
            q.cmd("block-job-complete", device=jid)
            for _ in range(600):                     # wait for the job to end
                jobs = q.cmd("query-block-jobs").get("return", [])
                if not any((j.get("device") or j.get("id")) == jid
                           for j in jobs):
                    log.debug("%s: mirror %s drained + completed" % (dbg, image))
                    return
                time.sleep(0.1)
            log.debug("%s: mirror %s did not finish draining in time"
                      % (dbg, image))
    except Exception as e:
        log.debug("%s: drain_mirror(%s) failed: %s" % (dbg, image, e))


def detach(dbg, image):
    """Tear down the nbd device + storage-daemon (must run BEFORE the caller
    unmaps /dev/rbdN, which the daemon holds open). First drain any live SXM
    mirror so no writes are lost, then qsd.stop waits for the daemon to exit."""
    drain_mirror(dbg, image)
    try:
        qsd.stop(dbg, image)
    except Exception as e:
        log.debug("%s: qsd stop failed: %s" % (dbg, e))


def get_nbd_server(dbg, image):
    """SXM dest: return the storage-daemon's NBD unix socket, which xapi's
    import_nbd_proxy connects to and proxies the incoming mirror stream into."""
    sock = qsd.nbd_sock(image)
    if not os.path.exists(sock):
        raise Exception("get_nbd_server: no storage-daemon for %s" % image)
    log.debug("%s: get_nbd_server %s -> %s" % (dbg, image, sock))
    return sock


def mirror(dbg, image, remote):
    """SXM source: mirror the storage-daemon's live 'vol' node to [remote] =
    nbd+unix:///<export>?socket=<proxy_sock> via qemu blockdev-mirror (base copy
    + live tee in one). Returns the MirrorV1 handle "<image>|<job>"."""
    u = urllib.parse.urlparse(remote)
    export = u.path.lstrip("/")
    proxy_sock = urllib.parse.parse_qs(u.query).get("socket", [""])[0]
    job = "m_" + export[:32]
    with qsd.Qmp(dbg, image) as q:
        q.cmd("blockdev-add", **{
            "driver": "nbd", "node-name": "dst",
            "server": {"type": "unix", "path": proxy_sock},
            "export": export,
        })
        q.cmd("blockdev-mirror", **{
            "job-id": job, "device": qsd.NODE, "target": "dst", "sync": "full",
        })
    log.debug("%s: mirror %s export=%s job=%s" % (dbg, image, export, job))
    return ["MirrorV1", "%s|%s" % (image, job)]


def stat(dbg, key):
    """Report the blockdev-mirror job status ([key] = "<image>|<job>"). 'ready'
    (base copy done, now steady-state teeing) => complete=True; an absent job =>
    failed (the job holds itself in 'ready' until finalize, so it must be present
    while xapi polls -- never report a silent, data-losing 'complete')."""
    image, job = key.split("|", 1)
    try:
        with qsd.Qmp(dbg, image) as q:
            jobs = q.cmd("query-block-jobs").get("return", [])
    except Exception as e:
        log.debug("%s: stat %s qmp error: %s" % (dbg, key, e))
        return {"failed": True, "complete": False, "progress": None}
    for j in jobs or []:
        if j.get("device") == job or j.get("id") == job:
            ln = j.get("len") or 0
            off = j.get("offset") or 0
            prog = float(off) / float(ln) if ln else 0.0
            failed = j.get("status") in ("aborting", "concluded") \
                and not j.get("ready")
            return {"failed": bool(failed), "complete": bool(j.get("ready")),
                    "progress": prog}
    log.debug("%s: stat %s: job absent -> failed" % (dbg, key))
    return {"failed": True, "complete": False, "progress": None}
