"""
srmeta.py - per-host SR metadata store for the rbd-vol SMAPIv3 plugin.

SMAPIv3 volume methods only receive the SR *handle* (a path string), not the SR
device-config. SR.attach stashes the device-config here (keyed by SR uuid) so
that later Volume/SR calls -- and the datapath plugin -- can recover pool,
namespace, mon_host, the cephx key and the dashboard creds from just the handle.

Transient: lives under /var/run (tmpfs), re-written on every SR.attach (xapi
supplies the full config at each attach). Keeping the config here also keeps the
cephx key OUT of the volume URIs that xapi stores/logs.

python3, stdlib only.
"""

import json
import os

ROOT = "/var/run/rbd-vol"


def sr_dir(sr_uuid):
    return os.path.join(ROOT, sr_uuid)


def handle_uuid(sr):
    """The SR handle we return from SR.attach is the sr_dir path; accept either
    that path or a bare uuid and return the uuid."""
    return os.path.basename(sr.rstrip("/"))


def write(sr_uuid, meta):
    """Persist meta (a dict) for sr_uuid; return the handle path."""
    d = sr_dir(sr_uuid)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError:
        pass
    tmp = os.path.join(d, ".meta.json.tmp")
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, os.path.join(d, "meta.json"))
    return d


def read(sr):
    """Load meta for an SR handle (path) or bare uuid."""
    p = sr if os.path.isdir(sr) else sr_dir(sr)
    with open(os.path.join(p, "meta.json")) as f:
        return json.load(f)


def remove(sr):
    p = sr if os.path.isdir(sr) else sr_dir(sr)
    try:
        os.unlink(os.path.join(p, "meta.json"))
    except OSError:
        pass
    try:
        os.rmdir(p)
    except OSError:
        pass
