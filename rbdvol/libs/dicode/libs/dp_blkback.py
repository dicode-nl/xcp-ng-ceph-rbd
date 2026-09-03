#!/usr/bin/python3
#
# dp_blkback.py - the "blkback" serve mode for the rbd datapath.
#
# Hands xenopsd the raw mapped /dev/rbdN (backend_type "vbd") so the kernel
# blkback serves the guest directly -- no userspace process in the data path,
# maximum performance. To also be an SXM DESTINATION, a qemu-nbd over /dev/rbdN
# provides a writable NBD (dicode.libs.blknbd), started LAZILY in get_nbd_server
# so the normal guest attach stays pure blkback. blkback cannot TEE live writes,
# so it is an SXM source via an iterative rbd-diff pre-copy instead of a live
# mirror (dicode.libs.blkmirror, wired in DATA.mirror + the paused final delta in
# Datapath.detach) -- not this file.

from xapi.storage import log

from dicode.libs import blknbd
from dicode.libs import rbd_sysfs


def _is_sxm_domain(domain):
    """xapi's get_nbd_server (bridge main.ml) provides the SXM NBD server by
    RE-ATTACHING with domain = the raw mirror VM string ("MIR<hex>"), and
    receive_start3 attaches the dest with the same. A normal guest VBD plug
    attaches with domain = the numeric domid. So a non-numeric domain marks the
    SXM-receive attach -- the only case that needs an NBD server. (dom0 also maps
    to a non-numeric "u0-<dp>", which for blkback only occurs for such transfers.)"""
    return not str(domain).isdigit()


def attach(dbg, image, snap, dev, domain):
    # Pure kernel blkback for a guest: hand xenopsd the raw /dev/rbdN (BlockDevice
    # wins in params_of_backend -> type=phy -> kernel blkback, no userspace
    # process). The bridge's get_nbd_server does NOT call our DATA.get_nbd_server
    # script; it re-attaches and reads the Nbd uri from THIS response, then
    # connects to that socket -- so for an SXM RECEIVE the qemu-nbd must be LIVE
    # here and the Nbd advertised (also needed by receive_start3's
    # nbd_export_of_attach_info, else "Cannot parse nbd uri"). We start it ONLY for
    # the SXM (mirror-VM) attach, so guest attaches stay a pure kernel datapath.
    impls = [
        ["XenDisk", {"backend_type": "vbd", "params": dev, "extra": {}}],
        ["BlockDevice", {"path": dev}],
    ]
    if _is_sxm_domain(domain):
        sock = blknbd.serve(dbg, image, dev, read_only=(snap is not None))
        impls.append(["Nbd", {"uri": "nbd:unix:%s:exportname=%s"
                              % (sock, blknbd.export(image))}])
        log.debug("%s: attach %s -> %s (blkback SXM-dest dom=%s, nbd %s)"
                  % (dbg, image, dev, domain, sock))
    else:
        log.debug("%s: attach %s -> %s (blkback guest dom=%s)"
                  % (dbg, image, dev, domain))
    return impls


def detach(dbg, image):
    # Stop the receive qemu-nbd if one was started (must run before the caller
    # unmaps /dev/rbdN); a no-op for the common (non-SXM) attach.
    blknbd.stop(dbg, image)


def get_nbd_server(dbg, pool, ns, image, snap):
    """SXM dest: start (lazily) and return a qemu-nbd over the mapped /dev/rbdN,
    which xapi's import_nbd_proxy proxies the incoming mirror stream into."""
    dev = rbd_sysfs.find_device(pool, image, snap=snap, namespace=ns)
    if not dev:
        raise Exception("get_nbd_server: %s not mapped" % image)
    return blknbd.serve(dbg, image, dev, read_only=(snap is not None))
