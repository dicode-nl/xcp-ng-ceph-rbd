#!/usr/bin/python3
#
# plugin.py - Plugin.Query/diagnostics for the rbd datapath (scheme "rbd://").

import os
import sys

import xapi.storage.api.v5.plugin
from xapi.storage import log


class Implementation(xapi.storage.api.v5.plugin.Plugin_skeleton):

    def diagnostics(self, dbg):
        return "rbd datapath: OK"

    def query(self, dbg):
        return {
            "plugin": "rbd",
            "name": "Ceph RBD datapath",
            "description": ("Maps RBD images via native krbd (/sys/bus/rbd) and "
                            "serves them in one of three modes (blkback / tapdisk "
                            "/ qemu-storage-daemon), chosen per-SR by device-config "
                            "`datapath=`."),
            "vendor": "dicode",
            "copyright": "(C) 2026 dicode",
            "version": "0.2",
            "required_api_version": "5.0",
            "features": ["VDI_MIRROR_IN"],
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
