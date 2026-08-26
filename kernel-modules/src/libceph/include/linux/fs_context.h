/* SPDX-License-Identifier: GPL-2.0 */
/*
 * compat: linux/fs_context.h — minimal subset for the ceph/rbd fs_parser
 * backport on XCP-ng 8.3 (4.19). The real fs_context/fs_parameter machinery
 * arrived in 5.1; 4.19 has none of it. We provide only the types the parameter
 * parser needs, and redirect the fs_context error-log ring buffer to printk
 * (so no struct fs_context / fc_log is required).
 */
#ifndef _CEPH_COMPAT_FS_CONTEXT_H
#define _CEPH_COMPAT_FS_CONTEXT_H

#include <linux/kernel.h>
#include <linux/errno.h>
#include <linux/printk.h>

struct filename;
struct file;
struct fc_log;

enum fs_value_type {
	fs_value_is_undefined,
	fs_value_is_flag,		/* Value not given a value */
	fs_value_is_string,		/* Value is a string */
	fs_value_is_blob,		/* Value is a binary blob */
	fs_value_is_filename,		/* Value is a filename* + dirfd */
	fs_value_is_file,		/* Value is a file* */
};

/* Configuration parameter. */
struct fs_parameter {
	const char		*key;		/* Parameter name */
	enum fs_value_type	type:8;		/* The type of value here */
	union {
		char		*string;
		void		*blob;
		struct filename	*name;
		struct file	*file;
	};
	size_t	size;
	int	dirfd;
};

struct p_log {
	const char *prefix;
	struct fc_log *log;
};

/*
 * Logging: upstream routes these to a per-fs_context ring buffer via logfc().
 * On 4.19 we simply emit to the kernel log, using the p_log prefix.
 */
#define __ceph_plog(p, kern, fmt, ...) \
	printk(kern "%s: " fmt "\n", \
	       ((p) && (p)->prefix) ? (p)->prefix : "fs", ##__VA_ARGS__)

#define warn_plog(p, fmt, ...)  __ceph_plog(p, KERN_WARNING, fmt, ##__VA_ARGS__)
#define error_plog(p, fmt, ...) __ceph_plog(p, KERN_ERR, fmt, ##__VA_ARGS__)
#define info_plog(p, fmt, ...)  __ceph_plog(p, KERN_INFO, fmt, ##__VA_ARGS__)
#define inval_plog(p, fmt, ...) ({ error_plog(p, fmt, ##__VA_ARGS__); -EINVAL; })

/* fs_context-based variants: ignore the (unavailable) fs_context, just print. */
#define errorf(fc, fmt, ...) pr_err(fmt "\n", ##__VA_ARGS__)
#define warnf(fc, fmt, ...)  pr_warn(fmt "\n", ##__VA_ARGS__)
#define infof(fc, fmt, ...)  pr_info(fmt "\n", ##__VA_ARGS__)
#define invalf(fc, fmt, ...) ({ pr_err(fmt "\n", ##__VA_ARGS__); -EINVAL; })

#endif /* _CEPH_COMPAT_FS_CONTEXT_H */
