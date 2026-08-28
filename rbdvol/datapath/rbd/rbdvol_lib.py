"""
rbdvol_lib.py - shared helpers for the rbd-vol SMAPIv3 volume plugin.

Data model (same as the SMAPIv1 driver): namespace == SR uuid, image == VDI uuid,
snapshot == snapshot-VDI uuid stored as <base_image>@<snap_uuid>.

Volume "key" (the storage locator xapi hands back to us):
  - base VDI  : "<image>"            (== the rbd image name == vdi uuid)
  - snapshot  : "<base_image>@<snap_uuid>"   (so we can locate base+snap w/o a DB)

Volume "uri" (selects the datapath, secrets excluded -- datapath reads srmeta):
  rbd://<sr_uuid>/<key>

python3, stdlib only.
"""

import re

from rbd_backend import make_backend

DATAPATH_SCHEME = "rbd"
UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


# rbd_features presets (device-config:rbd_features). 'layering' is always enforced
# (clones need it). 'performance' needs a krbd that maps object-map/fast-diff (5.3+);
# 'compat' maps on older/stock clients (4.9+) but is slower and has no deep-flatten
# (GC then defers, see rbd_gc.py). An explicit comma-list is an advanced override.
RBD_FEATURE_PRESETS = {
    "performance": ["layering", "exclusive-lock", "object-map", "fast-diff", "deep-flatten"],
    "compat": ["layering", "exclusive-lock"],
}


def resolve_features(value):
    v = (value or "performance").strip()
    preset = RBD_FEATURE_PRESETS.get(v.lower())
    if preset is not None:
        return list(preset)
    feats = [f.strip() for f in v.split(",") if f.strip()]
    if "layering" not in feats:
        feats.insert(0, "layering")
    return feats


def backend(meta):
    return make_backend(meta["dconf"])


def pool_ns(meta):
    dconf = meta["dconf"]
    return dconf["pool"], (dconf.get("namespace") or meta["sr_uuid"])


def is_snapshot_key(key):
    return "@" in key


def split_key(key):
    """(base_image, snap_or_None) for a volume key."""
    if "@" in key:
        base, snap = key.split("@", 1)
        return base, snap
    return key, None


def snap_key(base_image, snap_uuid):
    return "%s@%s" % (base_image, snap_uuid)


def volume_uri(sr_uuid, key):
    return "%s://%s/%s" % (DATAPATH_SCHEME, sr_uuid, key)


# ---- VDI name/description/custom-keys, backed by rbd image-meta on the base
# image. RBD snapshots have no metadata of their own, so a snapshot's fields are
# stored on the base image under a "snap.<uuid>." prefix. ----
def meta_names(snap):
    """(name_key, desc_key, custom_key_prefix) within the base image's metadata."""
    b = "vdi" if snap is None else "snap.%s" % snap
    return b + ".name", b + ".desc", b + ".k."


def snap_meta_prefix(snap):
    """Common prefix of every base-image meta key owned by snapshot <snap>
    (name/desc/custom keys/cbt.*). Used to purge them when the snapshot is
    destroyed so orphan keys don't accumulate on the base image."""
    return "snap.%s." % snap


_SNAP_META_RE = re.compile(r"^snap\.([0-9a-f-]{36})\.", re.I)


def orphan_snap_meta_keys(metadata, live_snap_names):
    """Base-image meta keys of the form 'snap.<uuid>.*' whose <uuid> is not a
    currently-existing snapshot -- i.e. cruft a failed/partial destroy (or an
    out-of-band snap removal) left behind. SR.ls prunes these opportunistically
    so the base image's metadata self-heals instead of growing without bound."""
    live = set(live_snap_names or [])
    out = []
    for k in (metadata or {}):
        m = _SNAP_META_RE.match(k)
        if m and UUID_RE.match(m.group(1)) and m.group(1) not in live:
            out.append(k)
    return out


def meta_view(metadata, snap, uuid):
    """Extract (name, description, keys) for a logical volume from a base image's
    metadata dict."""
    nk, dk, kp = meta_names(snap)
    m = metadata or {}
    # A snapshot with no explicit name of its own inherits the base image's
    # name (as xapi does on VDI.snapshot). Without this fallback a re-scan would
    # rename the snapshot VDI to its bare uuid.
    base_nk = meta_names(None)[0]  # "vdi.name"
    fallback = m.get(base_nk) if snap is not None else None
    name = m.get(nk) or fallback or uuid
    desc = m.get(dk, "")
    keys = {k[len(kp):]: v for k, v in m.items() if k.startswith(kp)}
    return name, desc, keys


# ---- CBT: changed-block bitmap ----
CBT_BLOCK = 65536  # 64 KiB, the XAPI / NBD changed-block-tracking granularity


def changed_bitmap(diffs, offset, length, block=CBT_BLOCK):
    """Turn rbd changed extents ([{offset,length,exists}, ...]) into the base64
    bitmap that Volume.list_changed_blocks returns: one bit per <block> bytes
    over [offset, offset+length), set when that block changed.

    Bit order is MSB-first within each byte (block b -> byte b//8, bit
    0x80>>(b%8)) -- the XenServer/XAPI CBT convention. NB: validate end-to-end
    against a real XO delta backup; if the delta looks byte-reversed, this single
    shift is the only thing to flip.
    """
    import base64
    length = int(length or 0)
    nblocks = (length + block - 1) // block
    bits = bytearray((nblocks + 7) // 8)
    end = offset + length
    for d in diffs or []:
        if not d.get("exists"):
            continue
        s = max(int(d["offset"]), offset)
        e = min(int(d["offset"]) + int(d["length"]), end)
        if e <= s:
            continue
        for b in range((s - offset) // block, (e - 1 - offset) // block + 1):
            bits[b // 8] |= 0x80 >> (b % 8)
    return base64.b64encode(bytes(bits)).decode("ascii")


def volume_dict(sr_uuid, key, uuid, name, description, size,
                physical_utilisation=0, read_write=True, sharable=False, keys=None,
                cbt_enabled=False):
    """The SMAPIv3 volume record returned by create/stat/ls/snapshot/clone.

    cbt_enabled is read by the xapi-storage-script bridge into the VDI's
    cbt_enabled field; xapi's VDI.data_destroy pre-check (and XO's CBT backup)
    require a snapshot to report it, so a snapshot of a CBT-enabled base must
    carry it through."""
    return {
        "key": key,
        "uuid": uuid,
        "name": name if name is not None else uuid,
        "description": description or "",
        "read_write": bool(read_write),
        "virtual_size": int(size),
        "physical_utilisation": int(physical_utilisation or 0),
        "uri": [volume_uri(sr_uuid, key)],
        "sharable": bool(sharable),
        "keys": keys or {},
        "cbt_enabled": bool(cbt_enabled),
    }


def cbt_is_on(metadata):
    """Whether CBT is enabled on the base image (enable_cbt marks 'cbt.enabled').
    Snapshots inherit the base's flag -- once CBT is on, snapshots are CBT
    reference points."""
    return bool((metadata or {}).get("cbt.enabled"))
