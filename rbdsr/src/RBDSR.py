#!/usr/bin/python3
#
# RBDSR.py - Ceph RBD Storage Repository driver for XCP-ng 8.3 (SMAPIv1)
#
# Per-VDI native Ceph RBD storage. Datapath = our backported kernel rbd (krbd)
# with aes256k / msgr2-secure, mapped directly via /sys/bus/rbd (no `rbd`
# binary, no ceph-common). Control-plane = the ceph-mgr dashboard REST API
# (no ceph userspace on dom0). All config via `xe` device-config; the cephx
# key is used only for the sysfs datapath. No keyfiles on dom0.
#
# Lineage: structure adapted from Petr Bena's CephRBDSR (LGPL-2.1), reworked to
# drop all ceph/rbd CLI use in favour of RbdBackend + rbd_sysfs, with RBD
# namespaces and native VDI.revert (rbd snap rollback).
#
# License: LGPL-2.1-only

import SR
import VDI
import SRCommand
import util
import xs_errors
import os
import re
import sys
import json
import time
import tempfile
import subprocess
import xmlrpc.client

# XCP-ng 8.3 raw-block VDIs use vdi_type "phy": blktap2 VDI_PLUG_TYPE maps 'phy'
# -> plug the device directly (no tapdisk). There is no vhdutil.VDI_TYPE_RAW here.
VDI_TYPE_PHY = "phy"

import rbd_sysfs
from rbd_backend import make_backend, RbdBackendError

# Raw-block ("phy") SR: like RawISCSISR/HBASR we do NOT declare
# VDI_ACTIVATE/VDI_DEACTIVATE (those pair with ATOMIC_PAUSE / tapdisk pause,
# which a phy device has no use for). Map/unmap live entirely in attach/detach.
CAPABILITIES = [
    "SR_PROBE", "SR_UPDATE", "SR_SCAN", "SR_ATTACH", "SR_DETACH",
    "THIN_PROVISIONING",
    "VDI_CREATE", "VDI_DELETE", "VDI_ATTACH", "VDI_DETACH",
    "VDI_CLONE", "VDI_SNAPSHOT",
    "VDI_RESIZE", "VDI_RESIZE_ONLINE",
    "VDI_UPDATE", "VDI_INTRODUCE",
    "VDI_GENERATE_CONFIG", "VDI_ATTACH_OFFLINE",
    "VDI_RESET_ON_BOOT/2",
    "VDI_REVERT",
]

CONFIGURATION = [
    ['pool', 'Ceph pool name (required)'],
    ['mon_host', 'Ceph monitor address(es), comma-separated (required)'],
    ['user', 'cephx user for the datapath (default: admin)'],
    ['key', 'cephx base64 secret, aes256k (required)'],
    ['namespace', 'RBD namespace (default: SR uuid)'],
    ['ms_mode', 'msgr mode: secure|prefer-secure|crc|prefer-crc|legacy (default: secure)'],
    ['rbd_features', 'RBD image features, comma-separated (optional)'],
    ['image_prefix', 'optional image-name prefix'],
    ['api_url', 'ceph-mgr dashboard URL, e.g. https://mgr:8443 (required)'],
    ['api_user', 'dashboard account'],
    ['api_secret', 'dashboard password (not the cephx key)'],
    ['api_tls_verify', 'verify dashboard TLS cert (default: false)'],
    ['backend', 'control-plane backend (default: rest)'],
    ['backend_kind', 'guest VBD backend (default: vbd)'],
]

DRIVER_INFO = {
    'name': 'Ceph RBD Storage (native krbd + dashboard API)',
    'description': 'Per-VDI Ceph RADOS Block Device SR using the in-kernel rbd '
                   'driver (aes256k/msgr2-secure) for data and the ceph-mgr '
                   'dashboard REST API for management. No ceph userspace on dom0.',
    'vendor': 'dicode',
    'copyright': '(C) 2026 dicode; portions (C) 2025 Petr Bena',
    'driver_version': '2.0',
    'required_api_version': '1.0',
    'capabilities': CAPABILITIES,
    'configuration': CONFIGURATION,
}

# We hand XAPI a raw kernel block device (/dev/rbdN); no tapdisk in the datapath.
DRIVER_CONFIG = {"ATTACH_FROM_CONFIG_WITH_TAPDISK": False}

# One RBD namespace per SR (named by the SR uuid); image name == VDI uuid;
# snapshot name == snapshot-VDI uuid. No prefixes. Internal clone-parent snaps
# use a non-UUID name so scan() ignores them.
CLONE_SNAP_PREFIX = "xcp-clonebase-"
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

