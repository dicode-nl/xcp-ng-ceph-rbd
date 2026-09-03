#!/usr/bin/python3
#
# datapath.py - Datapath.* + DATA.* for the rbd-vol datapath (scheme "rbd://").
#
# Maps the RBD image via native krbd (/sys/bus/rbd) to /dev/rbdN, then serves it
# in one of three modes (chosen by device-config `datapath=` or a URI `?dp=`
# query; default blkback). The three are serve MODES of the one rbd datapath,
# not separate datapaths -- the underlying storage access is always rbd:
#   * blkback  -> hand xenopsd the raw /dev/rbdN (backend_type "vbd"): kernel
#                 blkback, tapdisk-less, max performance. SXM source via an
#                 iterative rbd-diff pre-copy (dicode.libs.blkmirror); SXM dest
#                 via a lazily-started qemu-nbd (dicode.libs.blknbd).
#   * tapdisk  -> a userspace tapdisk (backend_type "vbd3"): unlocks CBT and the
#                 tapdisk NBD mirror (dicode.libs.dp_tapdisk).
#   * qemu     -> a per-VDI qemu-storage-daemon behind blkback (dicode.libs.
#                 dp_qemu): race-free live SXM via blockdev-mirror.
# The mode-specific serve/detach/mirror logic lives in the dicode.libs.dp_*
# helpers; this file just maps the device and dispatches on the mode.
#
# URI: rbd://<sr_uuid>/<key>[?dp=blkback|tapdisk|qemu]  (key="<image>"|"<base>@<snap>")
# Config (mon_host/user/cephx key/ms_mode/pool/namespace/datapath) comes from the
# per-host SR metadata store written by SR.attach -- secrets stay out of the URI.

import os
import sys
import urllib.parse

import xapi.storage.api.v5.datapath
from xapi.storage import log

from dicode.libs import srmeta
from dicode.libs import rbd_sysfs
from dicode.libs import dp_blkback
from dicode.libs import dp_tapdisk
from dicode.libs import dp_qemu
from dicode.libs import blkmirror


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


class Implementation(xapi.storage.api.v5.datapath.Datapath_skeleton):

    def attach(self, dbg, uri, domain):
        dconf, pool, ns, image, snap, mode = _resolve(uri)
        read_only = snap is not None                 # snapshots are read-only
        dev = rbd_sysfs.map_image(
            dconf["mon_host"], dconf.get("user", "admin"), dconf["key"],
            pool, image, snap=snap, ms_mode=dconf.get("ms_mode", "prefer-crc"),
            read_only=read_only, namespace=ns)
        if mode == "tapdisk":
            impls = dp_tapdisk.attach(dbg, dconf, pool, ns, image, snap, dev)
        elif mode == "qemu":
            impls = dp_qemu.attach(dbg, image, snap, dev)
        else:
            impls = dp_blkback.attach(dbg, image, snap, dev, domain)
        return {"implementations": impls}

    def detach(self, dbg, uri, domain):
        dconf, pool, ns, image, snap, mode = _resolve(uri)
        if mode == "tapdisk":
            dp_tapdisk.detach(dbg, dconf, pool, ns, image, snap)
        elif mode == "qemu":
            dp_qemu.detach(dbg, image)
        else:
            # A blkback SXM source parks its pre-copy mirror on the image; this
            # detach is the cutover window (measured guest-paused, pre-dest-
            # resume). Ship the final delta if the pre-copy converged, else tear
            # down a failed/aborted migration. MUST run before the unmap below
            # (the final delta reads source snapshots; the base map is the
            # guest's and stays busy). Idempotent: only the first such detach
            # finalizes.
            if blkmirror.active(image):
                st = blkmirror.read_status(image)
                if st.get("complete") and not st.get("failed"):
                    blkmirror.finalize(dbg, dconf, pool, ns, image)
                else:
                    blkmirror.cancel(dbg, dconf, pool, ns, image)
            dp_blkback.detach(dbg, image)
        try:
            rbd_sysfs.unmap_image(pool, image, snap=snap, namespace=ns)
        except rbd_sysfs.RbdMapError as e:
            log.debug("%s: Datapath.detach unmap warning: %s" % (dbg, e))

    # The device is live for the whole attach/detach bracket; per-domain
    # activate/deactivate are no-ops for every mode.
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

    # ---- SXM destination hook (declared via VDI_MIRROR_IN) ----
    def import_activate(self, dbg, uri, domain):
        """Fd-passing NBD-server hook. Unused by the qemu mode (its SXM receive
        path uses DATA.get_nbd_server); retained as a stub for a future tapdisk
        fd-receive implementation."""
        raise xapi.storage.api.v5.datapath.Unimplemented(
            "import_activate (use DATA.get_nbd_server)")


