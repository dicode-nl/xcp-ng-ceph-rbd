/* SPDX-License-Identifier: GPL-2.0 */
/*
 * compat-4.19.h — shims to build CentOS Stream 9 (5.14) ceph/krb5 code
 * against the XCP-ng 8.3 dom0 kernel (4.19.0+1).
 *
 * Force-included via `ccflags-y += -include .../compat-4.19.h`.
 * All shims are version-guarded so the same header is a no-op on >= the
 * version that introduced each API (safe to reuse across build trees).
 */
#ifndef _CEPH_COMPAT_4_19_H
#define _CEPH_COMPAT_4_19_H

#include <linux/version.h>

/* Include-graph drift: several backported files rely on symbols that were
 * pulled in transitively upstream but need an explicit include on 4.19.
 * These headers exist on all supported versions, so include unconditionally. */
#include <linux/random.h>	/* get_random_bytes() */

/* ---- mm: kfree_sensitive() was kzfree() before 5.10 ------------------ */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 10, 0)
#include <linux/slab.h>
#ifndef kfree_sensitive
#define kfree_sensitive(p) kzfree(p)
#endif
#endif

/* ---- crypto: sync skcipher API introduced in 5.0 -------------------- */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 0, 0)
#include <crypto/skcipher.h>

/* On 4.19 a "sync" skcipher is just a normal (synchronous) skcipher. */
#define crypto_sync_skcipher crypto_skcipher

static inline struct crypto_sync_skcipher *
crypto_alloc_sync_skcipher(const char *alg_name, u32 type, u32 mask)
{
	/* exclude async implementations, matching upstream semantics */
	return crypto_alloc_skcipher(alg_name, type, mask | CRYPTO_ALG_ASYNC);
}

#define crypto_free_sync_skcipher(tfm)          crypto_free_skcipher(tfm)
#define crypto_sync_skcipher_setkey(tfm, k, l)  crypto_skcipher_setkey(tfm, k, l)
#define crypto_sync_skcipher_blocksize(tfm)     crypto_skcipher_blocksize(tfm)
#define crypto_sync_skcipher_ivsize(tfm)        crypto_skcipher_ivsize(tfm)
#define crypto_sync_skcipher_reqtfm(req)        crypto_skcipher_reqtfm(req)

/* SKCIPHER_REQUEST_ON_STACK exists in 4.19; the *sync* variant does not. */
#ifndef SYNC_SKCIPHER_REQUEST_ON_STACK
#define SYNC_SKCIPHER_REQUEST_ON_STACK(name, tfm) \
	SKCIPHER_REQUEST_ON_STACK(name, tfm)
#endif

static inline void
skcipher_request_set_sync_tfm(struct skcipher_request *req,
			      struct crypto_sync_skcipher *tfm)
{
	skcipher_request_set_tfm(req, tfm);
}
#endif /* < 5.0 */

/* ---- scatterlist: SG_MITER_LOCAL (kmap_local) is a recent addition --
 * It is an enum value (invisible to the preprocessor), so we cannot use
 * #ifndef; version-guard instead. On 4.19 it does not exist; ORing 0 makes
 * sg_miter fall back to the default (sleepable kmap) mapping, which is safe
 * in the process-context callers used here. This header is only ever
 * force-included into the 4.19 target build.
 */
#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 6, 0)
#define SG_MITER_LOCAL 0
#endif

/* ---- crypto: crypto_shash_tfm_digest() added in 5.8 ----------------- */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 8, 0)
#include <crypto/hash.h>
static inline int crypto_shash_tfm_digest(struct crypto_shash *tfm,
					  const u8 *data, unsigned int len,
					  u8 *out)
{
	SHASH_DESC_ON_STACK(desc, tfm);
	int err;

	desc->tfm = tfm;
	err = crypto_shash_digest(desc, data, len, out);
	shash_desc_zero(desc);
	return err;
}
#endif

#endif /* _CEPH_COMPAT_4_19_H */
