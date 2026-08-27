%global debug_package %{nil}
# Our modules are python3; dom0 compiles them into __pycache__ at runtime. Disable
# rpm's (python2) bytecompile so it doesn't emit stray, wrong-version .pyc/.pyo.
%global __os_install_post %(echo '%{__os_install_post}' | sed -e '/brp-python-bytecompile/d')

%global xsdir  /usr/libexec/xapi-storage-script
%global voldir %{xsdir}/volume/org.xen.xapi.storage.rbd-vol
%global dpdir  %{xsdir}/datapath/rbd

# SRPM base. Produces two component RPMs matching the XCP-ng naming convention
# (cf. xcp-ng-xapi-storage-volume-zfsvol / -datapath-tapdisk) plus a thin meta.
Name:           dicode-xapi-storage-rbd
Version:        0.1
Release:        1%{?dist}
Summary:        Ceph RBD SMAPIv3 storage plugin for XCP-ng (meta: volume + datapath)

License:        LGPL-2.1-only
URL:            https://github.com/dicode-nl/xcp-ng-ceph-rbd
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       dicode-xapi-storage-volume-rbd = %{version}-%{release}
Requires:       dicode-xapi-storage-datapath-rbd = %{version}-%{release}

%description
Meta-package pulling in the Ceph RBD SMAPIv3 volume plugin and its datapath.

# ---------------- volume plugin ----------------
%package -n dicode-xapi-storage-volume-rbd
Summary:        Ceph RBD SMAPIv3 volume plugin (org.xen.xapi.storage.rbd-vol)
Requires:       xapi-storage-script
Requires:       python3-xapi-storage
Requires:       python3 >= 3.6
# VDIs use the "rbd" datapath scheme -> pull the datapath in.
Requires:       dicode-xapi-storage-datapath-rbd = %{version}-%{release}
%description -n dicode-xapi-storage-volume-rbd
SMAPIv3 volume plugin presenting each VDI as a native Ceph RBD image. Manages the
SR and volumes over the ceph-mgr dashboard REST API (native snapshot/clone/
rollback, async flatten-on-delete GC, VDI metadata via rbd image-meta). SR type:
rbd-vol. Python3, standalone (no libcow).

# ---------------- datapath ----------------
%package -n dicode-xapi-storage-datapath-rbd
Summary:        Ceph RBD SMAPIv3 datapath (scheme "rbd"; blkback + tapdisk)
Requires:       xapi-storage-script
Requires:       python3-xapi-storage
Requires:       python3 >= 3.6
# tap-ctl for the tapdisk (vbd3) mode.
Requires:       blktap
# native krbd datapath (pulls libceph-module-alt + krb5-module).
Requires:       rbd-module-alt >= 1.0.0
%description -n dicode-xapi-storage-datapath-rbd
SMAPIv3 datapath for the "rbd" URI scheme. Maps RBD images via the in-kernel rbd
client (/sys/bus/rbd, aes256k/msgr2) and serves them either raw via kernel blkback
(backend_type vbd, default) or through tapdisk (backend_type vbd3; device-config
datapath=tapdisk or URI ?dp=tapdisk). Python3, driving tap-ctl directly.

%prep
%setup -q

%install
mkdir -p %{buildroot}%{voldir} %{buildroot}%{dpdir}
install -m 0755 volume/*.py   %{buildroot}%{voldir}/
install -m 0755 datapath/*.py %{buildroot}%{dpdir}/

cd %{buildroot}%{voldir}
ln -s plugin.py Plugin.Query
ln -s plugin.py Plugin.diagnostics
for m in probe create attach detach destroy stat ls set_name set_description; do
    ln -s sr.py SR.$m
done
for m in create destroy snapshot clone revert resize stat set unset set_name set_description; do
    ln -s volume.py Volume.$m
done

cd %{buildroot}%{dpdir}
ln -s plugin.py Plugin.Query
ln -s plugin.py Plugin.diagnostics
for m in attach detach activate activate_readonly deactivate open close; do
    ln -s datapath.py Datapath.$m
done

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
echo "      Run 'xe-toolstack-restart' on each pool host to register SR type 'rbd-vol'."

%post -n dicode-xapi-storage-datapath-rbd
systemctl try-restart xapi-storage-script.service >/dev/null 2>&1 || :

%changelog
* Thu Aug 27 2026 dicode <info@dicode.nl> - 0.1-1
- Initial SMAPIv3 Ceph RBD plugin, split per XCP-ng convention into
  dicode-xapi-storage-volume-rbd (org.xen.xapi.storage.rbd-vol) and
  dicode-xapi-storage-datapath-rbd (scheme rbd; blkback vbd + tapdisk vbd3).
