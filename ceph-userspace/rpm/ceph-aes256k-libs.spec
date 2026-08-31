# Dicode override: repackage the aes256k-backported Ceph 15.2.17 client
# libraries (already built from the patched source tree) into drop-in
# replacements for the stock librados2/librbd1/python3-rados/python3-rbd on an
# XCP-ng 8.3 (el7) dom0. Only these client packages are produced -- NOT the
# ceph daemons/rgw/mgr. See ceph-15.2.17-aes256k.patch for the source change.

%global debug_package %{nil}
%global __brp_check_rpaths %{nil}
# The stock Ceph subpackages (ceph-common, librgw2, python3-cephfs, ...) pin
# librados2/librbd1/python3-rados/python3-rbd with an EXACT "= 2:15.2.17-0.el7".
# Provide that exact EVR (in addition to our own .dicode release) so those pins
# stay satisfied when our libs override the stock ones.
%global stock_evr 2:15.2.17-0.el7
# keep the RPATH we set with chrpath; do not let QA mangle/strip it away.

Name:           ceph-aes256k-libs
Version:        15.2.17
Release:        0%{?dist}.dicode.aes256k1
Epoch:          2
Summary:        Dicode aes256k rebuild of the Ceph client libraries (override metapackage)
License:        LGPL-2.1
Vendor:         Dicode <info@dicode.nl> (github.com/dicode-nl/xcp-ng-ceph-rbd)
URL:            https://github.com/dicode-nl/xcp-ng-ceph-rbd
Source0:        ceph-aes256k-libs.tar.gz
BuildRequires:  chrpath
ExclusiveArch:  x86_64

%description
Prebuilt librados/librbd and their python3 bindings from Ceph 15.2.17 with the
cephx AES256KRB5 (aes256k, RFC 8009 enctype 20) backport, repackaged to
override the stock Ceph client libraries so an el7 dom0 can drive a Ceph 20.2.4
cluster with a type-2 cephx key. Unofficial Dicode build; not endorsed by or
affiliated with the Ceph project. This metapackage itself ships no files.

#------------------------------------------------------------------- librados2
%package -n librados2
Summary:        RADOS distributed object store client library (Dicode aes256k rebuild)
License:        LGPL-2.1
Provides:       librados2 = %{stock_evr}
%description -n librados2
librados client library (Ceph 15.2.17) with the cephx aes256k backport.
Ships libceph-common.so.2 as well. Unofficial Dicode rebuild.
%post -n librados2 -p /sbin/ldconfig
%postun -n librados2 -p /sbin/ldconfig
%files -n librados2
%dir /usr/lib64/ceph
/usr/lib64/ceph/libceph-common.so.2
/usr/lib64/librados.so.2
/usr/lib64/librados.so.2.0.0

#--------------------------------------------------------------------- librbd1
%package -n librbd1
Summary:        RADOS block device client library (Dicode aes256k rebuild)
License:        LGPL-2.1
Provides:       librbd1 = %{stock_evr}
Requires:       librados2 = %{epoch}:%{version}-%{release}
%description -n librbd1
librbd client library (Ceph 15.2.17) with the cephx aes256k backport.
Unofficial Dicode rebuild.
%post -n librbd1 -p /sbin/ldconfig
%postun -n librbd1 -p /sbin/ldconfig
%files -n librbd1
/usr/lib64/librbd.so.1
/usr/lib64/librbd.so.1.12.0

#---------------------------------------------------------------- python3-rados
%package -n python3-rados
Summary:        Python 3 libraries for the RADOS object store (Dicode aes256k rebuild)
License:        LGPL-2.1
Provides:       python3-rados = %{stock_evr}
Requires:       librados2 = %{epoch}:%{version}-%{release}
%description -n python3-rados
Python 3 bindings for librados, with the cephx aes256k backport.
Unofficial Dicode rebuild.
%files -n python3-rados
/usr/lib64/python3.6/site-packages/rados.cpython-36m-x86_64-linux-gnu.so

#------------------------------------------------------------------ python3-rbd
%package -n python3-rbd
Summary:        Python 3 libraries for the RADOS block device (Dicode aes256k rebuild)
License:        LGPL-2.1
Provides:       python3-rbd = %{stock_evr}
Requires:       librbd1 = %{epoch}:%{version}-%{release}
Requires:       python3-rados = %{epoch}:%{version}-%{release}
%description -n python3-rbd
Python 3 bindings for librbd, with the cephx aes256k backport.
Unofficial Dicode rebuild.
%files -n python3-rbd
/usr/lib64/python3.6/site-packages/rbd.cpython-36m-x86_64-linux-gnu.so

#--------------------------------------------------------------------------------
%prep
%setup -q -n ceph-aes256k-libs

%build
# nothing to compile: the .so's are prebuilt from the patched tree.

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}/usr/lib64/ceph
mkdir -p %{buildroot}/usr/lib64/python3.6/site-packages

install -m 0755 librados.so.2.0.0     %{buildroot}/usr/lib64/librados.so.2.0.0
ln -s librados.so.2.0.0               %{buildroot}/usr/lib64/librados.so.2
install -m 0755 librbd.so.1.12.0      %{buildroot}/usr/lib64/librbd.so.1.12.0
ln -s librbd.so.1.12.0                %{buildroot}/usr/lib64/librbd.so.1
install -m 0755 libceph-common.so.2   %{buildroot}/usr/lib64/ceph/libceph-common.so.2
install -m 0755 rados.cpython-36m-x86_64-linux-gnu.so %{buildroot}/usr/lib64/python3.6/site-packages/rados.cpython-36m-x86_64-linux-gnu.so
install -m 0755 rbd.cpython-36m-x86_64-linux-gnu.so   %{buildroot}/usr/lib64/python3.6/site-packages/rbd.cpython-36m-x86_64-linux-gnu.so

# Match the stock layout: libceph-common lives in /usr/lib64/ceph, so the
# client libs need RPATH=/usr/lib64/ceph to resolve it (same as upstream).
for f in %{buildroot}/usr/lib64/librados.so.2.0.0 \
         %{buildroot}/usr/lib64/librbd.so.1.12.0 \
         %{buildroot}/usr/lib64/ceph/libceph-common.so.2 \
         %{buildroot}/usr/lib64/python3.6/site-packages/rados.cpython-36m-x86_64-linux-gnu.so \
         %{buildroot}/usr/lib64/python3.6/site-packages/rbd.cpython-36m-x86_64-linux-gnu.so; do
  chrpath -r /usr/lib64/ceph "$f" || chrpath -d "$f" || true
  strip "$f" || true
done

%changelog
* Fri Aug 29 2026 Dicode <info@dicode.nl> - 2:15.2.17-0.dicode.aes256k1
- Repackage the aes256k-backported librados/librbd + python3 bindings as
  drop-in overrides for the stock Ceph 15.2.17 client packages on el7 dom0.
  See the ceph.spec / ceph-15.2.17-aes256k.patch for the crypto backport.
- Unofficial Dicode rebuild; not endorsed by or affiliated with the Ceph project.
