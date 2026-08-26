%global kver 4.19.0+1

# Prebuilt, validated .ko artifacts: no debuginfo and no rpm post-processing
# (a full strip would drop .modinfo/vermagic and break module loading).
%global debug_package %{nil}
%global __os_install_post %{nil}

Name:           rbd-module-alt
Version:        1.0.0
Release:        1.xcpng8.3
Summary:        Alternate krbd (rbd.ko) with aes256k / msgr2-secure for XCP-ng 8.3
License:        GPLv2
URL:            https://github.com/dicode-nl/xcp-ng-ceph-rbd
Group:          System Environment/Kernel
BuildArch:      x86_64

# ABI-tied to the XCP-ng 8.3 dom0 kernel (uname 4.19.0+1).
Requires:       kernel-uname-r = %{kver}
Requires:       kmod
Requires(post): kmod
# The alternate rbd depends on the alternate libceph (which in turn depends on
# the krb5 module) — the aes256k/msgr2 code lives in libceph + krb5.
Requires:       libceph-module-alt = %{version}-%{release}

Source0:        krb5.ko
Source1:        libceph.ko
Source2:        rbd.ko

%description
Alternate rbd.ko (krbd) for the XCP-ng 8.3 dom0 kernel (Linux 4.19.0+1),
backported from mainline 5.14. Installed in override/ so it takes precedence
over the in-tree rbd; uninstalling reverts to the stock module. Supports RBD
namespaces and object-map/fast-diff. Requires libceph-module-alt (which
requires krb5-module) for msgr2 secure mode and the aes256k CephX key type
needed by Ceph 20.2.4 (Tentacle) / CVE-2025-30156.

%package -n libceph-module-alt
Summary:        Alternate libceph.ko (msgr1/msgr2 secure + aes256k) for XCP-ng 8.3
Group:          System Environment/Kernel
Requires:       kernel-uname-r = %{kver}
Requires:       kmod
Requires(post): kmod
# libceph's aes256k cephx crypto lives in the krb5 module (crypto_krb5_*).
Requires:       krb5-module = %{version}-%{release}
# ABI-locked pair: our libceph and our rbd have matched symbol CRCs (modversions)
# and MUST be installed together — stock rbd cannot load against our libceph and
# vice versa (the kernel refuses on CRC mismatch). Lock them as a versioned set.
Requires:       rbd-module-alt = %{version}-%{release}
%description -n libceph-module-alt
Alternate libceph.ko for the XCP-ng 8.3 dom0 kernel, backported from CentOS
Stream 9 (5.14) net/ceph. Adds msgr2 (incl. secure mode) and the aes256k CephX
key type (AES256-CTS-HMAC-SHA384-192). Installed in override/ (overrides the
in-tree libceph). Depends on krb5-module for the aes256k crypto.

%package -n krb5-module
Summary:        In-kernel Kerberos5 crypto library (aes256k enctype) for XCP-ng 8.3
Group:          System Environment/Kernel
Requires:       kernel-uname-r = %{kver}
Requires:       kmod
Requires(post): kmod
%description -n krb5-module
The in-kernel Kerberos5 crypto library (crypto/krb5) backported to the XCP-ng
8.3 dom0 kernel, providing the AES256-CTS-HMAC-SHA384-192 enctype used by the
aes256k CephX key type. This is an additional module (no in-tree equivalent on
4.19), installed in extra/. Required by libceph-module-alt.

%prep
%build
# Prebuilt out-of-tree against kernel-devel-%{kver}; reproducible build lives in
# the source project (src/ + patches/; see kernel-modules/README.md).

%install
rm -rf %{buildroot}
# alternate modules -> override/ (highest depmod priority, overrides in-tree)
install -d -m0755 %{buildroot}/lib/modules/%{kver}/override
install -m0644 %{SOURCE2} %{buildroot}/lib/modules/%{kver}/override/rbd.ko
install -m0644 %{SOURCE1} %{buildroot}/lib/modules/%{kver}/override/libceph.ko
# additional module (no in-tree equivalent) -> extra/
install -d -m0755 %{buildroot}/lib/modules/%{kver}/extra
install -m0644 %{SOURCE0} %{buildroot}/lib/modules/%{kver}/extra/krb5.ko

# ---- krb5-module ----
%post -n krb5-module
depmod -a %{kver} > /dev/null 2>&1 || :
[ -x /sbin/weak-modules ] && echo /lib/modules/%{kver}/extra/krb5.ko | \
    /sbin/weak-modules --add-modules --no-initramfs > /dev/null 2>&1 || :
%preun -n krb5-module
[ -x /sbin/weak-modules ] && echo /lib/modules/%{kver}/extra/krb5.ko | \
    /sbin/weak-modules --remove-modules --no-initramfs > /dev/null 2>&1 || :
%postun -n krb5-module
depmod -a %{kver} > /dev/null 2>&1 || :

# ---- libceph-module-alt ----
%post -n libceph-module-alt
depmod -a %{kver} > /dev/null 2>&1 || :
[ -x /sbin/weak-modules ] && echo /lib/modules/%{kver}/override/libceph.ko | \
    /sbin/weak-modules --add-modules --no-initramfs > /dev/null 2>&1 || :
%preun -n libceph-module-alt
[ -x /sbin/weak-modules ] && echo /lib/modules/%{kver}/override/libceph.ko | \
    /sbin/weak-modules --remove-modules --no-initramfs > /dev/null 2>&1 || :
%postun -n libceph-module-alt
depmod -a %{kver} > /dev/null 2>&1 || :

# ---- rbd-module-alt ----
%post
depmod -a %{kver} > /dev/null 2>&1 || :
[ -x /sbin/weak-modules ] && echo /lib/modules/%{kver}/override/rbd.ko | \
    /sbin/weak-modules --add-modules --no-initramfs > /dev/null 2>&1 || :
%preun
[ -x /sbin/weak-modules ] && echo /lib/modules/%{kver}/override/rbd.ko | \
    /sbin/weak-modules --remove-modules --no-initramfs > /dev/null 2>&1 || :
%postun
depmod -a %{kver} > /dev/null 2>&1 || :

%files
%dir /lib/modules/%{kver}/override
/lib/modules/%{kver}/override/rbd.ko

%files -n libceph-module-alt
%dir /lib/modules/%{kver}/override
/lib/modules/%{kver}/override/libceph.ko

%files -n krb5-module
# /lib/modules/%{kver}/extra is owned by the kernel package; do not claim it.
/lib/modules/%{kver}/extra/krb5.ko

%changelog
* Thu Aug 21 2026 dicode <info@dicode.nl> - 1.0.0-1.xcpng8.3
- Initial alternate ceph client for XCP-ng 8.3: rbd-module-alt + libceph-module-alt
  (override/) + krb5-module (extra/). aes256k, msgr2 secure, namespaces,
  object-map/fast-diff. Dep chain rbd-alt -> libceph-alt -> krb5-module.
  Runtime-validated on XCP-ng 8.3 vs Ceph 20.2.4.
