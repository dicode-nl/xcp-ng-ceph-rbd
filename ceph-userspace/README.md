# ceph-userspace — backported aes256k Ceph client libraries for XCP-ng 8.3 dom0

Backports the CephX **aes256k** key type (AES256-CTS-HMAC-SHA384-192, RFC 8009
enctype 20 — the CVE-2025-30156 fix used by Ceph 20.2.4 / Tentacle) into the
**userspace** Ceph client (`librados` / `librbd` + python3 bindings) so an
XCP-ng 8.3 dom0 can drive the cluster's *control plane* (`rbd create/snap/clone`,
image metadata, namespaces, diff) with a **type-2 (`AgD…`) cephx key** — without
the Ceph mgr dashboard REST proxy.

This is the userspace companion to [`../kernel-modules`](../kernel-modules),
which does the same for the in-kernel client (the krbd **data** path). Kernel =
data path; this = control plane.

> **Unofficial Dicode rebuild.** Not endorsed by or affiliated with the Ceph
> project. The RPMs keep the stock package names but carry a `.dicode.aes256kN`
> release suffix and `Vendor: Dicode`.

## Why

XCP-ng 8.3 dom0 is **el7 (CentOS 7.5)** and ships Ceph **Octopus 15.2.17**
userspace. Octopus predates aes256k, so `rados.connect()` with a type-2 key
fails hard:

```
auth: failed to decode key 'AgD…'  →  rados … InvalidArgumentError [errno 22]
```

A native el7 build of Ceph **20.2.4** is impossible (it needs python ≥3.9 /
gcc ≥13; el7 caps at 3.6 / 11). So instead we **backport just the cephx aes256k
handler** onto the Octopus 15.2.17 that already builds on el7. aes256k only
touches the cephx **auth** crypto — the msgr2 wire cipher stays standard
AES-128-GCM, which Octopus already speaks — so the change is small and additive.

## What's here

| path | what |
|------|------|
| `patches/ceph-15.2.17-aes256k.patch` | the source backport (8 files, ~570 added lines) |
| `rpm/ceph-aes256k-libs.spec` | **slim override spec** — repackages the prebuilt client `.so`s (recommended) |
| `rpm/ceph.spec.dicode` | the stock Ceph 15.2.17 `ceph.spec` with the patch wired in (`Patch1000`, dicode release/vendor/changelog) — for a full `rpmbuild` |

## The patch (route A — pure OpenSSL 3.0)

Ceph's `CryptoAES256KRB5` is hand-rolled RFC 8009:

```
Ke = KDF(secret, usage, 0xAA, 32)     Ki = KDF(secret, usage, 0x55, 24)
KDF(K,usage,t,k) = HMAC-SHA384(K, 00000001 | usage_BE32 | t | 00 | (k*8)_BE32)[0:k]
token = AES-256-CBC-CTS(CS3, Ke, IV=0, confounder16 | plaintext)
        || HMAC-SHA384(Ki, IV16 | ciphertext)[0:24]
```

We take the upstream handler **verbatim** (OpenSSL 3.0 `EVP_CIPHER_fetch
"AES-256-CBC-CTS"` + `OSSL_CIPHER_PARAM_CTS_MODE "CS3"`, `HMAC(EVP_sha384)`) —
no libkrb5 dependency — because a rebuild against the dom0's own OpenSSL 3.0.9
links `libcrypto.so.3`, which has the CS3 provider (the *stock* download.ceph.com
librados links the el7 `libcrypto.so.10` / OpenSSL 1.0.2, which does not).

What the patch touches, all **additive** (type-1 AES-128 keys ignore the new
`usage` arg, so existing behaviour is unchanged):

