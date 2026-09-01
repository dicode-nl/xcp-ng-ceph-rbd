%global debug_package %{nil}
# Our modules are python3; dom0 compiles them into __pycache__ at runtime. Disable
# rpm's (python2) bytecompile so it doesn't emit stray, wrong-version .pyc/.pyo.
%global __os_install_post %(echo '%{__os_install_post}' | sed -e '/brp-python-bytecompile/d')

%global xsdir   /usr/libexec/xapi-storage-script
%global voldir  %{xsdir}/volume/org.xen.xapi.storage.rbd-vol
%global dpdir   %{xsdir}/datapath/rbd
# Shared code lives in a normal python3 package `dicode.libs` on the dom0 path
# (like the canonical xapi.storage.libs) so the volume + datapath plugins import
# it as `from dicode.libs import ...` instead of duplicating it.
%global libsdir /usr/lib/python3.6/site-packages

# SRPM base. Produces three component RPMs matching the XCP-ng naming convention
# (cf. xcp-ng-xapi-storage-{libs,volume-zfsvol,datapath-tapdisk}) plus a thin meta.
Name:           dicode-xapi-storage-rbd
Version:        0.2
Release:        3%{?dist}
Summary:        Ceph RBD SMAPIv3 storage plugin for XCP-ng (meta: libs + volume + datapath)

License:        LGPL-2.1-only
URL:            https://github.com/dicode-nl/xcp-ng-ceph-rbd
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       dicode-xapi-storage-volume-rbd = %{version}-%{release}
Requires:       dicode-xapi-storage-datapath-rbd = %{version}-%{release}

%description
Meta-package pulling in the Ceph RBD SMAPIv3 volume plugin, its datapath, and the
shared dicode.libs support code.

# ---------------- shared libs ----------------
%package -n dicode-xapi-storage-libs-rbd
Summary:        Shared python3 support library (dicode.libs) for the rbd-vol plugin
Requires:       python3 >= 3.6
# OPTIONAL runtime deps (deliberately NOT hard Requires -- el7 rpm 4.11 has no
# weak deps): the LocalBackend (backend=local) drives the cluster through
# librados/librbd, so it needs python3-rados + python3-rbd. Install them only if
# you use backend=local; rbd_backend.py raises a clear RbdBackendError otherwise.
#   Recommends: python3-rados
#   Recommends: python3-rbd
%description -n dicode-xapi-storage-libs-rbd
Shared dicode.libs package used by both the rbd-vol volume plugin and the rbd
datapath: SR metadata store, krbd (/sys/bus/rbd) mapping, the librados/librbd
LocalBackend, the ceph-mgr dashboard REST client, CBT (cbtlog / rbd-diff),
flatten-on-delete GC, and the per-serve-mode datapath helpers (blkback / tapdisk /
qemu-storage-daemon). Installed to the dom0 python3 site-packages as `dicode.libs`.

# ---------------- volume plugin ----------------
%package -n dicode-xapi-storage-volume-rbd
Summary:        Ceph RBD SMAPIv3 volume plugin (org.xen.xapi.storage.rbd-vol)
Requires:       xapi-storage-script
Requires:       python3-xapi-storage
Requires:       python3 >= 3.6
Requires:       dicode-xapi-storage-libs-rbd = %{version}-%{release}
# VDIs use the "rbd" datapath scheme -> pull the datapath in.
Requires:       dicode-xapi-storage-datapath-rbd = %{version}-%{release}
%description -n dicode-xapi-storage-volume-rbd
SMAPIv3 volume plugin presenting each VDI as a native Ceph RBD image. Manages the
SR and volumes over the ceph-mgr dashboard REST API (native snapshot/clone/
rollback, async flatten-on-delete GC, VDI metadata via rbd image-meta), advertises
CBT (VDI_CONFIG_CBT) and live storage migration (VDI_MIRROR). SR type: rbd-vol.
Python3, standalone (no libcow).

