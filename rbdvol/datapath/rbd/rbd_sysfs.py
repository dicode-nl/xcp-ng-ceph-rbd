"""
rbd_sysfs.py - datapath for the Ceph RBD SR: map/unmap RBD images to kernel
block devices directly via /sys/bus/rbd, using our backported krbd (aes256k /
msgr2-secure). No `rbd` userspace binary and no ceph-common udev rules are
required: we write the map spec to sysfs ourselves and resolve /dev/rbdN from
/sys/bus/rbd/devices/.

The cephx key comes straight from SR device-config and is passed as the
`secret=<base64>` map option (libceph Opt_secret -> ceph_crypto_key_unarmor;
verified in our backported net/ceph/ceph_common.c). No keyfile on dom0.

python 3.6+, stdlib only.
"""

import os
import time

try:
    import util
    def _log(msg):
        util.SMlog("[rbd_sysfs] " + msg)
except Exception:  # pragma: no cover
    def _log(msg):
        pass

# Overridable for off-box unit tests.
SYSBUS = "/sys/bus/rbd"
DEV_PREFIX = "/dev/rbd"


class RbdMapError(Exception):
    pass


def _single_major():
    return os.path.exists(os.path.join(SYSBUS, "add_single_major"))


def _add_path():
    return os.path.join(SYSBUS, "add_single_major" if _single_major() else "add")


def _remove_path():
    return os.path.join(SYSBUS, "remove_single_major" if _single_major() else "remove")


def _devices_dir():
    return os.path.join(SYSBUS, "devices")


def _read(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except (IOError, OSError):
        return None


def _mon_default_port(ms_mode):
    # msgr2 (v2) listens on 3300 and carries secure/crc; only 'legacy' is msgr1.
    return "6789" if (ms_mode or "").lower() == "legacy" else "3300"


def _normalize_mons(mon_host, ms_mode):
    """Split the mon list and fill in the right default port when none is given.
    Prevents the classic mistake of hitting the msgr1 port (6789) while asking
    for ms_mode=secure (which needs the msgr2 port, 3300)."""
    port = _mon_default_port(ms_mode)
    out = []
    for m in mon_host.replace(";", ",").split(","):
        m = m.strip()
        if not m:
            continue
        if m.startswith("["):               # [ipv6] or [ipv6]:port
            out.append(m if "]:" in m else "%s:%s" % (m, port))
        elif m.count(":") == 1:             # host:port (ipv4)
            out.append(m)
        elif ":" in m:                      # bare ipv6
            out.append("[%s]:%s" % (m, port))
        else:                               # bare ipv4/hostname
            out.append("%s:%s" % (m, port))
    if not out:
        raise RbdMapError("no mon addresses given")
    return ",".join(out)


def _build_spec(mon_host, user, secret, pool, image, snap=None,
                ms_mode="prefer-crc", read_only=False, extra_opts=None, namespace=None):
    """Build the krbd sysfs 'add' line: '<mons> <options> <pool> <image> [<snap>]'.
    Namespaced images use the krbd '_pool_ns=<ns>' option (our rbd.c Opt_pool_ns)."""
    mons = _normalize_mons(mon_host, ms_mode)
    opts = ["name=%s" % user, "secret=%s" % secret]
    if namespace:
        opts.append("_pool_ns=%s" % namespace)
    if ms_mode:
        opts.append("ms_mode=%s" % ms_mode)
    if read_only:
        opts.append("ro")
    if extra_opts:
        if isinstance(extra_opts, str):
            opts.extend(o for o in extra_opts.split(",") if o)
        else:
            opts.extend(extra_opts)
    spec = "%s %s %s %s" % (mons, ",".join(opts), pool, image)
    if snap:
        spec += " " + snap
    return spec


def _iter_devices():
    """Yield (dev_id, {pool, name, snap, major, minor}) for each mapped rbd."""
    ddir = _devices_dir()
    try:
        ids = os.listdir(ddir)
    except (IOError, OSError):
        return
    for dev_id in ids:
        base = os.path.join(ddir, dev_id)
        snap = _read(os.path.join(base, "current_snap"))
        ns = _read(os.path.join(base, "pool_ns"))
        yield dev_id, {
            "pool": _read(os.path.join(base, "pool")),
            "name": _read(os.path.join(base, "name")),
            "snap": None if snap in (None, "-") else snap,
            "namespace": None if ns in (None, "") else ns,
            "major": _read(os.path.join(base, "major")),
            "minor": _read(os.path.join(base, "minor")),
        }


def find_device(pool, image, snap=None, namespace=None):
    """Return /dev/rbdN for an already-mapped (pool,image[,snap][,ns]) or None."""
    ns = namespace or None
    for dev_id, info in _iter_devices():
        if (info["pool"] == pool and info["name"] == image
                and info["snap"] == snap and info["namespace"] == ns):
            return DEV_PREFIX + dev_id
    return None


def _device_for_id(dev_id):
    dev = DEV_PREFIX + dev_id
    for _ in range(50):  # devtmpfs node usually immediate; brief grace for udev-less
        if os.path.exists(dev):
            return dev
        time.sleep(0.1)
    raise RbdMapError("device node %s did not appear after map" % dev)


def map_image(mon_host, user, secret, pool, image, snap=None,
              ms_mode="prefer-crc", read_only=False, extra_opts=None, namespace=None):
    """Map an RBD image/snapshot; return its /dev/rbdN. Idempotent."""
    existing = find_device(pool, image, snap, namespace)
    if existing:
        _log("already mapped %s/%s/%s@%s -> %s" % (pool, namespace or "", image, snap, existing))
        return existing

    if snap and not read_only:
        read_only = True  # snapshots are inherently read-only
    spec = _build_spec(mon_host, user, secret, pool, image, snap,
                       ms_mode=ms_mode, read_only=read_only, extra_opts=extra_opts,
                       namespace=namespace)
    safe = spec.replace(secret, "<secret>")
    _log("map: %s" % safe)
    try:
        with open(_add_path(), "w") as f:
            f.write(spec)
    except (IOError, OSError) as e:
        raise RbdMapError("failed to write rbd add for %s/%s: %s" % (pool, image, e))

    dev = find_device(pool, image, snap, namespace)
    if not dev:
        raise RbdMapError("map of %s/%s succeeded but device not found in sysfs" % (pool, image))
    dev_id = dev[len(DEV_PREFIX):]
    return _device_for_id(dev_id)


def unmap_device(device, force=False):
    """Unmap by /dev/rbdN path or numeric id."""
    dev_id = device[len(DEV_PREFIX):] if str(device).startswith(DEV_PREFIX) else str(device)
    val = dev_id + (" force" if force else "")
    _log("unmap id=%s force=%s" % (dev_id, force))
    try:
        with open(_remove_path(), "w") as f:
            f.write(val)
    except (IOError, OSError) as e:
        raise RbdMapError("failed to unmap rbd id %s: %s" % (dev_id, e))


def unmap_image(pool, image, snap=None, force=False, namespace=None):
    dev = find_device(pool, image, snap, namespace)
    if not dev:
        _log("unmap_image: %s/%s@%s not mapped (noop)" % (pool, image, snap))
        return False
    unmap_device(dev, force=force)
    return True