# Async flatten-on-delete: an object with CoW children is renamed to a non-UUID
# "xcp-trash-<uuid>" name (so scan() skips it) and handed to a detached rbd_gc.py
# worker, which flattens the children then purges the object. Job files persist
# in GC_SPOOL so a crashed/rebooted worker is retried by scan()'s sweep.
TRASH_PREFIX = "xcp-trash-"
GC_SPOOL = "/var/lib/rbdsr/gc"
GC_STALE_AGE = 600   # s; a job file older than this => its worker likely died


# ---------------------------------------------------------------------------
# vdi_revert routing: stock 8.3 SRCommand has no 'vdi_revert' branch, so xapi's
# VM.revert -> VDI.revert never reaches the driver. We add the dispatch here.
# Contract (sm.ml): vdi_uuid = the SNAPSHOT (target); args[0] = the base VDI ref.
# ---------------------------------------------------------------------------
def _install_vdi_revert_hook():
    if getattr(SRCommand.SRCommand, "_rbdsr_revert_hooked", False):
        return
    _orig_run = SRCommand.SRCommand._run

    def _run(self, sr, target):
        if self.cmd == 'vdi_revert':
            snap_vdi = target or sr.vdi(self.vdi_uuid)
            base_ref = self.params['args'][0]
            base_uuid = sr.session.xenapi.VDI.get_uuid(base_ref)
            return snap_vdi.revert(self.params['sr_uuid'], self.vdi_uuid, base_uuid)
        return _orig_run(self, sr, target)

    SRCommand.SRCommand._run = _run
    SRCommand.SRCommand._rbdsr_revert_hooked = True


_install_vdi_revert_hook()


