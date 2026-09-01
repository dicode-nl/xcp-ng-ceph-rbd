#!/usr/bin/python3
#
# blknbd.py - qemu-nbd control: expose a mapped /dev/rbdN over a unix-socket NBD
# server.
#
# Used by the blkback serve mode to be an SXM DESTINATION (receive an incoming
# mirror into /dev/rbdN) without adding a userspace process to the guest data
# path -- the qemu-nbd runs ONLY during the receive (started lazily by the
# datapath's get_nbd_server, stopped on detach). qemu-nbd over /dev/rbdN is plain
# block I/O on the kernel krbd device (not a second rbd client), so it neither
# fights the krbd exclusive-lock nor changes how the guest is later served.

import os
import signal
import subprocess
import time

from xapi.storage import log

QEMU_NBD = "/usr/lib64/qemu-dp/bin/qemu-nbd"
_RUN = "/run/rbd-nbd"


def export(image):
    return str(image).replace("-", "")


def _dir(image):
    return os.path.join(_RUN, str(image))


def sock(image):
    return os.path.join(_dir(image), "nbd.sock")


def _pid_file(image):
    return os.path.join(_dir(image), "pid")


def _log_file(image):
    return os.path.join(_dir(image), "qemu-nbd.log")


def _running(image):
    try:
        with open(_pid_file(image)) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def serve(dbg, image, dev, read_only=False):
    """Start (or reuse) a qemu-nbd exporting [dev] over a unix socket; return the
    socket path. Idempotent."""
    if not os.path.exists(QEMU_NBD):
        raise Exception(
            "SXM receive into the blkback datapath needs %s (qemu-dp), which is "
            "not installed." % QEMU_NBD)
    if _running(image):
        return sock(image)
    try:
        os.makedirs(_dir(image), 0o700)
    except OSError:
        pass
    try:
        os.unlink(sock(image))
    except OSError:
        pass
    cmd = [QEMU_NBD, "--socket", sock(image), "--format", "raw",
           "--export-name", export(image), "--persistent", "--shared", "8",
           "--pid-file", _pid_file(image), "--cache", "none",
           "--aio", "native", "--discard", "unmap"]
    if read_only:
        cmd.append("--read-only")
    cmd.append(dev)
    log.debug("%s: blknbd serve: %s" % (dbg, " ".join(cmd)))
    # Launch DETACHED, with NO inherited stdio. qemu-nbd --persistent runs forever,
    # so we must not wait on it; and its --fork does NOT close stdio, so a forked
    # daemon would inherit -- and hold open -- the caller's stdout, which is the
    # pipe xapi reads DATA.get_nbd_server's result from. That kept the read from
    # ever seeing EOF and hung the whole SXM receive. So: own session (survives the
    # short-lived script), stdio to a log file, and poll for the socket instead.
    logf = open(_log_file(image), "ab", 0)
    devnull = open(os.devnull, "rb")
    try:
        proc = subprocess.Popen(cmd, stdin=devnull, stdout=logf, stderr=logf,
                                start_new_session=True, close_fds=True)
    finally:
        logf.close()
        devnull.close()
    for _ in range(100):
        if os.path.exists(sock(image)):
            return sock(image)
        if proc.poll() is not None:
            raise Exception("blknbd: qemu-nbd exited rc=%s (see %s)"
                            % (proc.returncode, _log_file(image)))
        time.sleep(0.1)
    raise Exception("blknbd: socket never appeared: %s" % sock(image))


def stop(dbg, image):
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
    for p in (sock(image), _pid_file(image), _log_file(image)):
        try:
            os.unlink(p)
        except OSError:
            pass
    try:
        os.rmdir(_dir(image))
    except OSError:
        pass
