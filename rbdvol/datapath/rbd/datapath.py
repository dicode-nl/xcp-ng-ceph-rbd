#!/usr/bin/python3
#
# datapath.py - Datapath.* for the rbd-vol SMAPIv3 plugin (scheme "rbd://").
#
# Maps the RBD image via native krbd (/sys/bus/rbd) to /dev/rbdN, then serves it
# to xenopsd in one of two modes (chosen by device-config `datapath=` or a URI
# `?dp=` query; default blkback):
#   * blkback  -> return the raw /dev/rbdN with backend_type "vbd" (kernel
#                 blkback, tapdisk-less, max performance).
#   * tapdisk  -> `tap-ctl create -a aio:/dev/rbdN` puts a userspace tapdisk in
#                 front and we return its /dev/xen/blktap-2/tapdevN with
#                 backend_type "vbd3" -- the "accepted" xcp-ng path, and the one
#                 that unlocks CBT (tap-ctl -C) and SXM mirror (tap-ctl -2).
# Same functionality as zfs-vol + the stock tapdisk datapath, but driven from
# python3 via tap-ctl (the py2 xapi.storage.libs.tapdisk lib isn't available for
# python3 on 8.3).
#
# URI: rbd://<sr_uuid>/<key>[?dp=blkback|tapdisk]  (key = "<image>" or "<base>@<snap>")
# Config (mon_host/user/cephx key/ms_mode/pool/namespace/datapath) comes from the
# per-host SR metadata store written by SR.attach -- secrets stay out of the URI.

import os
import subprocess
import sys
import urllib.parse

import xapi.storage.api.v5.datapath
from xapi.storage import log

import srmeta
import rbd_sysfs
import cbtlog
import rbdvol_lib as lib

TAP_CTL = "/usr/sbin/tap-ctl"


def _cbt_meta(dconf, pool, ns, base):
    """Best-effort read of the base image's rbd meta (for CBT companion info).
    Uses the stock dashboard REST -- needs NO /diff patch. Returns {} if the
    control plane is unreachable so a plain attach still succeeds (writes just
    won't be CBT-tracked for that attach)."""
    try:
        be = lib.make_backend(dconf)
        return be.image_info(pool, base, namespace=ns).get("metadata") or {}
    except Exception as e:
        log.debug("cbt meta unavailable for %s: %s" % (base, e))
        return {}


def _parse_uri(uri):
    u = urllib.parse.urlparse(uri)
    return u.netloc, u.path.lstrip("/"), u.query     # (sr_uuid, key, query)


def _resolve(uri):
    sr_uuid, key, query = _parse_uri(uri)
    meta = srmeta.read(sr_uuid)
    dconf = meta["dconf"]
    pool = dconf["pool"]
    ns = dconf.get("namespace") or meta["sr_uuid"]
    image, snap = (key.split("@", 1) + [None])[:2] if "@" in key else (key, None)
    q = urllib.parse.parse_qs(query)
    mode = (q["dp"][0] if "dp" in q else dconf.get("datapath") or "blkback").lower()
    return dconf, pool, ns, image, snap, mode


# ---- tapdisk (tap-ctl) helpers ----
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
        out = subprocess.check_output([TAP_CTL, "list"], stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError:
        return None, None
    for line in out.splitlines():
        if ("args=aio:%s" % dev) in line:
            f = dict(kv.split("=", 1) for kv in line.split() if "=" in kv)
            return f.get("pid"), f.get("minor")
    return None, None


def _tap_destroy(pid, minor):
    subprocess.check_call([TAP_CTL, "destroy", "-p", str(pid), "-m", str(minor)])


class Implementation(xapi.storage.api.v5.datapath.Datapath_skeleton):

    def attach(self, dbg, uri, domain):
        dconf, pool, ns, image, snap, mode = _resolve(uri)
        read_only = snap is not None                 # snapshots are read-only
        dev = rbd_sysfs.map_image(
            dconf["mon_host"], dconf.get("user", "admin"), dconf["key"],
            pool, image, snap=snap, ms_mode=dconf.get("ms_mode", "prefer-crc"),
            read_only=read_only, namespace=ns)
        if mode == "tapdisk":
            cbtdev = None
            if not read_only:            # track writes to the live base via -C
                md = _cbt_meta(dconf, pool, ns, image)
                try:
                    cbtdev = cbtlog.attach_live(dconf, pool, ns, image, md)
                except Exception as e:
                    log.debug("%s: cbt live map failed: %s" % (dbg, e))
            tapdev = _tap_create(dev, read_only=read_only, cbtlog_dev=cbtdev)
            log.debug("%s: Datapath.attach %s -> tapdisk %s over %s%s"
                      % (dbg, uri, tapdev, dev, " +cbt" if cbtdev else ""))
            return {"implementations": [
                ["XenDisk", {"backend_type": "vbd3", "params": tapdev, "extra": {}}],
                ["BlockDevice", {"path": tapdev}],
            ]}
        log.debug("%s: Datapath.attach %s -> %s (blkback, ro=%s)" % (dbg, uri, dev, read_only))
        return {"implementations": [
            ["XenDisk", {"backend_type": "vbd", "params": dev, "extra": {}}],
            ["BlockDevice", {"path": dev}],
        ]}

    def detach(self, dbg, uri, domain):
        dconf, pool, ns, image, snap, mode = _resolve(uri)
        if mode == "tapdisk":
            dev = rbd_sysfs.find_device(pool, image, snap=snap, namespace=ns)
            if dev:
                pid, minor = _tap_find(dev)
                if pid is not None:
                    try:
                        _tap_destroy(pid, minor)
                    except Exception as e:
                        log.debug("%s: tap-ctl destroy failed: %s" % (dbg, e))
        try:
            rbd_sysfs.unmap_image(pool, image, snap=snap, namespace=ns)
        except rbd_sysfs.RbdMapError as e:
            log.debug("%s: Datapath.detach unmap warning: %s" % (dbg, e))
        if mode == "tapdisk" and snap is None:        # unmap the live cbtlog too
            try:
                cbtlog.detach_live(pool, ns, image, _cbt_meta(dconf, pool, ns, image))
            except Exception as e:
                log.debug("%s: cbt live unmap warning: %s" % (dbg, e))

    # Device is live for the whole attach/detach bracket; per-domain
    # activate/deactivate are no-ops for both modes.
    def activate(self, dbg, uri, domain):
        return None

    def activate_readonly(self, dbg, uri, domain):
        return None

    def deactivate(self, dbg, uri, domain):
        return None

    def open(self, dbg, uri, persistent):
        return None

    def close(self, dbg, uri):
        return None


if __name__ == "__main__":
    log.log_call_argv()
    cmd = xapi.storage.api.v5.datapath.Datapath_commandline(Implementation())
    base = os.path.basename(sys.argv[0])
    dispatch = {
        "Datapath.attach": cmd.attach, "Datapath.detach": cmd.detach,
        "Datapath.activate": cmd.activate,
        "Datapath.activate_readonly": cmd.activate_readonly,
        "Datapath.deactivate": cmd.deactivate,
        "Datapath.open": cmd.open, "Datapath.close": cmd.close,
    }
    if base in dispatch:
        dispatch[base]()
    else:
        raise xapi.storage.api.v5.datapath.Unimplemented(base)
