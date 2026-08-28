"""
cbtlog.py - changed-block tracking over RBD via tapdisk + cbt-util.

The self-contained CBT route (no ceph-mgr dashboard /diff patch, no rbd
fast-diff/object-map needed) for the tapdisk datapath. Every CBT-enabled base
image gets small companion RBD images holding cbt-util logs (a ~48-byte header
plus a 1-bit-per-64KiB-block bitmap); tapdisk's `-C` log layer sets a bit for
every written block. On snapshot the current live log is sealed to that
snapshot and a fresh live log starts; list_changed_blocks OR-coalesces the
sealed logs.

Companion images live in the SR pool+namespace, named "cbtlog.<base>.<gen>"
(non-uuid, so SR.ls skips them). The mapping lives in the base image's rbd
image-meta:
    cbt.enabled = "1"
    cbt.mode    = "tapdisk"
    cbt.live    = <gen>                # current live companion
    cbt.snap.<snap_uuid> = <gen>       # sealed companion for that snapshot

cbt-util's on-disk bitmap already uses the XenServer/XAPI bit order (block b ->
byte b//8, bit 0x80>>(b%8)), so a coalesced bitmap is returned to xapi verbatim.

Runs on dom0 (krbd map + cbt-util + tap-ctl are all host-side); imported by both
the volume plugin (enable/snapshot/list) and the datapath plugin (attach -C).
python3, stdlib + rbd_sysfs only.
"""

import base64
import subprocess
import uuid

import rbd_sysfs

CBT_BLOCK = 65536                 # 64 KiB, the XAPI CBT granularity
_HEADER = 64                      # cbt-util header (48 real), rounded for margin
CBT_UTIL = "/usr/sbin/cbt-util"
TAP_CTL = "/usr/sbin/tap-ctl"
COMPANION_PREFIX = "cbtlog."      # non-uuid name -> SR.ls skips these images
MODE_TAPDISK = "tapdisk"

K_ENABLED = "cbt.enabled"
K_MODE = "cbt.mode"
K_LIVE = "cbt.live"
_SNAP_PREFIX = "cbt.snap."


class CbtError(Exception):
    pass


# ---- naming / sizing ----
def _gen():
    return uuid.uuid4().hex[:12]


def companion_name(base, gen):
    return "%s%s.%s" % (COMPANION_PREFIX, base, gen)


def is_companion(name):
    return bool(name) and name.startswith(COMPANION_PREFIX)


def num_blocks(size):
    return (int(size) + CBT_BLOCK - 1) // CBT_BLOCK


def _bitmap_bytes(size):
    return (num_blocks(size) + 7) // 8


