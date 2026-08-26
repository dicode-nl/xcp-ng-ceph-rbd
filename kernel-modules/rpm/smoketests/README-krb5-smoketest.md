# krb5.ko — dom0 load smoke-test (XCP-ng 8.3)

This is the vendored in-kernel Kerberos5 crypto library (the aes256k basis for
libceph), built against 4.19.0+1. Loading **only** this module does nothing
functional — it is a library that libceph calls into. But loading it proves the
whole load chain (vermagic / symbol-CRC / no signing required) on your real dom0.

## Copy to dom0 (from the build box)
    scp krb5.ko root@<dom0>:/tmp/

## Load (on the dom0)
    insmod /tmp/krb5.ko          # direct file load; modprobe only after install + depmod
    lsmod | grep krb5            # expect: krb5  <size>  0
    dmesg | tail -n 15

### What to expect in dmesg
- Possibly: "module verification failed: signature and/or required key missing - tainting kernel"
  → harmless; that is the `O` taint of an out-of-tree module (Secure Boot is off).
- NO "disagrees about version of symbol" and NO "version magic ... should be ...".

## Unload
    rmmod krb5

## If it does NOT load
- "disagrees about version of symbol X" → symbol-CRC mismatch: your dom0 runs a
  different 4.19.19-8.0.46.x sub-build than the one it was built against (.46.10).
  Send the output of `uname -r` + `cat /proc/version` and rebuild against your
  exact sub-build.
- "Required key not available" / -EKEYREJECTED → Secure Boot is on after all; then
  the modules must be signed.

Runtime crypto (authenc, cts, cbc, aes, hmac, sha384) is loaded by the kernel
automatically once libceph requests the rfc8009 enctype — not needed for this
bare load test.
