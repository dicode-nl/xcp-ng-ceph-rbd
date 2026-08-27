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


def meta_view(metadata, snap, uuid):
    """Extract (name, description, keys) for a logical volume from a base image's
    metadata dict."""
    nk, dk, kp = meta_names(snap)
    m = metadata or {}
    name = m.get(nk) or uuid
    desc = m.get(dk, "")
    keys = {k[len(kp):]: v for k, v in m.items() if k.startswith(kp)}
    return name, desc, keys


def volume_dict(sr_uuid, key, uuid, name, description, size,
                physical_utilisation=0, read_write=True, sharable=False, keys=None):
    """The SMAPIv3 volume record returned by create/stat/ls/snapshot/clone."""
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
    }
