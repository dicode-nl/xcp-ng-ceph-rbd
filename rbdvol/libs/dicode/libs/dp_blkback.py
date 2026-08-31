#!/usr/bin/python3
#
# dp_blkback.py - the "blkback" serve mode for the rbd datapath.
#
# Hands xenopsd the raw mapped /dev/rbdN (backend_type "vbd") so the kernel
# blkback serves the guest directly -- no userspace process in the data path,
# maximum performance. To also be an SXM DESTINATION, a qemu-nbd over /dev/rbdN
# provides a writable NBD (dicode.libs.blknbd), started LAZILY in get_nbd_server
# so the normal guest attach stays pure blkback. blkback cannot be an SXM source
# (it can't tee live writes); use the qemu mode for that.

from xapi.storage import log

from dicode.libs import blknbd
from dicode.libs import rbd_sysfs


def attach(dbg, image, snap, dev):
    log.debug("%s: attach %s -> %s (blkback)" % (dbg, image, dev))
    return [
        ["XenDisk", {"backend_type": "vbd", "params": dev, "extra": {}}],
        ["BlockDevice", {"path": dev}],
        # SXM-dest handle: receive_start3 only reads the exportname from this Nbd;
        # the actual qemu-nbd server is started lazily by get_nbd_server, so a
        # plain guest attach never spawns it. BlockDevice still wins in xenopsd's
        # params_of_backend, so the guest is served by blkback either way.
        ["Nbd", {"uri": "nbd:unix:%s:exportname=%s"
                        % (blknbd.sock(image), blknbd.export(image))}],
    ]


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
