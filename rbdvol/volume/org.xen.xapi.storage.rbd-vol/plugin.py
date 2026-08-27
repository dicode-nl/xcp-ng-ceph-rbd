#!/usr/bin/python3
#
# plugin.py - SMAPIv3 Plugin.* dispatcher for the Ceph RBD volume plugin (rbd-vol).
#
# Standalone (no libcow): per-VDI Ceph RBD, control-plane via the ceph-mgr
# dashboard REST API, datapath via native krbd (/sys/bus/rbd) exposed as a raw
# block device (kernel blkback / vbd) - the SMAPIv3 sibling of the SMAPIv1 'rbd'
# driver. Phase-1 skeleton: Plugin.Query + Plugin.diagnostics only.
#
# The per-method executables (Plugin.Query, Plugin.diagnostics, ...) are symlinks
# to this file; we dispatch on argv[0]. python3 + python3-xapi-storage (v5).

import os
import sys

import xapi.storage.api.v5.plugin
from xapi.storage import log


class Implementation(xapi.storage.api.v5.plugin.Plugin_skeleton):

    def diagnostics(self, dbg):
        return "rbd-vol: phase-1 skeleton OK"

    def query(self, dbg):
        return {
            "plugin": "rbd-vol",
            "name": "Ceph RBD Volume plugin",
            "description": ("Per-VDI Ceph RBD storage (SMAPIv3). Control plane via "
                            "the ceph-mgr dashboard REST API; data path via native "
                            "krbd (aes256k/msgr2-secure) as a raw block device."),
            "vendor": "dicode",
            "copyright": "(C) 2026 dicode",
            "version": "0.1",
            "required_api_version": "5.0",
            # Keep this list in LOCKSTEP with the Volume.* executables we actually
            # ship (advertising a feature whose method is missing => runtime errors).
            # Target set for rbd-vol, all native RBD:
            #   phase 2/3: CREATE, DESTROY, SNAPSHOT, CLONE, RESIZE, REVERT (rbd snap
            #              rollback - zfs-vol omits this; we have it natively)
            #   later:     CBT via enable_cbt/disable_cbt/list_changed_blocks (rbd
            #              fast-diff) and copy/mirror via the separate Data plugin
            #              (snapshot-diff or opt-in tapdisk datapath) for backup/SXM.
            # compose is N/A for raw RBD.
            "features": [
                "VDI_CREATE",
                "VDI_DESTROY",
                "VDI_RESIZE",
                "VDI_SNAPSHOT",
                "VDI_CLONE",
            ],
            "configuration": {},
            "required_cluster_stack": []}


if __name__ == "__main__":
    log.log_call_argv()
    cmd = xapi.storage.api.v5.plugin.Plugin_commandline(Implementation())
    base = os.path.basename(sys.argv[0])
    if base == "Plugin.Query":
        cmd.query()
    elif base == "Plugin.diagnostics":
        cmd.diagnostics()
    else:
        raise xapi.storage.api.v5.plugin.Unimplemented(base)
