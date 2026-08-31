#!/usr/bin/python3
#
# qsd.py - qemu-storage-daemon control for the rbd-vol "qemu" datapath (model C).
#
# Model C serves /dev/rbdN to the guest via the kernel's own blkback, over a
# /dev/nbdX that a per-VDI qemu-storage-daemon (running as root) backs: the
# daemon opens /dev/rbdN as the block node "vol" and exports it over a unix NBD
# socket; nbd-client wires that export to /dev/nbdX; the datapath then hands xapi
# a plain BlockDevice(/dev/nbdX) exactly like the tapdisk datapath hands it a
# tapdev, so blkback serves the guest the canonical way.
#
# The payoff over tapdisk: ALL guest I/O flows through the storage-daemon's "vol"
# node, so a QMP `blockdev-mirror` of that node does a full base copy + live
# write-tee in ONE race-free operation (what tapdisk's tee-only mirror cannot),
# over a QMP socket we own outright -- the basis for SXM (DATA.mirror/stat).
#
# Aligned with xapi.storage.libs.nbdclient (the canonical nbd->block hop) and the
# tapdisk datapath's attach shape.

import json
import os
import signal
import socket
import subprocess
import time

from xapi.storage import log

QSD = "/usr/lib64/qemu-dp/bin/qemu-storage-daemon"
NBD_CLIENT = "/usr/sbin/nbd-client"
_RUN = "/run/rbd-qsd"
NODE = "vol"                      # the single block node inside each daemon
BLOCK_SIZE = "4096"


def _dir(image):
    return os.path.join(_RUN, str(image))


def qmp_sock(image):
    return os.path.join(_dir(image), "qmp.sock")


def nbd_sock(image):
    return os.path.join(_dir(image), "nbd.sock")


def _pid_file(image):
    return os.path.join(_dir(image), "pid")


def _nbd_file(image):
    return os.path.join(_dir(image), "nbddev")


def _running(image):
    """Return the daemon pid if a live qemu-storage-daemon exists, else None."""
    try:
        with open(_pid_file(image)) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def _ensure_nbd_module():
    if not os.path.exists("/dev/nbd0"):
        subprocess.call(["modprobe", "nbd", "nbds_max=64"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def start(dbg, image, dev, export, read_only=False):
    """Start (or reuse) a per-VDI qemu-storage-daemon exporting [dev] as node
    'vol' over a unix NBD socket under export name [export]. Returns the nbd
    socket path."""
    d = _dir(image)
    if _running(image):
        return nbd_sock(image)
    try:
        os.makedirs(d, 0o700)
    except OSError:
        pass
    for p in (qmp_sock(image), nbd_sock(image)):
        try:
            os.unlink(p)
        except OSError:
            pass
    cmd = [
        QSD,
        "--pidfile", _pid_file(image),
        "--blockdev",
        "driver=host_device,node-name=%s,filename=%s,cache.direct=on,"
        "cache.no-flush=off,discard=unmap,read-only=%s"
        % (NODE, dev, "on" if read_only else "off"),
        "--nbd-server", "addr.type=unix,addr.path=%s" % nbd_sock(image),
        "--export",
        "type=nbd,id=e0,node-name=%s,name=%s,writable=%s"
        % (NODE, export, "off" if read_only else "on"),
        "--chardev", "socket,id=qmp0,path=%s,server=on,wait=off" % qmp_sock(image),
        "--monitor", "chardev=qmp0",
        "--daemonize",
    ]
    log.debug("%s: qsd start: %s" % (dbg, " ".join(cmd)))
    subprocess.check_call(cmd)
    for _ in range(50):
        if os.path.exists(nbd_sock(image)) and os.path.exists(qmp_sock(image)):
            break
        time.sleep(0.1)
    if not os.path.exists(nbd_sock(image)):
        raise Exception("qsd: nbd socket never appeared: %s" % nbd_sock(image))
    return nbd_sock(image)


def _nbd_in_use():
    used = set()
    try:
        for name in os.listdir("/sys/block"):
            if name.startswith("nbd") and os.path.exists(
                    "/sys/block/%s/pid" % name):
                used.add(name)
    except OSError:
        pass
    return used


def nbd_attach(dbg, image, export):
    """Wire the storage-daemon's unix NBD export to a free /dev/nbdX (kernel
    nbd-client). Returns the device path and remembers it for detach."""
    _ensure_nbd_module()
    sock = nbd_sock(image)
    used = _nbd_in_use()
    names = sorted((n for n in os.listdir("/dev")
                    if n.startswith("nbd") and n[3:].isdigit()),
                   key=lambda n: int(n[3:]))
    for name in names:
        if name in used:
            continue
        dev = "/dev/" + name
        try:
            subprocess.check_call(
                [NBD_CLIENT, "-u", "-N", export, sock, dev, "-b", BLOCK_SIZE],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            log.debug("%s: nbd-client %s busy/failed: %s" % (dbg, dev, e))
            continue
        try:
            with open("/sys/block/%s/queue/scheduler" % name, "w") as fd:
                fd.write("none")
        except Exception:
            pass
        with open(_nbd_file(image), "w") as fd:
            fd.write(dev)
        log.debug("%s: qsd nbd_attach %s -> %s (export %s)"
                  % (dbg, sock, dev, export))
        return dev
    raise Exception("qsd: no free /dev/nbd device")


def find_nbd(image):
    try:
        with open(_nbd_file(image)) as f:
            return f.read().strip()
    except Exception:
        return None


def nbd_detach(dbg, dev):
    try:
        subprocess.check_call([NBD_CLIENT, "-d", dev],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.STDOUT)
    except Exception as e:
        log.debug("%s: nbd-client -d %s: %s" % (dbg, dev, e))


def stop(dbg, image):
    """Disconnect the nbd device (if any) and terminate the daemon, waiting for
    it to release /dev/rbdN before the caller unmaps."""
    dev = find_nbd(image)
    if dev:
        nbd_detach(dbg, dev)
    pid = _running(image)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            if not _running(image):
                break
            time.sleep(0.1)
    for p in (qmp_sock(image), nbd_sock(image), _pid_file(image),
              _nbd_file(image)):
        try:
            os.unlink(p)
        except OSError:
            pass
    try:
        os.rmdir(_dir(image))
    except OSError:
        pass


class Qmp(object):
    """Minimal dependency-free QMP client for the storage-daemon (SXM mirror)."""

    def __init__(self, dbg, image):
        self.dbg = dbg
        self.image = image
        self._s = None
        self._f = None

    def __enter__(self):
        self._s = socket.socket(socket.AF_UNIX)
        self._s.connect(qmp_sock(self.image))
        self._f = self._s.makefile("rw")
        self._f.readline()                       # QMP greeting
        self.cmd("qmp_capabilities")
        return self

    def __exit__(self, *a):
        try:
            self._s.close()
        except Exception:
            pass

    def cmd(self, execute, **args):
        o = {"execute": execute}
        if args:
            o["arguments"] = args
        self._f.write(json.dumps(o) + "\r\n")
        self._f.flush()
        while True:
            line = self._f.readline()
            if not line:
                raise Exception("qsd qmp: connection closed")
            r = json.loads(line)
            if "event" in r:                     # skip async events
                continue
            log.debug("%s: qsd qmp %s -> %s" % (self.dbg, execute, str(r)[:200]))
            if "error" in r:                     # surface QMP failures loudly
                raise Exception("qsd qmp %s failed: %s" % (execute, r["error"]))
            return r
