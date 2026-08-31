#!/usr/bin/python3
#
# dp_tapdisk.py - the "tapdisk" serve mode for the rbd datapath.
#
# Puts a userspace tapdisk in front of the mapped /dev/rbdN (`tap-ctl create -a
# aio:/dev/rbdN`) and returns the resulting /dev/xen/blktap-2/tapdevN with
# backend_type "vbd3" -- the "accepted" xcp-ng path, which also unlocks CBT
# (tap-ctl -C on a cbtlog device) and the tapdisk NBD mirror (tap-ctl -2). We
# drive tap-ctl directly; the py2 xapi.storage.libs.tapdisk lib isn't available
# for python3 on 8.3.
#
# NOTE: the tapdisk fd-passing mirror tees live writes but does not do a race-
# free base copy on its own, so it is NOT a correct SXM source yet -- use the
# qemu mode for live VDI_MIRROR. This mirror/stat code is retained as WIP.

import array
import socket
import subprocess
import urllib.parse

from xapi.storage import log

from dicode.libs import rbd_sysfs
from dicode.libs import cbtlog
from dicode.libs import rbdvol_lib as lib

TAP_CTL = "/usr/sbin/tap-ctl"


def _cbt_meta(dconf, pool, ns, base):
    """Best-effort read of the base image's rbd meta (CBT companion info); {} if
    the control plane is unreachable so a plain attach still succeeds."""
    try:
        be = lib.make_backend(dconf)
        return be.image_info(pool, base, namespace=ns).get("metadata") or {}
    except Exception as e:
        log.debug("cbt meta unavailable for %s: %s" % (base, e))
        return {}


def _tap_create(dev, read_only=False, cbtlog_dev=None):
    cmd = [TAP_CTL, "create", "-a", "aio:%s" % dev]
    if read_only:
        cmd.append("-R")
    if cbtlog_dev:                                    # CBT: track changed blocks
        cmd += ["-C", cbtlog_dev]
    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
    return out.strip().splitlines()[-1].strip()      # /dev/xen/blktap-2/tapdevN


def _tap_find(dev):
    try:
        out = subprocess.check_output([TAP_CTL, "list"],
                                      stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError:
        return None, None
    for line in out.splitlines():
        if ("args=aio:%s" % dev) in line:
            f = dict(kv.split("=", 1) for kv in line.split() if "=" in kv)
            return f.get("pid"), f.get("minor")
    return None, None


def _tap_destroy(pid, minor):
    subprocess.check_call([TAP_CTL, "destroy", "-p", str(pid), "-m", str(minor)])


def attach(dbg, dconf, pool, ns, image, snap, dev):
    read_only = snap is not None
    cbtdev = None
    if not read_only:                    # track writes to the live base via -C
        md = _cbt_meta(dconf, pool, ns, image)
        try:
            cbtdev = cbtlog.attach_live(dconf, pool, ns, image, md)
        except Exception as e:
            log.debug("%s: cbt live map failed: %s" % (dbg, e))
    tapdev = _tap_create(dev, read_only=read_only, cbtlog_dev=cbtdev)
    log.debug("%s: attach %s -> tapdisk %s over %s%s"
              % (dbg, image, tapdev, dev, " +cbt" if cbtdev else ""))
    impls = [
        ["XenDisk", {"backend_type": "vbd3", "params": tapdev, "extra": {}}],
        ["BlockDevice", {"path": tapdev}],
    ]
    # tapdisk3 auto-serves NBD on /run/blktap-control/nbd<pid>.<minor>; advertise
    # it so xapi's SXM nbd_export_of_attach_info finds a server on the dest.
    pid, minor = _tap_find(dev)
    if pid is not None and minor is not None:
        nbd_sock = "/run/blktap-control/nbd%s.%s" % (pid, minor)
        impls.append(["Nbd", {"uri": "nbd:unix:%s:exportname=%s"
                                     % (nbd_sock, image)}])
    return impls


def detach(dbg, dconf, pool, ns, image, snap):
    """Destroy the tapdisk (must run BEFORE the caller unmaps /dev/rbdN) and, for
    a live base, unmap its cbtlog device."""
    dev = rbd_sysfs.find_device(pool, image, snap=snap, namespace=ns)
    if dev:
        pid, minor = _tap_find(dev)
        if pid is not None:
            try:
                _tap_destroy(pid, minor)
            except Exception as e:
                log.debug("%s: tap-ctl destroy failed: %s" % (dbg, e))
    if snap is None:
        try:
            cbtlog.detach_live(pool, ns, image, _cbt_meta(dconf, pool, ns, image))
        except Exception as e:
            log.debug("%s: cbt live unmap warning: %s" % (dbg, e))


def get_nbd_server(dbg, pool, ns, image, snap):
    """SXM dest: return tapdisk's auto-served NBD socket for the mapped device."""
    dev = rbd_sysfs.find_device(pool, image, snap=snap, namespace=ns)
    pid, minor = _tap_find(dev) if dev else (None, None)
    if pid is None:
        raise Exception("get_nbd_server: no tapdisk for %s (dev=%s)"
                        % (image, dev))
    return "/run/blktap-control/nbd%s.%s" % (pid, minor)


def mirror(dbg, pool, ns, image, snap, remote):
    """SXM source (WIP): tapdisk consumes a NAMED FD passed via SCM_RIGHTS to its
    fdreceiver, then `tap-ctl unpause -2 nbd:<export>` arms the tee. Returns
    MirrorV1 "<pid>.<minor>". (Tees new writes only -- no race-free base copy.)"""
    dev = rbd_sysfs.find_device(pool, image, snap=snap, namespace=ns)
    pid, minor = _tap_find(dev) if dev else (None, None)
    if pid is None:
        raise Exception("mirror: no tapdisk for %s (dev=%s)" % (image, dev))
    u = urllib.parse.urlparse(remote)
    label = u.path.lstrip("/")
    nbdproxy_sock = urllib.parse.parse_qs(u.query).get("socket", [""])[0]
    dest = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dest.connect(nbdproxy_sock)
    try:
        ctrl_path = "/var/run/blktap-control/nbdclient%s" % pid
        ctrl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ctrl.connect(ctrl_path)
        try:
            ctrl.sendmsg([label.encode()],
                         [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                           array.array("i", [dest.fileno()]))])
        finally:
            ctrl.close()
    finally:
        dest.close()                                 # tapdisk holds a dup now
    subprocess.check_call([TAP_CTL, "pause", "-p", str(pid), "-m", str(minor)])
    subprocess.check_call([TAP_CTL, "unpause", "-p", str(pid), "-m", str(minor),
                           "-2", "nbd:%s" % label])
    return ["MirrorV1", "%s.%s" % (pid, minor)]


def stat(dbg, key):
    """Report tapdisk mirror progress ([key] = "<pid>.<minor>"). WIP: reports
    in-progress so xapi keeps polling."""
    pid, minor = key.split(".")
    try:
        subprocess.check_output([TAP_CTL, "stats", "-p", pid, "-m", minor],
                                stderr=subprocess.STDOUT)
    except Exception as e:
        log.debug("%s: tap-ctl stats failed: %s" % (dbg, e))
    return {"failed": False, "complete": False, "progress": 0.0}
