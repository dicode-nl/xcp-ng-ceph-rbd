/* compat shim: crypto/sha2.h was split out in 5.9; on 4.19 use crypto/sha.h */
#ifndef _CEPH_COMPAT_CRYPTO_SHA2_H
#define _CEPH_COMPAT_CRYPTO_SHA2_H
#include <crypto/sha.h>
#endif
