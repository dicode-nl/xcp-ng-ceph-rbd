# kernel-modules — backported aes256k / msgr2-secure Ceph client for XCP-ng 8.3

Backports the in-kernel Ceph client to the XCP-ng 8.3 dom0 kernel (Linux
`4.19.0+1`) so it speaks **msgr2 secure** and understands the **aes256k** CephX
key type (AES256-CTS-HMAC-SHA384-192) needed by Ceph 20.2.4 (Tentacle) /
CVE-2025-30156. Source base: `net/ceph` + `crypto/krb5` from CentOS Stream 9
kernel 5.14, and `drivers/block/rbd.c` from mainline 5.14 — made to build on 4.19
via a force-included compat header and a handful of vendored headers. The only
real edits to the upstream sources are the diffs in **`patches/`** (0001-0004).

## The three modules (and their RPMs)

| module      | RPM                   | install path                         | role |
|-------------|-----------------------|--------------------------------------|------|
| `krb5.ko`   | `krb5-module`         | `/lib/modules/4.19.0+1/extra/`       | aes256k crypto (no in-tree equiv on 4.19) |
| `libceph.ko`| `libceph-module-alt`  | `/lib/modules/4.19.0+1/override/`    | msgr1/msgr2 secure + aes256k cephx |
| `rbd.ko`    | `rbd-module-alt`      | `/lib/modules/4.19.0+1/override/`    | krbd: namespaces, object-map/fast-diff |

`override/` has higher depmod priority than the in-tree modules, so these win;
uninstalling reverts to stock. **Dependency chain (ABI-locked, bidirectional):**
`rbd-module-alt` ⇄ `libceph-module-alt` → `krb5-module`, all pinned to the same
version — our libceph/rbd have matched symbol CRCs and MUST be installed together
(the kernel refuses to load a stock↔ours mix). Install all three in one `rpm`
transaction.

## `src/` layout

- `src/krb5/`    — the `crypto/krb5` backport → `krb5.ko` (see its `Makefile`).
- `src/libceph/` — `net/ceph` + vendored `include/` + `compat-4.19.h` → `libceph.ko`.
- `src/rbd/`     — `drivers/block/rbd.c` + vendored `include/` → `rbd.ko`.

Each module force-includes `compat-4.19.h` (additive, version-guarded shims) and
carries the vendored headers it needs under `include/`. The `patches/` diffs are
the *only* real edits to upstream sources (≈102 lines across 3 files); everything
else is verbatim CS9/mainline, kept in `src/` so the tree is self-contained.

## Building the `.ko`

Needs the `4.19.0+1` kernel-devel headers (the `ceph-build` container). Build
order matters — **krb5 → libceph → rbd** (rbd links against libceph's
`Module.symvers`):

```sh
make -C src/krb5    KDIR=/usr/src/kernels/4.19.0+1-x86_64
make -C src/libceph KDIR=/usr/src/kernels/4.19.0+1-x86_64   # picks up krb5 symbols
make -C src/rbd     KDIR=/usr/src/kernels/4.19.0+1-x86_64 \
                    KBUILD_EXTRA_SYMBOLS=$PWD/src/libceph/Module.symvers
```

(The Makefiles keep the original build-env paths; adjust `KDIR` /
`KBUILD_EXTRA_SYMBOLS` to your layout.) Smoke-tests: `rpm/smoketests/`.

## Building the RPMs

`rpm/rbd-module-alt.spec` packages the three **prebuilt** `.ko` (Source0..2 =
`krb5.ko`, `libceph.ko`, `rbd.ko`) — it does no compilation itself (a full rpm
strip would drop `.modinfo`/vermagic and break loading). Drop the freshly built
`.ko` into the rpmbuild `SOURCES/`, then:

```sh
rpmbuild --define "_topdir $PWD/rpmbuild" -bb rpm/rbd-module-alt.spec
# -> RPMS/x86_64/{krb5-module,libceph-module-alt,rbd-module-alt}-1.0.0-1.xcpng8.3.x86_64.rpm
```

Install on dom0 (one transaction, so the circular dep resolves):

```sh
rpm -ivh krb5-module-*.rpm libceph-module-alt-*.rpm rbd-module-alt-*.rpm
modprobe rbd   # pulls the whole stack
```