# ---------------- datapath ----------------
%package -n dicode-xapi-storage-datapath-rbd
Summary:        Ceph RBD SMAPIv3 datapath (scheme "rbd"; blkback / tapdisk / qemu + SXM)
Requires:       xapi-storage-script
Requires:       python3-xapi-storage
Requires:       python3 >= 3.6
Requires:       dicode-xapi-storage-libs-rbd = %{version}-%{release}
# native krbd datapath (pulls libceph-module-alt + krb5-module).
Requires:       rbd-module-alt >= 1.0.0
# tap-ctl for the tapdisk (vbd3) serve mode.
Requires:       blktap
# OPTIONAL runtime deps (deliberately NOT hard Requires -- el7 rpm 4.11 has no
# weak deps): the qemu serve mode + SXM need qemu-storage-daemon / qemu-nbd
# (qemu-dp) exported over NBD and wired to /dev/nbdX by nbd-client (nbd). Install
# them only if you use datapath=qemu or receive SXM into a blkback SR; qsd.py /
# blknbd.py raise a clear error otherwise. The default blkback mode needs neither.
#   Recommends: qemu-dp
#   Recommends: nbd
%description -n dicode-xapi-storage-datapath-rbd
SMAPIv3 datapath for the "rbd" URI scheme. Maps RBD images via the in-kernel rbd
client (/sys/bus/rbd, aes256k/msgr2) and serves them in one of three modes, chosen
per-SR by device-config datapath= (or URI ?dp=): blkback (raw /dev/rbdN, kernel
blkback, backend_type vbd, default, max performance), tapdisk (backend_type vbd3;
CBT via cbtlog), or qemu (a per-VDI qemu-storage-daemon behind blkback that enables
race-free live storage migration via blockdev-mirror; declares VDI_MIRROR_IN).
Python3.

%prep
%setup -q

%install
mkdir -p %{buildroot}%{voldir} %{buildroot}%{dpdir} %{buildroot}%{libsdir}

# shared libs -> site-packages/dicode/{__init__.py,libs/*}
cp -a libs/dicode %{buildroot}%{libsdir}/
find %{buildroot}%{libsdir}/dicode -name '__pycache__' -type d -prune -exec rm -rf {} +
find %{buildroot}%{libsdir}/dicode -name '*.py' -exec chmod 0644 {} +