1. `src/include/ceph_fs.h` — `#define CEPH_CRYPTO_AES256KRB5 0x2` (the key's u16 type field).
2. `src/auth/Crypto.h` — `CryptoKeyHandler` gains `default_usage` + `encrypt_ext/decrypt_ext(cct,usage,…)` (defaults delegate to the plain encrypt/decrypt); `CryptoKey` gains the passthroughs; `hmac_sha256` made `const`.
3. `src/auth/Crypto.cc` — the `CryptoAES256KRB5` handler (Ke/Ki derived **lazily per-usage** under a mutex, so no `get_key_handler_ext`/usages-vector refactor) + the `CryptoHandler::create` factory case.
4. `src/auth/cephx/CephxProtocol.h` — the 8 `CEPHX_KEY_USAGE_*` constants, the `usage` overloads of `encode_encrypt/decode_decrypt[_enc_bl]`, and `encode_hash` (keyed HMAC-SHA256).
5. cephx call sites route through the usage overloads — `CephxProtocol.cc` (incl. the `cephx_calc_client_server_challenge` type-switch: AES→`encode_encrypt`, aes256k→`encode_hash`, because the random confounder makes ciphertext non-deterministic), `CephxClientHandler.cc`, `CephxKeyServer.cc`, `CephxServiceHandler.cc`.

The exact `CEPHX_KEY_USAGE_*` per call site must match the cluster; they were
taken from upstream ceph `src/auth/`.

## Building

Needs an el7 Ceph-15 build env — the `ceph-build` container
(`ghcr.io/xcp-ng/xcp-ng-build-env:8.3`), which has the right runtime libs
(openssl 3.0.9 + krb5 1.21.3, matching dom0). Its XCP-ng **base** repo actually
ships the full `devtoolset-11` (gcc 11.2, C++17):

```sh
# 1. toolchain + build deps
yum install -y devtoolset-11-gcc devtoolset-11-gcc-c++ cmake3 ninja-build \
               --enablerepo=epel yasm chrpath
source scl_source enable devtoolset-11
# the ceph BuildRequires (yum-builddep aborts on any miss, so filter+skip-broken):
rpmspec -q --buildrequires SPECS/ceph.spec \
  | grep -vE 'devtoolset-8|gperftools|junit|libibverbs|librabbitmq|librdmacm|selinux-policy' \
  | xargs yum install -y --skip-broken --enablerepo=epel

# 2. patch the pristine Octopus 15.2.17 source tree, then configure (client libs only)
patch -p1 < patches/ceph-15.2.17-aes256k.patch
mkdir build && cd build
cmake3 -GNinja -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_C_COMPILER=$(which gcc) -DCMAKE_CXX_COMPILER=$(which g++) \
  -DWITH_RADOSGW=OFF -DWITH_RDMA=OFF -DWITH_SELINUX=OFF -DWITH_TESTS=OFF \
  -DWITH_MANPAGE=OFF -DWITH_LTTNG=OFF -DWITH_BABELTRACE=OFF -DALLOCATOR=libc \
  -DWITH_SYSTEM_BOOST=OFF -DWITH_PYTHON3=3.6 -DWITH_MGR=OFF -DWITH_CEPHFS=OFF \
  -DWITH_KRBD=OFF -DWITH_LIBCEPHSQLITE=OFF -DWITH_SYSTEMD=OFF -DWITH_FUSE=OFF \
  -DWITH_DPDK=OFF -DWITH_SPDK=OFF ..

# 3. bundled boost is an ExternalProject — build it FIRST, then the libs
ninja -j$(nproc) Boost
ninja -j$(nproc) librados librbd cython_rados cython_rbd
```

Then package (**slim override**, recommended — repackages the `build/lib` `.so`s,
does NOT rebuild the world):

```sh
# stage the artifacts as the spec's Source0, then:
rpmbuild -bb rpm/ceph-aes256k-libs.spec
# → librados2, librbd1, python3-rados, python3-rbd  (…-0.<dist>.dicode.aes256k1)
```

`ceph.spec.dicode` is the alternative: the full official spec with the patch
applied via `Patch1000` (auto-applied by its `%autosetup -p1`). It produces
byte-correct packages but builds the whole of Ceph and pins `devtoolset-8` +
rgw/rdma/selinux BuildRequires — heavier, and not needed for the client libs.

## Install (dom0)

The `.dicode.aes256k1` release sorts **above** the stock `2:15.2.17-0.el7`, and
the package names are unchanged, so it's a clean override:

```sh
rpm -Uvh librados2-*.rpm librbd1-*.rpm python3-rados-*.rpm python3-rbd-*.rpm
```

Uninstall/downgrade to the stock Ceph repo packages reverts. The rebuild links
the dom0's own `libcrypto.so.3` (openssl 3.0.9) and keeps `RPATH=/usr/lib64/ceph`
for `libceph-common.so.2`, matching the stock layout. The lttng tracepoint libs
(`librados_tp`/`librbd_tp`) are not shipped (built `WITH_LTTNG=OFF`); the stock
ones are removed on upgrade (harmless — they're only loaded under LTTNG tracing).

## Status

Validated end-to-end against a live Ceph **20.2.4** cluster (msgr2 **secure** to
a secure-only mon) with a real type-2 `client.xcp` key: `rados` connect + fsid /
stats, and `rbd` `namespace_list` / `list` / `Image.size` — from the stripped,
rpath-adjusted packaged libs. The stock-Octopus `errno 22 / failed to decode key`
is gone.

> Octopus has no `ms_mode` config key (that's a Tentacle-ism) — use
> `ms_client_mode=secure` on the client instead.