class CephRBDSR(SR.SR):
    """Ceph RBD Storage Repository."""

    DRIVER_TYPE = "rbd"

    @staticmethod
    def handles(srtype):
        return srtype == CephRBDSR.DRIVER_TYPE

    def load(self, sr_uuid):
        util.SMlog("RBDSR.load %s" % sr_uuid)
        if 'pool' not in self.dconf:
            raise xs_errors.XenError('ConfigParameterMissing', opterr='pool')
        if 'mon_host' not in self.dconf:
            raise xs_errors.XenError('ConfigParameterMissing', opterr='mon_host')
        if 'key' not in self.dconf:
            raise xs_errors.XenError('ConfigParameterMissing', opterr='key')

        self.pool = self.dconf['pool']
        # Default: one RBD namespace per SR, named by the SR uuid (auto-created
        # in create()). An explicit device-config:namespace overrides it (lets an
        # admin pre-create the ns + a cephx key scoped to it for least privilege).
        self.namespace = self.dconf.get('namespace') or sr_uuid
        self.mon_host = self.dconf['mon_host']
        self.ceph_user = self.dconf.get('user', 'admin')
        self.key = self.dconf['key']
        self.ms_mode = self.dconf.get('ms_mode', 'secure')
        self.features = self.dconf.get(
            'rbd_features', 'layering,exclusive-lock,object-map,fast-diff,deep-flatten')
        # XCP-ng's default guest VBD backend (vbd3/blktap3/tapback) only serves
        # blktap devices and IGNORES our raw RBD (major 252) -> the PV disk never
        # connects. We put backend-kind in each VDI's sm-config; XAPI propagates
        # it to the VBD's other-config (same as RawISCSI/LUNperVDI), so guests get
        # kernel blkback (phy-passthrough). Override with device-config:backend_kind.
        self.backend_kind = self.dconf.get('backend_kind', 'vbd')

        self._backend = None
        self.sm_config = self.dconf
        # /dev/rbd is the kernel device root; individual VDIs resolve their own node.
        self.path = "/dev/rbd"

    # ---- backend (lazy; control-plane only) ----
    @property
    def backend(self):
        if self._backend is None:
            try:
                self._backend = make_backend(self.dconf)
            except RbdBackendError as e:
                raise xs_errors.XenError('SRUnavailable', opterr=str(e))
        return self._backend

    # ---- naming (image name == VDI uuid; snap name == snapshot-VDI uuid) ----
    def _image_name(self, vdi_uuid):
        return vdi_uuid

    def _snap_name(self, snap_uuid):
        return snap_uuid

    def _uuid_from_image(self, name):
        return name if name and UUID_RE.match(name) else None

    def _uuid_from_snap(self, name):
        return name if name and UUID_RE.match(name) else None

    def _features_list(self):
        return [f.strip() for f in self.features.split(',') if f.strip()]

    # ---- SR ops ----
    def create(self, sr_uuid, size):
        util.SMlog("RBDSR.create %s ns=%s" % (sr_uuid, self.namespace))
        try:
            self.backend.pool_stats(self.pool)  # verify pool reachable
            if self.namespace:
                self.backend.namespace_create(self.pool, self.namespace)
        except RbdBackendError as e:
            raise xs_errors.XenError('SRUnavailable',
                                     opterr='cannot init pool %s ns %s: %s'
                                     % (self.pool, self.namespace, e))

    def delete(self, sr_uuid):
        util.SMlog("RBDSR.delete %s ns=%s" % (sr_uuid, self.namespace))
        # Only remove the namespace we own (== sr_uuid) and only when empty.
        if self.namespace and self.namespace == sr_uuid:
            try:
                imgs = self.backend.list_images(self.pool, namespace=self.namespace)
                if imgs:
                    raise xs_errors.XenError(
                        'SRNotEmpty',
                        opterr='namespace %s still has %d images' % (self.namespace, len(imgs)))
                self.backend.namespace_remove(self.pool, self.namespace)
            except RbdBackendError as e:
                util.SMlog("RBDSR.delete: namespace cleanup skipped: %s" % e)

    def attach(self, sr_uuid):
        util.SMlog("RBDSR.attach %s" % sr_uuid)
        try:
            self.backend.pool_stats(self.pool)
        except RbdBackendError as e:
            raise xs_errors.XenError('SRUnavailable', opterr=str(e))
        self._set_stats()

    def detach(self, sr_uuid):
        util.SMlog("RBDSR.detach %s" % sr_uuid)

    def scan(self, sr_uuid):
        util.SMlog("RBDSR.scan %s" % sr_uuid)
        self.vdis = {}
        va = 0
        # Retry any GC job whose worker died, and learn which trashed objects
        # already have a job queued (so we don't double-queue below).
        gc_targets = self._sweep_gc_jobs()
        try:
            images = self.backend.list_images(self.pool, namespace=self.namespace)
        except RbdBackendError as e:
            raise xs_errors.XenError('SRScanError', opterr=str(e))

        for info in images:
            name = info.get('name')
            # A trashed image mid-GC: never a VDI. Backstop: if no job covers it
            # (worker died before persisting, or box lost the spool), re-queue one.
            if name and name.startswith(TRASH_PREFIX):
                if ('image', name, None) not in gc_targets:
                    self._spawn_gc_for('image', name)
                    gc_targets.add(('image', name, None))
                continue
            uuid = self._uuid_from_image(name) if name else None
            if not uuid or not UUID_RE.match(uuid):
                continue
            vdi = self.vdi(uuid)
            vdi._load_from_info(info)
            self.vdis[uuid] = vdi
            va += vdi.size
            for snap in info.get('snapshots', []) or []:
                sname = snap.get('name', '')
                # A trashed snapshot mid-GC on a live image: backstop re-queue.
                if sname.startswith(TRASH_PREFIX):
                    if ('snap', name, sname) not in gc_targets:
                        self._spawn_gc_for('snap', name, snap=sname)
                        gc_targets.add(('snap', name, sname))
                    continue
                suuid = self._uuid_from_snap(sname)
                if not suuid or not UUID_RE.match(suuid):
                    continue
                svdi = self.vdi(suuid)
                svdi._load_as_snapshot(info, snap, parent_uuid=uuid)
                self.vdis[suuid] = svdi
                va += svdi.size

        self.virtual_allocation = va
        self._set_stats(virtual_allocation=va)
        return super(CephRBDSR, self).scan(sr_uuid)

    def vdi(self, uuid):
        return CephRBDVDI(self, uuid)

    def update(self, sr_uuid):
        self._set_stats()

    def _set_stats(self, virtual_allocation=None):
        try:
            s = self.backend.pool_stats(self.pool)
        except RbdBackendError as e:
            util.SMlog("RBDSR: pool_stats failed: %s" % e)
            s = {'total': 0, 'used': 0}
        self.physical_size = int(s.get('total', 0))
        self.physical_utilisation = int(s.get('used', 0))
        if virtual_allocation is not None:
            self.virtual_allocation = virtual_allocation
        elif not hasattr(self, 'virtual_allocation') or self.virtual_allocation is None:
            self.virtual_allocation = 0
        if self.sr_ref:
            self._db_update()

    def _updateStats(self, sr_uuid, virtAllocDelta):
        va = getattr(self, 'virtual_allocation', 0) or 0
        if self.sr_ref:
            try:
                va = int(self.session.xenapi.SR.get_virtual_allocation(self.sr_ref))
            except Exception:
                pass
        self._set_stats(virtual_allocation=max(0, va + virtAllocDelta))

    def probe(self):
        # Minimal probe: report pool usage; images enumerated via scan.
        util.SMlog("RBDSR.probe pool=%s ns=%s" % (self.pool, self.namespace))
        return "<?xml version=\"1.0\" ?>\n<SRlist/>\n"

    # ---- async GC (keep the big flatten off the VDI.delete path) ----
    def _worker_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "rbd_gc.py")

    def _launch_worker(self, job_path):
        # Detached (setsid) so it outlives this SM command process.
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(
                [sys.executable or "python3", self._worker_path(), job_path],
                stdin=devnull, stdout=devnull, stderr=devnull,
                close_fds=True, start_new_session=True)

    def _spawn_gc_for(self, kind, image, snap=None):
        """Persist a GC job (flatten CoW children + purge the trashed object) and
        launch a detached worker. The job file (0600, root-only spool) carries the
        SR device-config so the worker can re-auth to the dashboard on its own."""
        try:
            os.makedirs(GC_SPOOL, exist_ok=True)
            os.chmod(GC_SPOOL, 0o700)
        except OSError:
            pass
        job = {'dconf': self.dconf, 'pool': self.pool, 'namespace': self.namespace,
               'kind': kind, 'image': image}
        if snap is not None:
            job['snap'] = snap
        fd, path = tempfile.mkstemp(prefix="gc-", suffix=".json", dir=GC_SPOOL)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(job, f)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        try:
            self._launch_worker(path)
        except Exception as e:
            util.SMlog("RBDSR: GC worker launch failed (%s); scan will retry: %s" % (path, e))
        util.SMlog("RBDSR: queued async GC kind=%s img=%s snap=%s job=%s"
                   % (kind, image, snap, path))

    def _sweep_gc_jobs(self):
        """Relaunch job files whose worker looks dead (mtime older than
        GC_STALE_AGE) and return the set of (kind,image,snap) targets that already
        have a job, so scan() won't double-queue a trashed object."""
        targets = set()
        try:
            files = os.listdir(GC_SPOOL)
        except OSError:
            return targets
        now = time.time()
        for fn in files:
            if not fn.endswith(".json"):
                continue
            path = os.path.join(GC_SPOOL, fn)
            try:
                with open(path) as f:
                    job = json.load(f)
            except (IOError, OSError, ValueError):
                continue
            targets.add((job.get('kind'), job.get('image'), job.get('snap')))
            try:
                stale = (now - os.path.getmtime(path)) >= GC_STALE_AGE
            except OSError:
                stale = False
            if stale:
                try:
                    os.utime(path, None)   # reset clock so the next scan won't re-spawn
                    self._launch_worker(path)
                    util.SMlog("RBDSR.scan: re-spawned stale GC job %s" % path)
                except Exception as e:
                    util.SMlog("RBDSR.scan: could not re-spawn GC %s: %s" % (path, e))
        return targets


