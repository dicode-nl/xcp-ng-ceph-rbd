%global debug_package %{nil}
%global smdir /opt/xensource/sm
# Our modules are python3; dom0 compiles them into __pycache__ at runtime. Disable
# rpm's (python2) bytecompile so it doesn't emit stray, wrong-version .pyc/.pyo.
%global __os_install_post %(echo '%{__os_install_post}' | sed -e '/brp-python-bytecompile/d')

Name:           sm-additional-rbd
Version:        2.0
Release:        1%{?dist}
Summary:        Ceph RBD Storage Repository driver for XCP-ng (native krbd + ceph-mgr dashboard API)

License:        LGPL-2.1-only
URL:            https://github.com/dicode-nl/xcp-ng-ceph-rbd
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

# SMAPIv1 framework (provides /opt/xensource/sm and the SR/VDI python modules).
Requires:       sm >= 3.0
Requires:       python3 >= 3.6
# Datapath: our backported aes256k krbd stack. rbd-module-alt is uniquely OURS
# (stock XCP-ng has no such package) and, via its own ABI-locked bidirectional
# deps, pulls the matched libceph-module-alt + krb5-module. So this single
# versioned Requires guarantees the correct kernel-module set is present.
Requires:       rbd-module-alt >= 1.0.0
# The 'rbd' plugin type must be registered in xapi and the datapath module loaded.
Requires(post): /usr/bin/grep, /usr/bin/sed
Requires(post): kmod

%description
A SMAPIv1 Storage Repository driver presenting each VDI as a native Ceph RADOS
Block Device (RBD). The data path uses the in-kernel rbd client (our backported
aes256k / msgr2-secure krbd, mapped directly via /sys/bus/rbd -- no rbd userspace
binary and no ceph-common on dom0). The control plane uses the ceph-mgr dashboard
REST API. All configuration is passed through xe device-config; the cephx key is
used only for the datapath. Namespace == SR uuid, image == VDI uuid.

Registers the 'rbd' SM plugin type. After install, run 'xe-toolstack-restart' on
every host in the pool to activate the driver.

%prep
%setup -q

%install
mkdir -p %{buildroot}%{smdir}
install -m 0755 RBDSR.py       %{buildroot}%{smdir}/RBDSR.py
install -m 0755 rbd_gc.py      %{buildroot}%{smdir}/rbd_gc.py
install -m 0644 rbd_backend.py %{buildroot}%{smdir}/rbd_backend.py
install -m 0644 rbd_sysfs.py   %{buildroot}%{smdir}/rbd_sysfs.py
# xapi discovers SR drivers by the extension-less <Type>SR executable in smdir.
ln -s RBDSR.py %{buildroot}%{smdir}/RBDSR

# Load the datapath kernel module at boot.
mkdir -p %{buildroot}%{_sysconfdir}/modules-load.d
echo rbd > %{buildroot}%{_sysconfdir}/modules-load.d/rbd.conf

%files
%{smdir}/RBDSR.py
%{smdir}/RBDSR
%{smdir}/rbd_gc.py
%{smdir}/rbd_backend.py
%{smdir}/rbd_sysfs.py
%config(noreplace) %{_sysconfdir}/modules-load.d/rbd.conf

%post
# 1) Register the 'rbd' SM plugin type in xapi's config (no drop-in dir on 8.3).
CONF=/etc/xapi.conf
if [ -f "$CONF" ] && grep -q '^sm-plugins=' "$CONF"; then
    if ! grep -Eq '^sm-plugins=([^#]* )?rbd( |$)' "$CONF"; then
        sed -i -e '/^sm-plugins=/ s/[[:space:]]*$/ rbd/' "$CONF"
    fi
fi
# 2) Load the datapath module now (best-effort; also loaded at boot).
/usr/sbin/modprobe rbd >/dev/null 2>&1 || :
# 3) The driver only becomes active after a toolstack restart.
echo "NOTE: sm-additional-rbd installed. Run 'xe-toolstack-restart' on THIS host"
echo "      and every pool member to activate the 'rbd' SR driver."

%postun
# On full removal (not upgrade), de-register the plugin type.
if [ "$1" = "0" ]; then
    CONF=/etc/xapi.conf
    if [ -f "$CONF" ]; then
        sed -i -e '/^sm-plugins=/ s/ rbd\b//g' "$CONF"
    fi
fi

%changelog
* Wed Aug 26 2026 dicode <info@dicode.nl> - 2.0-1
- Initial packaging of the RBD SR driver (async flatten-on-delete GC, native
  vdi_revert, dashboard REST control-plane, aes256k krbd datapath).
