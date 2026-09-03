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
# xapi's own nbd<->/dev/nbdX manager. Using it (rather than nbd-client directly)
# is REQUIRED for correctness: connect writes /var/run/nonpersistent/nbd/<N>, the
# connect-info file that xapi's vhd-tool copy path reads to follow /dev/nbdN back
# to its NBD server -- without it VDI.copy TO this datapath dies with ENOENT on
# that file. It also shares xapi's FILE_LOCK, so we never race xapi for a device.
NBD_MANAGER = "/opt/xensource/libexec/nbd_client_manager.py"
_PERSIST_DIR = "/var/run/nonpersistent/nbd"
_RUN = "/run/rbd-qsd"
NODE = "vol"                      # the single block node inside each daemon
BLOCK_SIZE = "4096"


def _require_bin(path, pkg):
    """The qemu serve mode + SXM are optional -- their tools (qemu-dp, nbd) are
    NOT hard RPM deps (el7 rpm has no weak deps). Fail with a clear, actionable
    error if the datapath is used in a mode whose tool is not installed, instead
    of a raw FileNotFoundError from subprocess."""
    if not os.path.exists(path):
        raise Exception(
            "qemu datapath mode needs %s (%s), which is not installed. "
            "Install it, or use device-config datapath=blkback (default) / "
            "datapath=tapdisk." % (path, pkg))


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
    _require_bin(QSD, "qemu-dp")
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


def _persist_nbd_info(dev, sock, export):
    """Write the connect-info file xapi's copy path expects for /dev/nbdN (fallback
    for when nbd_client_manager.py is absent; the manager writes it itself)."""
    num = os.path.basename(dev)[len("nbd"):]
    try:
        os.makedirs(_PERSIST_DIR)
    except OSError:
        pass
    with open(os.path.join(_PERSIST_DIR, num), "w") as f:
        f.write(json.dumps({"path": sock, "exportname": export}))


# ---- attach refcount (one qsd+nbd shared across an image's attaches) ----
# Datapath.attach/detach are called once PER CONSUMER of a VDI: on an SXM RECEIVE
# receive_start3 attaches (vm=MIR...) AND the guest activates (vm=<domid>); an SXM
# SOURCE has the guest attach + the mirror dp attach. Without accounting, each
# attach wired a fresh /dev/nbdX (leak) and the FIRST detach tore the whole qsd
# down under a still-attached guest (I/O error / crash). We key a ref on the
# domain so the qsd+nbd are built once and torn down only when the LAST consumer
# detaches.

def _refs_dir(image):
    return os.path.join(_RUN, image, "refs")


def _ref_token(domain):
    return str(domain).replace("/", "_") or "none"


def ref_add(dbg, image, domain):
    d = _refs_dir(image)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, _ref_token(domain)), "w").close()
    n = len(os.listdir(d))
    log.debug("%s: qsd ref+ %s dom=%s -> %d" % (dbg, image, domain, n))
    return n


def ref_del(dbg, image, domain):
    """Drop this domain's ref; return the number of refs still held (0 => the
    caller should tear the qsd down)."""
    d = _refs_dir(image)
    try:
        os.unlink(os.path.join(d, _ref_token(domain)))
    except OSError:
        pass
    try:
        n = len(os.listdir(d))
    except OSError:
        n = 0
    log.debug("%s: qsd ref- %s dom=%s -> %d" % (dbg, image, domain, n))
    return n


def _nbd_wired_to(dev, sock):
    """True if [dev] is a live nbd-client connection to THIS qsd's [sock] (per the
    /var/run/nonpersistent/nbd/<N> connect-info) -- safe to reuse on a repeat
    attach."""
    try:
        num = os.path.basename(dev)[len("nbd"):]
        if not os.path.exists("/sys/block/nbd%s/pid" % num):
            return False
        with open(os.path.join(_PERSIST_DIR, num)) as f:
            return json.load(f).get("path") == sock
    except Exception:
        return False


def nbd_attach(dbg, image, export):
    """Wire the storage-daemon's unix NBD export to a free /dev/nbdX. Prefer xapi's
    nbd_client_manager.py -- it writes the /var/run/nonpersistent/nbd/<N> info file
    that vhd-tool needs (so VDI.copy TO this SR works) and holds xapi's device
    lock. Returns the device path and remembers it for detach.

    Idempotent: a repeat attach for the same image (second consumer) reuses the
    device already wired to this qsd rather than allocating -- and leaking -- a
    second one. Safe because teardown is refcounted (ref_del), so the shared
    device is only disconnected when the last consumer detaches."""
    _ensure_nbd_module()
    sock = nbd_sock(image)
    existing = find_nbd(image)
    if existing and _nbd_wired_to(existing, sock):
        log.debug("%s: qsd nbd_attach %s -> reuse %s" % (dbg, image, existing))
        return existing
    if os.path.exists(NBD_MANAGER):
        dev = subprocess.check_output(
            [NBD_MANAGER, "connect", "--path", sock, "--exportname", export],
            stderr=subprocess.STDOUT).decode().strip()
        with open(_nbd_file(image), "w") as fd:
            fd.write(dev)
        log.debug("%s: qsd nbd_attach %s -> %s (export %s, via manager)"
                  % (dbg, sock, dev, export))
        return dev
    # Fallback: no manager -- wire nbd-client ourselves AND write the info file.
    _require_bin(NBD_CLIENT, "nbd")
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
        _persist_nbd_info(dev, sock, export)
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
    if os.path.exists(NBD_MANAGER):
        try:
            subprocess.check_call([NBD_MANAGER, "disconnect", "--device", dev],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.STDOUT)
            return
        except Exception as e:
            log.debug("%s: nbd manager disconnect %s: %s" % (dbg, dev, e))
    try:
        subprocess.check_call([NBD_CLIENT, "-d", dev],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.STDOUT)
    except Exception as e:
        log.debug("%s: nbd-client -d %s: %s" % (dbg, dev, e))
    try:
        os.unlink(os.path.join(_PERSIST_DIR, os.path.basename(dev)[len("nbd"):]))
    except OSError:
        pass


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
    d = _refs_dir(image)                              # drop any leftover refs
    try:
        for t in os.listdir(d):
            os.unlink(os.path.join(d, t))
        os.rmdir(d)
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
