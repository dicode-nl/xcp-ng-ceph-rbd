# Full-stack dom0 smoke test (XCP-ng 8.3, Secure Boot off)

Modules (load in this order — deps go bottom-up):
  krb5.ko  →  libceph.ko (needs krb5 + libcrc32c)  →  rbd.ko (needs libceph)

## Copy to dom0
    scp krb5.ko libceph.ko rbd.ko root@<dom0>:/tmp/

## Load
    modprobe libcrc32c
    insmod /tmp/krb5.ko
    insmod /tmp/libceph.ko
    insmod /tmp/rbd.ko
    lsmod | grep -E 'rbd|libceph|krb5'
    dmesg | tail -n 30

Expected: all three load; `O` (out-of-tree) taint in dmesg is normal (SB off).
NOT expected: "disagrees about version of symbol" or "version magic".

## Functional test (against the Ceph 20.2.4 cluster, aes256k)
NOTE: these modules REPLACE the in-tree ceph modules. Unload the stock ones first
if loaded (rmmod rbd libceph), or blacklist them, before insmod'ing these.

    # map an RBD image (adjust mon addrs / pool / image / keyring)
    echo "<mon-ip>:3300 name=admin,secret=<key> <pool> <image> -" \
        > /sys/bus/rbd/add
    # or with the rbd CLI if installed:
    rbd map <pool>/<image> --id admin -k /etc/ceph/ceph.client.admin.keyring

Watch `dmesg` for the cephx handshake. With the cluster requiring aes256k, a stock
4.19 krbd would fail auth here; these modules should authenticate.

## Unload
    rmmod rbd libceph krb5

## Caveats being validated on real hardware (can't test on build box, kernel 6.14)
- msgr2 skip/discard drain path (patches/0002) — the one non-trivial runtime path.
- set_capacity_and_notify: full backport incl. the udev RESIZE=1 uevent, so live
  `rbd resize` is seen by userspace (uses GENHD_FL_UP for the liveness check).
- device_add_disk groups arg: NO functional loss — rbd passes NULL there anyway.
  All rbd sysfs is registered via device_add() (/sys/bus/rbd/devices/N/) and the
  bus group (/sys/bus/rbd/), which are untouched.