install -m 0755 volume/org.xen.xapi.storage.rbd-vol/*.py %{buildroot}%{voldir}/
install -m 0755 datapath/rbd/*.py                        %{buildroot}%{dpdir}/

# volume entry-point symlinks
cd %{buildroot}%{voldir}
ln -s plugin.py Plugin.Query
ln -s plugin.py Plugin.diagnostics
for m in probe create attach detach destroy stat ls set_name set_description; do
    ln -s sr.py SR.$m
done
for m in create destroy snapshot clone revert resize stat set unset \
         set_name set_description data_destroy enable_cbt disable_cbt \
         list_changed_blocks; do
    ln -s volume.py Volume.$m
done

# datapath entry-point symlinks
cd %{buildroot}%{dpdir}
ln -s plugin.py Plugin.Query
ln -s plugin.py Plugin.diagnostics
for m in attach detach activate activate_readonly deactivate open close \
         import_activate; do
    ln -s datapath.py Datapath.$m
done
# SXM DATA.* ops (get_nbd_server has a bespoke stdin parse; mirror/stat use the
# v5 Data_commandline) all dispatch from datapath.py by argv[0] basename.
for m in get_nbd_server mirror stat; do
    ln -s datapath.py DATA.$m
done

%files -n dicode-xapi-storage-libs-rbd
%dir %{libsdir}/dicode
%{libsdir}/dicode/*

%files -n dicode-xapi-storage-volume-rbd
%dir %{voldir}
%{voldir}/*

%files -n dicode-xapi-storage-datapath-rbd
%dir %{dpdir}
%{dpdir}/*

%post -n dicode-xapi-storage-volume-rbd
# v3 plugins are discovered by directory (no sm-plugins whitelist); nudge the
# storage-script to rescan. A toolstack restart registers the SR type in xapi.
systemctl try-restart xapi-storage-script.service >/dev/null 2>&1 || :
echo "NOTE: rbd-vol SMAPIv3 volume plugin installed."
echo "      Run 'xe-toolstack-restart' on each pool host to register SR type 'rbd-vol'"
echo "      (VDI_MIRROR/VDI_MIRROR_IN only promote once every host has re-registered)."

%post -n dicode-xapi-storage-datapath-rbd
systemctl try-restart xapi-storage-script.service >/dev/null 2>&1 || :

%changelog
* Mon Sep 01 2026 dicode <info@dicode.nl> - 0.2-3
- Cross-host live storage migration into the qemu/blkback rbd-vol datapath now
  works (VM node->node + a qemu disk -> blkback), validated md5-identical.
- qemu datapath VDI.copy/move fix: wire /dev/nbdX via xapi's
  nbd_client_manager.py so vhd-tool's copy path finds the connect-info file
  (VDI_COPY_FAILED ENOENT /var/run/nonpersistent/nbd/N otherwise).
- Volume.destroy gate: cascade-remove a base's rbd snapshots ONLY during SXM
  (dbg tagged migrate), refuse a normal user delete (protect real snapshots) --
  fixes the SXM base+snapshot cleanup leak that broke cross-host migration.
- Per-SR physical usage: SR.stat reports this SR's namespace allocation instead
  of the shared pool-wide figure (an empty SR no longer shows the pool as full).

* Mon Sep 01 2026 dicode <info@dicode.nl> - 0.2-2
- Live storage migration onto a NATIVE (blkback) rbd-vol SR now works
  (qemu-mode source -> blkback destination), validated md5 + reproducible.
- blkback datapath is a valid SXM destination: start the receive qemu-nbd
  (blknbd) ONLY for the mirror-VM attach (non-numeric domain), keeping guest
  attaches pure kernel blkback; advertise the live Nbd in that attach.
- Fix blknbd hang: detach qemu-nbd's stdio (its --fork does not close it, so
  the daemon held xapi's get_nbd_server result pipe open).
- Volume.destroy self-heals a stuck image: stop any per-image blknbd/qsd and
  unmap krbd before delete, so a failed/aborted SXM no longer leaks the dest
  image + qemu-nbd + krbd map ("RBD image is busy").

* Sun Aug 31 2026 dicode <info@dicode.nl> - 0.2-1
- Refactor: shared code moved into a python3 site-packages package `dicode.libs`,
  split out as the new dicode-xapi-storage-libs-rbd subpackage; volume + datapath
  are now thin and Require it.
- Datapath: single "rbd" datapath with three serve modes (blkback / tapdisk /
  qemu), selected by device-config datapath= or URI ?dp=.
- Live storage migration (SXM): qemu serve mode = per-VDI qemu-storage-daemon
  behind blkback; DATA.mirror (blockdev-mirror sync:full) + DATA.stat +
  Datapath.import_activate / DATA.get_nbd_server; drain-at-detach preserves data
  integrity for an actively-writing guest (crc32c-validated). Volume advertises
  VDI_MIRROR, datapath advertises VDI_MIRROR_IN.
- CBT wired across modes (cbtlog for tapdisk, rbd-diff for blkback/qemu):
  Volume.enable_cbt/disable_cbt/list_changed_blocks/data_destroy entry points.
- qemu-dp + nbd (qemu mode / SXM) and python3-rados + python3-rbd (backend=local)
  are OPTIONAL runtime deps, not hard Requires (el7 rpm has no weak deps): the
  code fails with a clear, actionable error if a mode is used without its tool,
  so the default blkback path installs with a minimal dependency set.

* Thu Aug 27 2026 dicode <info@dicode.nl> - 0.1-2
- GC smart-defer (flatten only when it frees the parent snap; else defer).
- rbd_features preset performance|compat; ms_mode default prefer-crc.

* Thu Aug 27 2026 dicode <info@dicode.nl> - 0.1-1
- Initial SMAPIv3 Ceph RBD plugin, split per XCP-ng convention into
  dicode-xapi-storage-volume-rbd (org.xen.xapi.storage.rbd-vol) and
  dicode-xapi-storage-datapath-rbd (scheme rbd; blkback vbd + tapdisk vbd3).