def companion_size(size):
    """RBD image size to hold one cbtlog for a `size`-byte disk (4KiB-aligned)."""
    raw = _HEADER + _bitmap_bytes(size)
    return ((raw + 4095) // 4096) * 4096


# ---- meta helpers ----
def is_enabled(meta):
    return bool((meta or {}).get(K_ENABLED))


def mode_of(meta):
    return (meta or {}).get(K_MODE)


def is_tapdisk(meta):
    return is_enabled(meta) and mode_of(meta) == MODE_TAPDISK


def live_gen(meta):
    return (meta or {}).get(K_LIVE)


def snap_key(snap):
    return _SNAP_PREFIX + snap


def sealed_gen(meta, snap):
    return (meta or {}).get(snap_key(snap))


# ---- krbd + cbt-util + tap-ctl primitives ----
def _map(dconf, pool, ns, image, read_only=False):
    return rbd_sysfs.map_image(
        dconf["mon_host"], dconf.get("user", "admin"), dconf["key"],
        pool, image, ms_mode=dconf.get("ms_mode", "prefer-crc"),
        read_only=read_only, namespace=ns)


def _unmap(pool, ns, image):
    try:
        rbd_sysfs.unmap_image(pool, image, namespace=ns)
    except Exception:
        pass


def _cbt(*args):
    subprocess.check_call([CBT_UTIL] + list(args), stdout=subprocess.DEVNULL,
                          stderr=subprocess.STDOUT)


def _cbt_bitmap(dev):
    return bytearray(subprocess.check_output([CBT_UTIL, "get", "-n", dev, "-b"]))


def _tap_find(data_dev):
    """(pid, minor) of a running tapdisk whose aio target is data_dev, or (None, None)."""
    try:
        out = subprocess.check_output([TAP_CTL, "list"],
                                      stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError:
        return None, None
    for line in out.splitlines():
        if ("args=aio:%s" % data_dev) in line:
            f = dict(kv.split("=", 1) for kv in line.split() if "=" in kv)
            return f.get("pid"), f.get("minor")
    return None, None


# ---- companion lifecycle ----
def create_companion(be, dconf, pool, ns, base, size):
    """Create + zero-init a fresh live companion for `base`; return its gen id."""
    gen = _gen()
    name = companion_name(base, gen)
    be.create(pool, name, companion_size(size), ["layering"], namespace=ns)
    dev = _map(dconf, pool, ns, name)
    try:
        _cbt("create", "-n", dev, "-s", str(num_blocks(size)))
        _cbt("set", "-n", dev, "-s", str(int(size)), "-f", "1")
    finally:
        _unmap(pool, ns, name)
    return gen


def remove_companion(be, dconf, pool, ns, base, gen):
    if not gen:
        return
    name = companion_name(base, gen)
    _unmap(pool, ns, name)
    try:
        be.remove(pool, name, namespace=ns)
    except Exception:
        pass


def remove_all_companions(be, dconf, pool, ns, base, meta):
    """Drop the live + every sealed companion (disable_cbt / VDI destroy)."""
    for k, v in list((meta or {}).items()):
        if k == K_LIVE or k.startswith(_SNAP_PREFIX):
            remove_companion(be, dconf, pool, ns, base, v)


# ---- datapath side: map the live log for tap-ctl -C ----
def attach_live(dconf, pool, ns, base, meta):
    """If CBT-tapdisk is on for `base`, map its live companion and return the
    /dev/rbdN to hand to `tap-ctl create -C`. Else None."""
    if not is_tapdisk(meta):
        return None
    gen = live_gen(meta)
    if not gen:
        return None
    return _map(dconf, pool, ns, companion_name(base, gen))


def detach_live(pool, ns, base, meta):
    gen = live_gen(meta)
    if gen:
        _unmap(pool, ns, companion_name(base, gen))


# ---- enable / inject into a running tapdisk ----
def inject_running(dconf, pool, ns, base, gen):
    """If a tapdisk is already running for `base` (no -C yet), pause it and
    unpause with the live companion as its log layer. No-op if not running."""
    data_dev = rbd_sysfs.find_device(pool, base, namespace=ns)
    if not data_dev:
        return False
    pid, minor = _tap_find(data_dev)
    if not pid:
        return False
    dev = _map(dconf, pool, ns, companion_name(base, gen))
    subprocess.check_call([TAP_CTL, "pause", "-p", pid, "-m", minor])
    subprocess.check_call([TAP_CTL, "unpause", "-p", pid, "-m", minor, "-c", dev])
    return True


# ---- snapshot rotation ----
def rotate(be, dconf, pool, ns, base, snap_uuid, meta, size):
    """Seal the current live companion as `snap_uuid`'s log and start a fresh
    live one. Returns the meta updates to persist. If a tapdisk is running for
    base, live-swap its log layer to the new companion (pause/unpause -c)."""
    old_gen = live_gen(meta)
    new_gen = create_companion(be, dconf, pool, ns, base, size)
    updates = {snap_key(snap_uuid): old_gen, K_LIVE: new_gen}

    data_dev = rbd_sysfs.find_device(pool, base, namespace=ns)
    if data_dev:
        pid, minor = _tap_find(data_dev)
        if pid:
            new_dev = _map(dconf, pool, ns, companion_name(base, new_gen))
            subprocess.check_call([TAP_CTL, "pause", "-p", pid, "-m", minor])
            subprocess.check_call([TAP_CTL, "unpause", "-p", pid, "-m", minor,
                                   "-c", new_dev])
            if old_gen:                    # tapdisk released the old log device
                _unmap(pool, ns, companion_name(base, old_gen))
    return updates


# ---- list_changed_blocks ----
def _snap_time(meta, snap):
    return (meta or {}).get("snap.%s.k.snapshot_time" % snap, "") if snap else ""


def gens_between(meta, from_snap, to_snap):
    """Sealed-companion gens whose interval falls in (from_snap, to_snap],
    ordered by snapshot_time; plus the live gen when to_snap is None (== the
    current base image)."""
    sealed = []
    for k, v in (meta or {}).items():
        if k.startswith(_SNAP_PREFIX):
            su = k[len(_SNAP_PREFIX):]
            sealed.append((_snap_time(meta, su), su, v))
    sealed.sort()
    from_t = _snap_time(meta, from_snap)
    to_t = _snap_time(meta, to_snap) if to_snap else None
    gens = [g for (t, su, g) in sealed
            if t > from_t and (to_t is None or t <= to_t) and g]
    if to_snap is None and live_gen(meta):
        gens.append(live_gen(meta))
    return gens


def changed_bitmap(dconf, pool, ns, base, meta, from_snap, to_snap,
                   offset, length, size):
    """base64 changed-block bitmap over [offset, offset+length) for the interval
    (from_snap, to_snap], coalescing the relevant sealed logs. Bit order is
    cbt-util's own == the XAPI convention, so no re-encoding."""
    off = int(offset or 0)
    length = int(size) - off if (length is None or int(length) < 0) else int(length)
    nblk = num_blocks(size)
    acc = bytearray((nblk + 7) // 8)
    for gen in gens_between(meta, from_snap, to_snap):
        try:
            bm = read_bitmap(dconf, pool, ns, base, gen)
        except Exception:
            continue
        for i in range(min(len(bm), len(acc))):
            acc[i] |= bm[i]

    b0 = off // CBT_BLOCK
    b1 = (off + length + CBT_BLOCK - 1) // CBT_BLOCK
    out = bytearray((max(b1 - b0, 0) + 7) // 8)
    for b in range(b0, min(b1, nblk)):
        if acc[b // 8] & (0x80 >> (b % 8)):
            j = b - b0
            out[j // 8] |= 0x80 >> (j % 8)
    return base64.b64encode(bytes(out)).decode("ascii")


def read_bitmap(dconf, pool, ns, base, gen):
    name = companion_name(base, gen)
    dev = _map(dconf, pool, ns, name)
    try:
        return _cbt_bitmap(dev)
    finally:
        _unmap(pool, ns, name)
