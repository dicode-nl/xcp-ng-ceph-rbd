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
            "version": "0.2",
            "required_api_version": "5.0",
            # Keep this list in LOCKSTEP with the Volume.* executables we actually
            # ship (advertising a feature whose method is missing => runtime errors).
            #   VDI_CONFIG_CBT -> enable_cbt/disable_cbt/data_destroy/
            #     list_changed_blocks, native via rbd diff (needs the rbd_features
            #     'performance' preset + the ceph-mgr /diff endpoint, or backend=local).
            # Still to come: copy/mirror via the separate Data plugin (snapshot-diff
            # or opt-in tapdisk datapath) for backup/SXM. compose is N/A for raw RBD.
            # VDI_MIRROR advertises the SR-level mirror capability so xapi allows
            # VDI.pool_migrate (SXM). The actual mirror runs in the *datapath*
            # plugin (DATA.mirror/Datapath.import_activate, which declares
            # VDI_MIRROR_IN) and only works with the tapdisk datapath.
            "features": [
                "VDI_CREATE",
                "VDI_DESTROY",
                "VDI_RESIZE",
                "VDI_SNAPSHOT",
                "VDI_CLONE",
                "VDI_CONFIG_CBT",
                "VDI_MIRROR",
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
