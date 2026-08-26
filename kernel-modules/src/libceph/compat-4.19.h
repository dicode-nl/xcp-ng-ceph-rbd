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
#include <linux/random.h>		/* get_random_bytes() */
#include <linux/sched/signal.h>		/* signal_pending() */
#include <linux/errno.h>

/* ENOPARAM (internal errno used by fs_parser) added in 5.1 */
#ifndef ENOPARAM
#define ENOPARAM 519
#endif

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

/* ---- compiler: `fallthrough` pseudo-keyword added to kernel in 5.4 --- */
#ifndef fallthrough
#define fallthrough do {} while (0)	/* empty stmt: falls through in switch */
#endif

/* ---- net: trace_sk_data_ready() tracepoint added ~6.0 --------------- */
#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 0, 0)
#define trace_sk_data_ready(sk) do { } while (0)
#endif

/* ---- uio: iov_iter_get_pages() renamed to _get_pages2() in 5.19,
 *      and the new one advances the iterator itself. ------------------- */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 19, 0)
#include <linux/uio.h>
static inline ssize_t iov_iter_get_pages2(struct iov_iter *i,
					  struct page **pages, size_t maxsize,
					  unsigned int maxpages, size_t *start)
{
	ssize_t n = iov_iter_get_pages(i, pages, maxsize, maxpages, start);

	if (n > 0)
		iov_iter_advance(i, n);
	return n;
}
#endif

/* ---- uio: direction/flavour split (5.0) + type-check helpers ---------
 * Before 5.0, iov_iter_{kvec,bvec}() encode the flavour into the direction
 * arg (ITER_KVEC|READ); 5.0+ pass just READ/WRITE and set the flavour inside.
 * Wrap the setters to OR the flavour back in. The self-reference is NOT
 * re-expanded by the preprocessor, so this recurses exactly once.
 * Also provide the iov_iter_is_*() accessors (added ~5.0). There is no
 * ITER_DISCARD on 4.19 — msgr2's skip path is drained manually instead
 * (see set_in_skip()/ceph_tcp_recv()), so iov_iter_is_discard() is false. */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 0, 0)
#include <linux/uio.h>
#define iov_iter_kvec(i, dir, kvec, nr, cnt) \
	iov_iter_kvec((i), ITER_KVEC | (dir), (kvec), (nr), (cnt))
#define iov_iter_bvec(i, dir, bvec, nr, cnt) \
	iov_iter_bvec((i), ITER_BVEC | (dir), (bvec), (nr), (cnt))
#define iov_iter_is_kvec(i)	(((i)->type & ~(READ | WRITE)) == ITER_KVEC)
#define iov_iter_is_bvec(i)	(((i)->type & ~(READ | WRITE)) == ITER_BVEC)
#define iov_iter_is_discard(i)	(false)
#endif

/* ---- net: tcp_sock_set_cork() added in 5.8 -------------------------- */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 8, 0)
#include <linux/socket.h>
#include <linux/in.h>
#include <linux/tcp.h>
#include <net/sock.h>
static inline void tcp_sock_set_cork(struct sock *sk, bool on)
{
	int val = on;

	kernel_setsockopt(sk->sk_socket, IPPROTO_TCP, TCP_CORK,
			  (char *)&val, sizeof(val));
}
#endif

/* ---- net: sendpage_ok() added in 5.6 -------------------------------- */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 6, 0)
#include <linux/mm.h>
static inline bool sendpage_ok(struct page *page)
{
	return !PageSlab(page) && page_count(page) >= 1;
}
#endif

/* ---- highmem: memcpy_page / memcpy_to_page / memcpy_from_page (5.4) -- */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 4, 0)
#include <linux/highmem.h>
static inline void memcpy_page(struct page *dst_page, size_t dst_off,
			       struct page *src_page, size_t src_off, size_t len)
{
	char *dst = kmap_atomic(dst_page);
	char *src = kmap_atomic(src_page);

	memcpy(dst + dst_off, src + src_off, len);
	kunmap_atomic(src);
	kunmap_atomic(dst);
}

static inline void memcpy_to_page(struct page *page, size_t offset,
				  const char *from, size_t len)
{
	char *to = kmap_atomic(page);

	memcpy(to + offset, from, len);
	kunmap_atomic(to);
}

static inline void memcpy_from_page(char *to, struct page *page,
				    size_t offset, size_t len)
{
	char *from = kmap_atomic(page);

	memcpy(to, from + offset, len);
	kunmap_atomic(from);
}
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
