#!/usr/bin/python3
#
# rbd_blkmir.py - detached pre-copy worker for the blkback SXM source.
#
# Spawned by dicode.libs.blkmirror.mirror() as `rbd_blkmir.py <image>`. Runs the
# snapshot -> rbd-diff -> copy-changed-objects loop until a pass converges under
# the threshold (or MAX_ITERS passes), reporting {complete, progress, failed,
# baseline_snap} into the shared status file. Then it keeps looping to stay
# caught up with the still-running guest until blkmirror.finalize() drops the
# `stop` file (at the guest-paused cutover) -- leaving the latest baseline
# snapshot in place for finalize to diff the last delta against.
#
# All the real work (backend diff, krbd snap map, pwrite to the dest /dev/nbdX)
# lives in blkmirror.copy_delta so the paused final pass runs the identical path.
#
# python3, stdlib only (+ dicode.libs).

import os
import sys
import time

from dicode.libs import blkmirror as bm


POLL = 2.0                    # seconds between pre-copy passes once converged


def _stopped(image):
    return os.path.exists(bm._stop_path(image))


def main(image):
    job = bm._read_job(image)
    dconf, pool, ns = job["dconf"], job["pool"], job["ns"]
    nbd_dev = job["nbd_dev"]
    threshold = job["threshold"]
    max_iters = job["max_iters"]

    baseline = None
    first_bytes = None
    converged = False
    iters = 0
    try:
        while not _stopped(image):
            snap = bm.SNAP_PREFIX + "%d-%d" % (int(time.time()), iters)
            be = bm.make_backend(dconf)
            be.snap_create(pool, image, snap, namespace=ns)

            n = bm.copy_delta(dconf, pool, ns, image, baseline, snap, nbd_dev)

            # reap the previous baseline; the new snap becomes the baseline that
            # finalize will diff the final (paused) delta against.
            if baseline:
                try:
                    be.snap_remove(pool, image, baseline, namespace=ns)
                except Exception as e:
                    bm._log("worker snap_remove %s@%s warning: %s"
                            % (image, baseline, e))
            baseline = snap
            iters += 1

            if first_bytes is None:
                first_bytes = n or 1          # size of the initial full copy
            if not converged and (n < threshold or iters >= max_iters):
                converged = True              # xapi may now proceed to cutover
            # progress: fraction of the initial volume of data still moving.
            if converged:
                progress = 1.0
            else:
                progress = max(0.0, min(0.95, 1.0 - float(n) / first_bytes))

            bm.write_status(image, phase=("synced" if converged else "precopy"),
                            progress=progress, complete=converged, failed=False,
                            baseline_snap=baseline, iters=iters, last_bytes=n)
            bm._log("worker %s pass %d: %d bytes%s"
                    % (image, iters, n, " [converged]" if converged else ""))

            if converged:
                # stay caught up (cheap deltas) until finalize drops `stop`.
                time.sleep(POLL)
        bm._log("worker %s stop requested; baseline=%s left for finalize"
                % (image, baseline))
    except Exception as e:
        bm._log("worker %s FAILED: %s" % (image, e))
        try:
            bm.write_status(image, failed=True, phase="failed", error=str(e),
                            baseline_snap=baseline)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: rbd_blkmir.py <image>\n")
        sys.exit(2)
    main(sys.argv[1])