class DataImplementation(xapi.storage.api.v5.datapath.Data_skeleton):
    """DATA.* SXM ops, dispatched to the mode helper. get_nbd_server + mirror
    resolve the mode from the URI; stat dispatches on the handle shape (qemu
    handles are "<image>|<job>", tapdisk handles are "<pid>.<minor>")."""

    def get_nbd_server(self, dbg, uri, domain):
        _dconf, pool, ns, image, snap, mode = _resolve(uri)
        if mode == "qemu":
            return dp_qemu.get_nbd_server(dbg, image)
        if mode == "tapdisk":
            return dp_tapdisk.get_nbd_server(dbg, pool, ns, image, snap)
        return dp_blkback.get_nbd_server(dbg, pool, ns, image, snap)

    def mirror(self, dbg, uri, domain, remote):
        dconf, pool, ns, image, snap, mode = _resolve(uri)
        if mode == "qemu":
            return dp_qemu.mirror(dbg, image, remote)
        if mode == "blkback":
            # blkback can't tee live writes, so instead of a qemu-style base-copy
            # + tee we run an iterative rbd-diff pre-copy (converge, then a final
            # delta in the paused cutover at Datapath.detach). See blkmirror.
            return blkmirror.mirror(dbg, dconf, pool, ns, image, remote)
        # The tapdisk fd-passing mirror tees NEW writes but NOT the base, so it
        # would silently lose pre-existing data -- deliberately NOT wired
        # (dp_tapdisk.mirror stays as unwired WIP). Fail loudly rather than
        # corrupt.
        raise xapi.storage.api.v5.datapath.Unimplemented(
            "DATA.mirror (SXM source) not supported for the %s datapath mode"
            % mode)

    def stat(self, dbg, operation):
        key = operation[1] if isinstance(operation, (list, tuple)) else operation
        if key.startswith(blkmirror.HANDLE_PREFIX):  # "blkmir:<image>"
            return blkmirror.stat(dbg, key)
        if "|" in key:                               # qemu handle "<image>|<job>"
            return dp_qemu.stat(dbg, key)
        return dp_tapdisk.stat(dbg, key)             # tapdisk "<pid>.<minor>"


def _read_dbg_uri_domain():
    """Parse (dbg, uri, domain) for DATA.get_nbd_server (no v5 commandline
    helper), honouring the --json/-j stdin convention."""
    import json
    if "--json" in sys.argv or "-j" in sys.argv:
        d = json.loads(sys.stdin.readline())
        return d["dbg"], d["uri"], d["domain"]
    args = [a for a in sys.argv[1:] if a not in ("-j", "--json")]
    return args[0], args[1], args[2]


if __name__ == "__main__":
    import json
    log.log_call_argv()
    base = os.path.basename(sys.argv[0])
    if base.startswith("DATA."):
        impl = DataImplementation()
        if base == "DATA.get_nbd_server":
            dbg, uri, domain = _read_dbg_uri_domain()
            print(json.dumps(impl.get_nbd_server(dbg, uri, domain)))
        else:
            dcmd = xapi.storage.api.v5.datapath.Data_commandline(impl)
            ddispatch = {"DATA.mirror": dcmd.mirror, "DATA.stat": dcmd.stat}
            if base in ddispatch:
                ddispatch[base]()
            else:
                raise xapi.storage.api.v5.datapath.Unimplemented(base)
    else:
        cmd = xapi.storage.api.v5.datapath.Datapath_commandline(Implementation())
        dispatch = {
            "Datapath.attach": cmd.attach, "Datapath.detach": cmd.detach,
            "Datapath.activate": cmd.activate,
            "Datapath.activate_readonly": cmd.activate_readonly,
            "Datapath.deactivate": cmd.deactivate,
            "Datapath.open": cmd.open, "Datapath.close": cmd.close,
            "Datapath.import_activate": cmd.import_activate,
        }
        if base in dispatch:
            dispatch[base]()
        else:
            raise xapi.storage.api.v5.datapath.Unimplemented(base)