class CephRBDVDI(VDI.VDI):
    """A single RBD image (or an RBD snapshot) presented as a VDI."""

    def __init__(self, sr, uuid):
        self.rbd_name = None
        self.snap_name = None
        # a VDI *ref* for _db_introduce; must be a valid ref string, never None
        # (util.default keeps an existing None instead of its "OpaqueRef:NULL"
        # default -> db_introduce would get None and fail "Expected string").
        self.snapshot_of = "OpaqueRef:NULL"
        self.parent_uuid = None   # parent VDI *uuid* - for all our internal logic
        self.is_a_snapshot = False
        self.read_only = False
        VDI.VDI.__init__(self, sr, uuid)
        self.vdi_type = VDI_TYPE_PHY
        if not hasattr(self, 'sm_config') or self.sm_config is None:
            self.sm_config = {}
        # Always carry backend-kind so _db_update never strips it (propagates to
        # the VBD -> kernel blkback). See CephRBDSR.load.
        self.sm_config['backend-kind'] = self.sr.backend_kind

    # ---- loading ----
    def load(self, vdi_uuid):
        self.uuid = vdi_uuid
        self.location = vdi_uuid
        # Default to a plain image; scan()/snapshot flows refine this.
        if not self.rbd_name:
            self.rbd_name = self.sr._image_name(vdi_uuid)
        try:
            ref = self.sr.session.xenapi.VDI.get_by_uuid(vdi_uuid)
            self.sm_config = self.sr.session.xenapi.VDI.get_sm_config(ref)
            self.is_a_snapshot = self.sr.session.xenapi.VDI.get_is_a_snapshot(ref)
            self.read_only = self.sr.session.xenapi.VDI.get_read_only(ref)
            self.size = int(self.sr.session.xenapi.VDI.get_virtual_size(ref))
            if self.is_a_snapshot:
                self.parent_uuid = self.sm_config.get('snapshot_of')
                self.snap_name = self.sr._snap_name(vdi_uuid)
        except Exception:
            pass  # not yet in the XAPI DB (fresh discovery) -> defaults stand

    def _load_from_info(self, info):
        self.uuid = self._uuid_or(info)
        self.location = self.uuid
        self.rbd_name = info.get('name')
        self.size = int(info.get('size', 0))
        self.utilisation = int(info.get('disk_usage', 0) or 0)
        self.is_a_snapshot = False

    def _load_as_snapshot(self, parent_info, snap, parent_uuid):
        self.location = self.uuid
        self.is_a_snapshot = True
        self.read_only = True
        self.parent_uuid = parent_uuid
        self.rbd_name = parent_info.get('name')
        self.snap_name = snap.get('name')
        self.size = int(snap.get('size', parent_info.get('size', 0)) or 0)
        self.utilisation = 0
        self.sm_config['snapshot_of'] = parent_uuid

    def _uuid_or(self, info):
        return self.sr._uuid_from_image(info.get('name', '')) or self.uuid

    # ---- create / delete ----
    def create(self, sr_uuid, vdi_uuid, size):
        util.SMlog("RBDVDI.create %s size=%d" % (vdi_uuid, size))
        self.uuid = vdi_uuid
        self.location = vdi_uuid
        self.size = int(size)
        self.utilisation = 0
        self.rbd_name = self.sr._image_name(vdi_uuid)
        try:
            self.sr.backend.create(self.sr.pool, self.rbd_name, size,
                                   self.sr._features_list(), namespace=self.sr.namespace)
        except RbdBackendError as e:
            raise xs_errors.XenError('VDICreate', opterr=str(e))
        self.sm_config['vdi_type'] = self.vdi_type
        try:
            self._db_introduce()
        except Exception as e:
            # DB introduce failed after the RBD image was created -> don't leak it.
            util.SMlog("RBDVDI.create: db_introduce failed, removing orphan %s: %s"
                       % (self.rbd_name, e))
            try:
                self.sr.backend.remove(self.sr.pool, self.rbd_name, namespace=self.sr.namespace)
            except RbdBackendError:
                pass
            raise
        self.sr._updateStats(sr_uuid, self.size)
        return self.get_params()

    def _flatten_children(self, base_image, snap_name=None):
        """Lazy flatten: any clone depending on base_image@snap (a CoW child)
        must be flattened before that snapshot can be removed. Children always
        hang off a snapshot in RBD; flattening detaches them from the parent."""
        try:
            info = self.sr.backend.image_info(self.sr.pool, base_image, namespace=self.sr.namespace)
        except RbdBackendError:
            return
        for snap in info.get('snapshots', []) or []:
            if snap_name is not None and snap.get('name') != snap_name:
                continue
            for child in snap.get('children', []) or []:
                cimage = child.get('image_name')
                if not cimage:
                    continue
                util.SMlog("RBDVDI.delete: flattening child %s of %s@%s"
                           % (cimage, base_image, snap.get('name')))
                self.sr.backend.flatten(child.get('pool_name', self.sr.pool), cimage,
                                        namespace=child.get('namespace', self.sr.namespace))

    def _remove_snap(self, base_image, snap_name):
        self._flatten_children(base_image, snap_name)   # detach any clones first
        try:
            self.sr.backend.snap_set_protected(self.sr.pool, base_image, snap_name, False,
                                               namespace=self.sr.namespace)
        except RbdBackendError:
            pass
        self.sr.backend.snap_remove(self.sr.pool, base_image, snap_name, namespace=self.sr.namespace)

    def _has_children(self, base_image, snap_name=None):
        """True if any snapshot of base_image (or just snap_name) has a CoW child.
        Such children must be flattened before the snap/image can be removed."""
        try:
            info = self.sr.backend.image_info(self.sr.pool, base_image, namespace=self.sr.namespace)
        except RbdBackendError:
            return False
        for snap in info.get('snapshots', []) or []:
            if snap_name is not None and snap.get('name') != snap_name:
                continue
            if snap.get('children'):
                return True
        return False

    def delete(self, sr_uuid, vdi_uuid, data_only=False):
        util.SMlog("RBDVDI.delete %s snap=%s" % (vdi_uuid, self.is_a_snapshot))
        if getattr(self, 'attached', False):
            raise xs_errors.XenError('VDIInUse', opterr='attached')
        try:
            if self.is_a_snapshot:
                base = self.sr._image_name(self.parent_uuid) if self.parent_uuid else self.rbd_name
                snap = self.snap_name or self.sr._snap_name(vdi_uuid)
                if self._has_children(base, snap_name=snap):
                    # Clone(s) still hang off this snap. Rename it out of the UUID
                    # space so scan() ignores it, and let the background GC flatten
                    # the clone(s) + purge the snap -> delete() returns at once.
                    trash = TRASH_PREFIX + util.gen_uuid()
                    self.sr.backend.snap_rename(self.sr.pool, base, snap, trash,
                                                namespace=self.sr.namespace)
                    self.sr._spawn_gc_for('snap', base, snap=trash)
                else:
                    self._remove_snap(base, snap)   # no children => quick, inline
            else:
                image = self.sr._image_name(vdi_uuid)
                try:
                    info = self.sr.backend.image_info(self.sr.pool, image, namespace=self.sr.namespace)
                except RbdBackendError as e:
                    info = None
                    if not e.not_found:
                        raise
                if info:
                    snaps = info.get('snapshots', []) or []
                    # An RBD snapshot cannot outlive its image, so deleting the
                    # image would silently destroy any real (XAPI-tracked, UUID-
                    # named) snapshots. Refuse and make the user delete those
                    # snapshot VDIs first (XAPI allows the destroy but keeps the
                    # snapshot VDI, which would otherwise dangle).
                    vdi_snaps = [s for s in snaps if UUID_RE.match(s.get('name') or '')]
                    if vdi_snaps:
                        raise xs_errors.XenError(
                            'VDIInUse',
                            opterr='image has %d snapshot(s); delete the snapshot VDIs first'
                                   % len(vdi_snaps))
                    if any(s.get('children') for s in snaps):
                        # A clonebase snap still has a CoW child. Trash-rename the
                        # whole image (scan-invisible); the GC flattens children,
                        # drops the internal snaps, then removes the image.
                        trash = TRASH_PREFIX + util.gen_uuid()
                        self.sr.backend.image_rename(self.sr.pool, image, trash,
                                                     namespace=self.sr.namespace)
                        self.sr._spawn_gc_for('image', trash)
                    else:
                        # only childless internal clonebase snaps -> remove inline
                        for snap in snaps:
                            self._remove_snap(image, snap.get('name'))
                        self.sr.backend.remove(self.sr.pool, image, namespace=self.sr.namespace)
                else:
                    self.sr.backend.remove(self.sr.pool, image, namespace=self.sr.namespace)
        except RbdBackendError as e:
            if e.not_found:
                util.SMlog("RBDVDI.delete: already gone, cleaning up DB entry")
            elif 'watchers' in str(e).lower() or 'in use' in str(e).lower():
                raise xs_errors.XenError('VDIInUse',
                                         opterr='image still mapped somewhere: %s' % e)
            else:
                raise xs_errors.XenError('VDIDelete', opterr=str(e))
        if vdi_uuid in self.sr.vdis:
            del self.sr.vdis[vdi_uuid]
        self.sr._updateStats(sr_uuid, -int(getattr(self, 'size', 0) or 0))
        self._db_forget()

    # ---- attach / detach (datapath via sysfs) ----
    def _map(self, read_only=False):
        base = self.sr._image_name(self.parent_uuid) if (self.is_a_snapshot and self.parent_uuid) \
            else (self.rbd_name or self.sr._image_name(self.uuid))
        snap = self.snap_name if self.is_a_snapshot else None
        try:
            return rbd_sysfs.map_image(
                self.sr.mon_host, self.sr.ceph_user, self.sr.key,
                self.sr.pool, base, snap=snap, ms_mode=self.sr.ms_mode,
                read_only=read_only or self.is_a_snapshot, namespace=self.sr.namespace)
        except rbd_sysfs.RbdMapError as e:
            raise xs_errors.XenError('VDIUnavailable', opterr='rbd map failed: %s' % e)

    def _unmap(self):
        base = self.sr._image_name(self.parent_uuid) if (self.is_a_snapshot and self.parent_uuid) \
            else (self.rbd_name or self.sr._image_name(self.uuid))
        snap = self.snap_name if self.is_a_snapshot else None
        try:
            rbd_sysfs.unmap_image(self.sr.pool, base, snap=snap, namespace=self.sr.namespace)
        except rbd_sysfs.RbdMapError as e:
            util.SMlog("RBDVDI._unmap warning: %s" % e)

    # attach/detach form the map bracket; blktap2 reads the phy path from the
    # struct returned here and plugs /dev/rbdN directly (vdi_type 'phy', no tap).
    def attach(self, sr_uuid, vdi_uuid):
        util.SMlog("RBDVDI.attach %s" % vdi_uuid)
        self.path = self._map(read_only=self.read_only)
        self.attached = True
        return VDI.VDI.attach(self, sr_uuid, vdi_uuid)

    def detach(self, sr_uuid, vdi_uuid):
        util.SMlog("RBDVDI.detach %s" % vdi_uuid)
        self._unmap()
        self.attached = False

    # No activate/deactivate: as a phy driver we don't declare VDI_ACTIVATE/
    # VDI_DEACTIVATE, so blktap2 never calls them. The kernel map lives for the
    # whole attach/detach bracket and /dev/rbdN is plugged directly (no tapdisk).

    # ---- snapshot / clone ----
    def snapshot(self, sr_uuid, vdi_uuid):
        util.SMlog("RBDVDI.snapshot of %s" % vdi_uuid)
        if self.is_a_snapshot:
            raise xs_errors.XenError('VDISnapshot', opterr='cannot snapshot a snapshot')
        snap_uuid = util.gen_uuid()
        base = self.sr._image_name(vdi_uuid)
        snap = self.sr._snap_name(snap_uuid)
        try:
            self.sr.backend.snap_create(self.sr.pool, base, snap, namespace=self.sr.namespace)
            self.sr.backend.snap_set_protected(self.sr.pool, base, snap, True,
                                               namespace=self.sr.namespace)
        except RbdBackendError as e:
            self._purge_snap_quiet(base, snap)   # roll back a half-made snap
            raise xs_errors.XenError('VDISnapshot', opterr=str(e))
        svdi = self.sr.vdi(snap_uuid)
        svdi.size = self.size
        svdi.utilisation = 0
        svdi.read_only = True
        svdi.is_a_snapshot = True
        svdi.parent_uuid = vdi_uuid
        # _db_introduce needs snapshot_of as a VDI *ref*, not a uuid.
        svdi.snapshot_of = self.sr.session.xenapi.VDI.get_by_uuid(vdi_uuid)
        svdi.label = (self.label or "") + " (snapshot)"
        svdi.description = self.description or ""
        svdi.rbd_name = base
        svdi.snap_name = snap
        svdi.location = snap_uuid
        svdi.sm_config = {'vdi_type': self.vdi_type, 'snapshot_of': vdi_uuid,
                          'backend-kind': self.sr.backend_kind}
        try:
            svdi._db_introduce()
        except Exception as e:
            util.SMlog("RBDVDI.snapshot: db_introduce failed, removing snap %s@%s: %s"
                       % (base, snap, e))
            self._purge_snap_quiet(base, snap)
            raise
        self.sr.vdis[snap_uuid] = svdi
        return svdi.get_params()

    def _purge_snap_quiet(self, base, snap):
        """Best-effort unprotect+remove of a snapshot; swallow backend errors
        (used only on the rollback paths of snapshot()/clone())."""
        try:
            self.sr.backend.snap_set_protected(self.sr.pool, base, snap, False,
                                               namespace=self.sr.namespace)
        except RbdBackendError:
            pass
        try:
            self.sr.backend.snap_remove(self.sr.pool, base, snap, namespace=self.sr.namespace)
        except RbdBackendError:
            pass

    def clone(self, sr_uuid, vdi_uuid):
        util.SMlog("RBDVDI.clone of %s (snap=%s)" % (vdi_uuid, self.is_a_snapshot))
        clone_uuid = util.gen_uuid()
        clone_img = self.sr._image_name(clone_uuid)
        feats = self.sr._features_list()
        base = None
        transient = None   # the throwaway clonebase snap we made (live-image path)
        try:
            if self.is_a_snapshot:
                base = self.sr._image_name(self.parent_uuid)
                snap = self.snap_name
            else:
                # writable clone of a live image: snap+protect a transient point,
                # then CoW-clone from it (instant; flatten deferred). The transient
                # snap uses a non-UUID name so scan() never treats it as a VDI.
                base = self.sr._image_name(vdi_uuid)
                snap = CLONE_SNAP_PREFIX + util.gen_uuid()
                transient = snap
                self.sr.backend.snap_create(self.sr.pool, base, snap, namespace=self.sr.namespace)
                self.sr.backend.snap_set_protected(self.sr.pool, base, snap, True,
                                                   namespace=self.sr.namespace)
            self.sr.backend.clone(self.sr.pool, base, snap, self.sr.pool, clone_img,
                                  features=feats, namespace=self.sr.namespace,
                                  dst_namespace=self.sr.namespace)
        except RbdBackendError as e:
            if transient:   # only remove a transient snap we ourselves created
                self._purge_snap_quiet(base, transient)
            raise xs_errors.XenError('VDIClone', opterr=str(e))
        cvdi = self.sr.vdi(clone_uuid)
        cvdi.size = self.size
        cvdi.utilisation = 0
        cvdi.read_only = False
        cvdi.rbd_name = clone_img
        cvdi.location = clone_uuid
        cvdi.sm_config = {'vdi_type': self.vdi_type, 'backend-kind': self.sr.backend_kind}
        try:
            cvdi._db_introduce()
        except Exception as e:
            # Roll back the whole clone: drop the child first (so the transient
            # clonebase snap has no dependants), then the transient snap.
            util.SMlog("RBDVDI.clone: db_introduce failed, rolling back %s: %s" % (clone_img, e))
            try:
                self.sr.backend.remove(self.sr.pool, clone_img, namespace=self.sr.namespace)
            except RbdBackendError:
                pass
            if transient:
                self._purge_snap_quiet(base, transient)
            raise
        self.sr.vdis[clone_uuid] = cvdi
        self.sr._updateStats(sr_uuid, self.size)
        return cvdi.get_params()

    # ---- resize ----
    def resize(self, sr_uuid, vdi_uuid, size, online=False):
        util.SMlog("RBDVDI.resize %s -> %d" % (vdi_uuid, size))
        if size == self.size:
            return VDI.VDI.get_params(self)
        try:
            self.sr.backend.resize(self.sr.pool, self.sr._image_name(vdi_uuid), size,
                                   namespace=self.sr.namespace)
        except RbdBackendError as e:
            raise xs_errors.XenError('VDISize', opterr=str(e))
        delta = int(size) - int(self.size or 0)
        self.size = int(size)
        self._db_update()
        self.sr._updateStats(sr_uuid, delta)
        return VDI.VDI.get_params(self)

    def resize_online(self, sr_uuid, vdi_uuid, size):
        return self.resize(sr_uuid, vdi_uuid, size, online=True)

    # ---- revert (native rbd rollback; see ceph-rbdsr-vdi-revert) ----
    def revert(self, sr_uuid, snap_uuid, base_uuid):
        util.SMlog("RBDVDI.revert base=%s to snap=%s" % (base_uuid, snap_uuid))
        base = self.sr._image_name(base_uuid)
        snap = self.sr._snap_name(snap_uuid)
        try:
            self.sr.backend.snap_rollback(self.sr.pool, base, snap, namespace=self.sr.namespace)
        except RbdBackendError as e:
            # Structural "can't do it" -> Unimplemented so VM.revert falls back to
            # the clone method rather than hard-failing. Real failures propagate.
            if e.not_found or e.not_supported:
                raise xs_errors.XenError('Unimplemented',
                                         opterr='rbd rollback unavailable: %s' % e)
            raise xs_errors.XenError('VDIUnavailable', opterr='rbd rollback failed: %s' % e)
        # vdi_revert is parsed by Sm_exec.parse_unit -> return unit/nil, not a struct.

    # ---- reset-on-boot (routed via vdi_epoch_begin) ----
    def reset_leaf(self, sr_uuid, vdi_uuid):
        util.SMlog("RBDVDI.reset_leaf %s" % vdi_uuid)
        snap_ref = self.sm_config.get('reset_snap')
        if not snap_ref:
            raise xs_errors.XenError('VDIUnavailable',
                                     opterr='no reset snapshot recorded for %s' % vdi_uuid)
        try:
            self.sr.backend.snap_rollback(self.sr.pool, self.sr._image_name(vdi_uuid),
                                          snap_ref, namespace=self.sr.namespace)
        except RbdBackendError as e:
            raise xs_errors.XenError('VDIUnavailable', opterr=str(e))

    # ---- attach-from-config (VDI_GENERATE_CONFIG / offline attach) ----
    def generate_config(self, sr_uuid, vdi_uuid):
        """Emit a self-contained blob to attach this VDI when XAPI isn't running
        (HA statefile / boot-from-SR). Double-XMLRPC-encoded, per the SM contract
        (same shape as CephFSSR/NFSSR)."""
        util.SMlog("RBDVDI.generate_config %s" % vdi_uuid)
        resp = {'device_config': self.sr.dconf,
                'sr_uuid': sr_uuid,
                'vdi_uuid': vdi_uuid,
                'sr_sm_config': self.sr.sm_config,
                'command': 'vdi_attach_from_config'}
        config = xmlrpc.client.dumps(tuple([resp]), "vdi_attach_from_config")
        return xmlrpc.client.dumps((config,), "", True)

    def attach_from_config(self, sr_uuid, vdi_uuid):
        """Map the VDI from the generated config (no XAPI DB). Datapath only:
        the control-plane (dashboard) isn't needed to map an existing image."""
        util.SMlog("RBDVDI.attach_from_config %s" % vdi_uuid)
        try:
            return self._map(read_only=False)
        except Exception:
            util.logException("RBDVDI.attach_from_config")
            raise xs_errors.XenError('SRUnavailable',
                                     opterr='Unable to attach %s from config' % vdi_uuid)

    def update(self, sr_uuid, vdi_uuid):
        # vdi_update is parsed by Sm_exec.parse_unit -> must return unit/nil,
        # NOT a struct (returning get_params() => "unbox struct should contain nil").
        self._db_update()


if __name__ == '__main__':
    SRCommand.run(CephRBDSR, DRIVER_INFO)
else:
    SR.registerSR(CephRBDSR)
