"""
rbd_backend.py - control-plane backends for the Ceph RBD SR.

All volume-management ops go through an RbdBackend. Two are implemented,
selected by device-config `backend=`:

  * backend=rest (default) -- talks to the ceph-mgr *dashboard* REST API over
    HTTPS (JWT auth). Needs nothing on the host but python stdlib, so it works
    on an el7 dom0 that has no aes256k-capable ceph userspace. The cephx key is
    used ONLY for the datapath (kernel rbd map, see rbd_sysfs.py), never here.
  * backend=local -- uses the librbd/librados python bindings (python3-rbd,
    python3-rados). For hosts that DO have a working ceph userspace; no
    dashboard required. Cluster access reuses the same mon_host/user/key as the
    datapath, or an explicit ceph_conf. The bindings are imported LAZILY (only
    when backend=local is selected), so the default REST path stays import-clean
    on a dom0 that has no ceph userspace.

Both return identical dict shapes so the rest of the driver is backend-agnostic.
REST contract validated against Ceph 20.2.4 (Tentacle); see RestBackend.VERSIONS.

REST path is stdlib-only (urllib); the local backend imports rbd/rados lazily.
python 3.6+ (matches XCP-ng 8.3 dom0 sm).
"""

import json
import math
import ssl
import time
import urllib.parse
import urllib.request
import urllib.error

try:
    import util  # XCP-ng sm logging; absent when unit-testing off-box
    def _log(msg):
        util.SMlog("[rbd_backend] " + msg)
except Exception:  # pragma: no cover
    def _log(msg):
        pass


class RbdBackendError(Exception):
    """Backend operation failed. .not_supported => map to Unimplemented so the
    toolstack can fall back (critical for VM.revert; see ceph-rbdsr-vdi-revert)."""
    def __init__(self, message, status=None, not_supported=False, not_found=False):
        super(RbdBackendError, self).__init__(message)
        self.status = status
        self.not_supported = not_supported
        self.not_found = not_found


class RbdBackend(object):
    """Interface every control-plane backend implements. size args are BYTES."""

    def list_images(self, pool, namespace=""):
        raise NotImplementedError
    def image_info(self, pool, image, namespace=""):
        raise NotImplementedError
    def create(self, pool, image, size, features, namespace="", obj_size=None):
        raise NotImplementedError
    def remove(self, pool, image, namespace=""):
        raise NotImplementedError
    def resize(self, pool, image, size, features=None, namespace=""):
        raise NotImplementedError
    def snap_create(self, pool, image, snap, namespace=""):
        raise NotImplementedError
    def snap_remove(self, pool, image, snap, namespace=""):
        raise NotImplementedError
    def snap_set_protected(self, pool, image, snap, protected, namespace=""):
        raise NotImplementedError
    def snap_rollback(self, pool, image, snap, namespace=""):
        raise NotImplementedError
    def clone(self, pool, image, snap, dst_pool, dst_image, features=None,
              namespace="", dst_namespace=""):
        raise NotImplementedError
    def flatten(self, pool, image, namespace=""):
        raise NotImplementedError
    def image_rename(self, pool, image, new_name, namespace=""):
        raise NotImplementedError
    def snap_rename(self, pool, image, snap, new_snap, namespace=""):
        raise NotImplementedError
    def image_diff(self, pool, image, from_snap, to_snap, offset=0, length=None,
                   whole_object=True, namespace=""):
        """Changed extents between two states of an image (for CBT).
        from_snap=None -> from image creation; to_snap=None -> the live head.
        -> [{'offset','length','exists'}]. May raise not_supported."""
        raise NotImplementedError
    def pool_stats(self, pool):
        """-> {'total':bytes,'used':bytes,'free':bytes}."""
        raise NotImplementedError
    def namespace_list(self, pool):
        """-> list of namespace names."""
        raise NotImplementedError
    def namespace_create(self, pool, namespace):
        raise NotImplementedError
    def namespace_remove(self, pool, namespace):
        raise NotImplementedError


def make_backend(dconf):
    """Factory from SR device-config. backend=rest (default) | local."""
    kind = dconf.get("backend", "rest").lower()
    if kind == "rest":
        url = dconf.get("api_url")
        if not url:
            raise RbdBackendError("device-config:api_url is required for backend=rest")
        return RestBackend(
            api_url=url,
            user=dconf.get("api_user", "admin"),
            secret=dconf.get("api_secret", ""),
            tls_verify=dconf.get("api_tls_verify", "false").lower() in ("1", "true", "yes"),
        )
    if kind in ("local", "cli", "librbd", "rbd"):
        return LocalBackend(
            mon_host=dconf.get("mon_host"),
            user=dconf.get("user") or dconf.get("api_user"),
            key=dconf.get("key"),
            ceph_conf=dconf.get("ceph_conf"),
        )
    raise RbdBackendError("unknown backend %r (use 'rest' or 'local')" % kind)


class RestBackend(RbdBackend):
    """ceph-mgr dashboard REST API backend. Validated on Ceph 20.2.4."""

    # Default Accept-version per endpoint template (from live probe). The client
    # still auto-negotiates on 415, so these are fast-paths, not hard-coded walls.
    VERSIONS = {
        "GET /api/block/image": "2.0",
        "POST /api/block/image": "1.0",
        "GET /api/block/image/{spec}": "1.0",
        "PUT /api/block/image/{spec}": "1.0",
        "DELETE /api/block/image/{spec}": "1.0",
        "POST /api/block/image/{spec}/snap": "1.0",
        "PUT /api/block/image/{spec}/snap/{name}": "1.0",
        "DELETE /api/block/image/{spec}/snap/{name}": "1.0",
        "POST /api/block/image/{spec}/snap/{name}/rollback": "1.0",
        "POST /api/block/image/{spec}/snap/{name}/clone": "1.0",
        "POST /api/block/image/{spec}/flatten": "1.0",
        "GET /api/block/image/{spec}/diff": "1.0",
        "GET /api/pool": "1.0",
        "GET /api/block/pool/{pool}/namespace": "1.0",
        "POST /api/block/pool/{pool}/namespace": "1.0",
        "DELETE /api/block/pool/{pool}/namespace/{ns}": "1.0",
    }
    CAND_VERSIONS = ["1.0", "2.0", "3.0", "1.1", "0.1", "4.0"]
    # flatten blocks the HTTP request until the ceph task completes, which for a
    # multi-GB CoW child can take minutes -> it must NOT ride the short timeout.
    LONG_TIMEOUT = 6 * 3600

    def __init__(self, api_url, user, secret, tls_verify=False, timeout=60):
        self.base = api_url.rstrip("/")
        self.user = user
        self.secret = secret
        self.timeout = timeout
        self._token = None
        self._ver = dict(self.VERSIONS)
        if tls_verify:
            self._ctx = ssl.create_default_context()
        else:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    # ---- low-level HTTP ----
    def _http(self, method, path, body, version, timeout=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.ceph.api.v%s+json" % version
                       if version else "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("Authorization", "Bearer " + self._token)
        try:
            resp = urllib.request.urlopen(
                req, context=self._ctx,
                timeout=timeout if timeout is not None else self.timeout)
            txt, status = resp.read().decode(), resp.getcode()
        except urllib.error.HTTPError as e:
            txt, status = e.read().decode(), e.code
        except urllib.error.URLError as e:
            raise RbdBackendError("dashboard unreachable at %s: %s" % (self.base, e))
        try:
            parsed = json.loads(txt) if txt else None
        except ValueError:
            parsed = txt
        return status, parsed

    def _login(self):
        st, bd = self._http("POST", "/api/auth",
                            {"username": self.user, "password": self.secret}, "1.0")
        if st not in (200, 201) or not isinstance(bd, dict) or "token" not in bd:
            raise RbdBackendError("dashboard login failed (HTTP %s): %s" % (st, bd), status=st)
        self._token = bd["token"]
        _log("dashboard login OK")

    def _call(self, method, path, tmpl, body=None, ok=(200, 201, 202, 204), timeout=None):
        """Versioned call with 401 re-login and 415 version auto-negotiation."""
        if self._token is None:
            self._login()
        tried_login = False
        # version order: cached fast-path first, then remaining candidates
        first = self._ver.get(tmpl)
        order = ([first] if first else []) + [v for v in self.CAND_VERSIONS if v != first]
        last = None
        for v in order:
            st, bd = self._http(method, path, body, v, timeout=timeout)
            if st == 401 and not tried_login:
                tried_login = True
                self._login()
                st, bd = self._http(method, path, body, v, timeout=timeout)
            if st == 415:
                last = (st, bd, v)
                continue
            if st in ok:
                self._ver[tmpl] = v
                return bd
            # a definitive non-version error
            self._raise(method, tmpl, st, bd)
        # exhausted versions on 415
        st, bd, v = last if last else (None, None, None)
        self._raise(method, tmpl, st, bd)

    @staticmethod
    def _raise(method, tmpl, st, bd):
        detail = bd
        if isinstance(bd, dict):
            detail = bd.get("detail") or bd.get("status") or json.dumps(bd)
        # RBD "image/snap not found" == errno 2
        nf = False
        code = bd.get("code") if isinstance(bd, dict) else None
        if st in (404,) or str(code) == "2" or (isinstance(detail, str) and "not found" in detail.lower()):
            nf = True
        ns = st in (404, 405, 501) or (isinstance(detail, str) and "not found" in detail.lower() and method in ("POST", "PUT"))
        raise RbdBackendError("%s %s -> HTTP %s: %s" % (method, tmpl, st, detail),
                              status=st, not_found=nf, not_supported=(st in (405, 501)))

    # ---- helpers ----
    @staticmethod
    def _spec(pool, image, namespace=""):
        s = "%s/%s/%s" % (pool, namespace, image) if namespace else "%s/%s" % (pool, image)
        return urllib.parse.quote(s, safe="")

    @staticmethod
    def _feats(features):
        if features is None:
            return None
        if isinstance(features, str):
            return [f.strip() for f in features.split(",") if f.strip()]
        return list(features)

    # ---- RbdBackend API ----
    def list_images(self, pool, namespace=""):
        bd = self._call("GET", "/api/block/image?pool_name=%s" % urllib.parse.quote(pool),
                        "GET /api/block/image")
        # v2.0 returns [{pool_name, value:[images...]}] or a paginated dict
        out = []
        if isinstance(bd, dict) and "value" in bd:
            bd = bd["value"]
        if isinstance(bd, list):
            for grp in bd:
                if isinstance(grp, dict) and "value" in grp and grp.get("pool_name") in (pool, None):
                    out.extend(grp["value"])
                elif isinstance(grp, dict) and "name" in grp:
                    out.append(grp)
        # The dashboard returns every image in the pool across ALL namespaces
        # (each carries a 'namespace' field); filter to the one we asked for so a
        # second SR on the same pool doesn't leak the other SR's VDIs into scan.
        want = namespace or ""
        return [im for im in out if (im.get("namespace") or "") == want]

    def image_info(self, pool, image, namespace=""):
        return self._call("GET", "/api/block/image/%s" % self._spec(pool, image, namespace),
                          "GET /api/block/image/{spec}")

    def create(self, pool, image, size, features, namespace="", obj_size=None):
        body = {"pool_name": pool, "namespace": namespace or "", "name": image,
                "size": int(size), "features": self._feats(features) or [],
                "configuration": {}}
        if obj_size:
            body["obj_size"] = int(obj_size)
        return self._call("POST", "/api/block/image", "POST /api/block/image", body)

    def remove(self, pool, image, namespace=""):
        return self._call("DELETE", "/api/block/image/%s" % self._spec(pool, image, namespace),
                          "DELETE /api/block/image/{spec}")

    def resize(self, pool, image, size, features=None, namespace=""):
        body = {"size": int(size), "configuration": {}}
        f = self._feats(features)
        if f is not None:
            body["features"] = f
        return self._call("PUT", "/api/block/image/%s" % self._spec(pool, image, namespace),
                          "PUT /api/block/image/{spec}", body)

    def image_meta_set(self, pool, image, metadata, size, namespace=""):
        # Whole-dict replace (callers read-modify-write). The dashboard image
        # edit only UPSERTs the keys it's given and removes a key only when its
        # value is null (RbdImageMetadataService.set_metadata: None -> remove).
        # So to actually drop keys, send an explicit null for every current key
        # that's absent from the desired dict. (image_info() metadata already
        # excludes conf_* config keys, so those are never touched.)
        metadata = dict(metadata or {})
        try:
            cur = self.image_info(pool, image, namespace=namespace).get("metadata") or {}
        except RbdBackendError:
            cur = {}
        for k in cur:
            if k not in metadata:
                metadata[k] = None
        return self._call("PUT", "/api/block/image/%s" % self._spec(pool, image, namespace),
                          "PUT /api/block/image/{spec}",
                          {"size": int(size), "metadata": metadata, "configuration": {}})

    def snap_create(self, pool, image, snap, namespace=""):
        # mirrorImageSnapshot MUST be present or the dashboard 500s (validated).
        return self._call("POST", "/api/block/image/%s/snap" % self._spec(pool, image, namespace),
                          "POST /api/block/image/{spec}/snap",
                          {"snapshot_name": snap, "mirrorImageSnapshot": False})

    def snap_remove(self, pool, image, snap, namespace=""):
        return self._call("DELETE", "/api/block/image/%s/snap/%s"
                          % (self._spec(pool, image, namespace), urllib.parse.quote(snap, safe="")),
                          "DELETE /api/block/image/{spec}/snap/{name}")

    def snap_set_protected(self, pool, image, snap, protected, namespace=""):
        return self._call("PUT", "/api/block/image/%s/snap/%s"
                          % (self._spec(pool, image, namespace), urllib.parse.quote(snap, safe="")),
                          "PUT /api/block/image/{spec}/snap/{name}",
                          {"is_protected": bool(protected)})

    def snap_rollback(self, pool, image, snap, namespace=""):
        return self._call("POST", "/api/block/image/%s/snap/%s/rollback"
                          % (self._spec(pool, image, namespace), urllib.parse.quote(snap, safe="")),
                          "POST /api/block/image/{spec}/snap/{name}/rollback", {})

    def clone(self, pool, image, snap, dst_pool, dst_image, features=None,
              namespace="", dst_namespace=""):
        body = {"child_pool_name": dst_pool, "child_image_name": dst_image,
                "child_namespace": dst_namespace or "", "configuration": {}}
        f = self._feats(features)
        if f is not None:
            body["features"] = f
        return self._call("POST", "/api/block/image/%s/snap/%s/clone"
                          % (self._spec(pool, image, namespace), urllib.parse.quote(snap, safe="")),
                          "POST /api/block/image/{spec}/snap/{name}/clone", body)

    def flatten(self, pool, image, namespace=""):
        # Synchronous on the dashboard side (blocks until the ceph flatten task
        # finishes) -> use the long timeout, never the 60s control-plane one.
        return self._call("POST", "/api/block/image/%s/flatten" % self._spec(pool, image, namespace),
                          "POST /api/block/image/{spec}/flatten", {}, timeout=self.LONG_TIMEOUT)

    def image_rename(self, pool, image, new_name, namespace=""):
        # Same PUT endpoint as resize, but with a name field. Child CoW links are
        # by parent image *id*, so a rename does not detach clones (validated).
        return self._call("PUT", "/api/block/image/%s" % self._spec(pool, image, namespace),
                          "PUT /api/block/image/{spec}", {"name": new_name})

    def snap_rename(self, pool, image, snap, new_snap, namespace=""):
        # Child CoW links are by parent snap *id*, so a snap rename keeps clones.
        return self._call("PUT", "/api/block/image/%s/snap/%s"
                          % (self._spec(pool, image, namespace), urllib.parse.quote(snap, safe="")),
                          "PUT /api/block/image/{spec}/snap/{name}", {"new_snap_name": new_snap})

    def image_diff(self, pool, image, from_snap, to_snap, offset=0, length=None,
                   whole_object=True, namespace=""):
        q = ["offset=%d" % int(offset),
             "whole_object=%s" % ("true" if whole_object else "false")]
        if from_snap:
            q.append("from_snapshot=" + urllib.parse.quote(from_snap, safe=""))
        if to_snap:
            q.append("snapshot_name=" + urllib.parse.quote(to_snap, safe=""))
        if length is not None:
            q.append("length=%d" % int(length))
        path = "/api/block/image/%s/diff?%s" % (self._spec(pool, image, namespace),
                                                "&".join(q))
        try:
            bd = self._call("GET", path, "GET /api/block/image/{spec}/diff")
        except RbdBackendError as e:
            # A route-level 404 ("The path ... was not found") means this mgr
            # lacks the rbd-diff patch -> surface as not_supported so CBT can
            # degrade gracefully (vs a real image/snap not_found, which we keep).
            if e.status == 404 and "the path" in str(e).lower():
                raise RbdBackendError("dashboard has no /diff endpoint "
                                      "(needs the ceph rbd-diff patch)",
                                      status=404, not_supported=True)
            raise
        if isinstance(bd, dict):
            return bd.get("diffs", []) or []
        return bd or []

    def pool_stats(self, pool):
        bd = self._call("GET", "/api/pool?stats=true", "GET /api/pool")
        pools = bd if isinstance(bd, list) else bd.get("value", []) if isinstance(bd, dict) else []
        for p in pools:
            if p.get("pool_name") == pool or p.get("pool_name") == pool:
                st = p.get("stats", {}) or {}
                def _v(k):
                    x = st.get(k)
                    return int(x.get("latest", 0)) if isinstance(x, dict) else int(x or 0)
                used = _v("bytes_used") or _v("stored")
                avail = _v("max_avail") or _v("avail_raw")
                return {"used": used, "free": avail, "total": used + avail}
        raise RbdBackendError("pool %r not found in dashboard pool stats" % pool, not_found=True)

    # ---- namespaces ----
    def namespace_list(self, pool):
        bd = self._call("GET", "/api/block/pool/%s/namespace" % urllib.parse.quote(pool),
                        "GET /api/block/pool/{pool}/namespace")
        out = []
        for n in bd or []:
            if isinstance(n, dict):
                out.append(n.get("namespace"))
            else:
                out.append(n)
        return [n for n in out if n]

    def namespace_create(self, pool, namespace):
        try:
            return self._call("POST", "/api/block/pool/%s/namespace" % urllib.parse.quote(pool),
                              "POST /api/block/pool/{pool}/namespace", {"namespace": namespace})
        except RbdBackendError as e:
            # idempotent: treat "already exists" as success
            if e.status in (400, 409) or "exist" in str(e).lower():
                if namespace in self.namespace_list(pool):
                    return None
            raise

    def namespace_remove(self, pool, namespace):
        return self._call("DELETE", "/api/block/pool/%s/namespace/%s"
                          % (urllib.parse.quote(pool), urllib.parse.quote(namespace)),
                          "DELETE /api/block/pool/{pool}/namespace/{ns}")


def _map_rbd_errors(method):
    """Translate librbd/librados exceptions into RbdBackendError (keeps
    not_found semantics so the toolstack behaves as with the REST backend)."""
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except RbdBackendError:
            raise
        except self._rbd.ImageNotFound as e:
            raise RbdBackendError(str(e), not_found=True)
        except self._rbd.ObjectExists as e:
            raise RbdBackendError(str(e), status=17)
        except Exception as e:  # rados.Error / rbd.Error / OSError / ...
            raise RbdBackendError("%s: %s" % (type(e).__name__, e))
    wrapper.__name__ = getattr(method, "__name__", "wrapped")
    return wrapper


class LocalBackend(RbdBackend):
    """Control-plane backend using the librbd/librados python bindings.

    For hosts that DO have a working (aes256k-capable) ceph userspace: an
    alternative to RestBackend that needs no ceph-mgr dashboard. python3-rbd /
    python3-rados are imported LAZILY (only when backend=local is selected) so
    the default REST path stays binding-free on a dom0 without ceph userspace.

    One rados connection per plugin invocation; cluster access reuses the
    datapath's mon_host/user/key (passed inline -- no keyring file) or an
    explicit ceph_conf. Every method returns the SAME dict shapes as
    RestBackend, so volume.py / sr.py / rbd_gc.py stay backend-agnostic.
    """
    CONNECT_TIMEOUT = 30

    def __init__(self, mon_host=None, user=None, key=None, ceph_conf=None):
        try:
            import rados
            import rbd
        except ImportError as e:
            raise RbdBackendError(
                "backend=local needs the ceph python bindings "
                "(python3-rbd/python3-rados): %s" % e)
        self._rados = rados
        self._rbd = rbd
        self._inst = rbd.RBD()
        # feature name<->bit map (guard: not every constant exists on old libs)
        self._feat_bit = {}
        for n in ("LAYERING", "STRIPINGV2", "EXCLUSIVE_LOCK", "OBJECT_MAP",
                  "FAST_DIFF", "DEEP_FLATTEN", "JOURNALING", "DATA_POOL",
                  "OPERATIONS"):
            bit = getattr(rbd, "RBD_FEATURE_" + n, None)
            if bit is not None:
                self._feat_bit[n.lower().replace("_", "-")] = bit
        conf = {}
        if mon_host:
            conf["mon_host"] = mon_host
        if key:
            conf["key"] = key
        try:
            if ceph_conf:
                self._cluster = rados.Rados(conffile=ceph_conf, conf=conf,
                                            rados_id=user or "admin")
            elif conf:
                self._cluster = rados.Rados(conf=conf, rados_id=user or "admin")
            else:
                self._cluster = rados.Rados(rados_id=user or "admin")
            self._cluster.connect(timeout=self.CONNECT_TIMEOUT)
        except Exception as e:
            raise RbdBackendError("cannot connect to ceph (backend=local): %s" % e)
        # librados segfaults if the Rados object is GC'd at interpreter exit
        # without a clean shutdown (intermittent SIGSEGV, e.g. SR.stat under the
        # rapid polling of an SXM migration). Shut it down explicitly at exit.
        import atexit
        atexit.register(self._safe_shutdown)
        _log("connected via librbd (backend=local)")

    def _safe_shutdown(self):
        try:
            if getattr(self, "_cluster", None) is not None:
                self._cluster.shutdown()
                self._cluster = None
        except Exception:
            pass

    # ---- helpers ----
    def _feats_to_names(self, bitmask):
        return sorted(n for n, bit in self._feat_bit.items()
                      if bitmask & bit == bit)

    def _feats_to_mask(self, features):
        f = RestBackend._feats(features)
        if f is None:
            return None
        mask = 0
        for name in f:
            mask |= self._feat_bit.get(name, 0)
        return mask

    def _ioctx(self, pool, namespace=""):
        ioctx = self._cluster.open_ioctx(pool)
        ioctx.set_namespace(namespace or "")
        return ioctx

    # ---- RbdBackend API ----
    @_map_rbd_errors
    def list_images(self, pool, namespace=""):
        # Return the SAME full per-image info as image_info (size, metadata,
        # snapshots, disk_usage) -- SR.ls relies on it: a bare {name} listing
        # makes SR.ls report virtual_size=0 (+ lose metadata/snapshots) for every
        # VDI, which xapi then persists on every scan (breaks VDI sizes + SXM).
        with self._ioctx(pool, namespace) as ioctx:
            # the ioctx namespace scopes the listing -> no cross-namespace leak
            names = list(self._inst.list(ioctx))
        out = []
        for n in names:
            try:
                out.append(self.image_info(pool, n, namespace=namespace))
            except Exception:
                # raced with a create/delete (or transiently busy): a bare entry
                # would zero the VDI in xapi, so drop it -- the next scan re-adds
                # it once it settles.
                continue
        return out

    @_map_rbd_errors
    def image_info(self, pool, image, namespace=""):
        out = {"name": image, "namespace": namespace or "", "size": 0,
               "features_name": [], "parent": None, "metadata": {},
               "snapshots": [], "disk_usage": 0}
        with self._ioctx(pool, namespace) as ioctx:
            with self._rbd.Image(ioctx, image) as img:
                size = int(img.size())
                out["size"] = size
                feats = self._feats_to_names(img.features())
                out["features_name"] = feats
                try:
                    pp, pi, ps = img.parent_info()
                    out["parent"] = {"pool": pp, "image": pi, "snapshot": ps}
                except self._rbd.ImageNotFound:
                    out["parent"] = None
                out["metadata"] = {k: v for k, v in img.metadata_list()}
                for s in img.list_snaps():
                    name = s["name"]
                    try:
                        img.set_snap(name)
                        children = list(img.list_children())
                    except Exception:
                        children = []
                    finally:
                        img.set_snap(None)
                    out["snapshots"].append({
                        "name": name, "size": int(s.get("size", 0) or 0),
                        "children": children})
                # Physical usage via a whole-object diff is cheap only when
                # fast-diff/object-map is present: it then just reads the small
                # object-map bitmap. Our backported krbd DOES maintain that map
                # (verified: krbd-written images keep valid flags), so on the
                # default 'performance' preset this is fast. The 'compat' preset
                # has no map -> a whole-object diff would scan every object, so
                # skip it there and leave disk_usage at 0.
                if size and "fast-diff" in feats:
                    used = [0]

                    def _cb(_off, _len, exists):
                        if exists:
                            used[0] += _len
                    try:
                        img.diff_iterate(0, size, None, _cb, whole_object=True)
                        out["disk_usage"] = used[0]
                    except Exception:
                        out["disk_usage"] = 0
        return out

    @_map_rbd_errors
    def create(self, pool, image, size, features, namespace="", obj_size=None):
        mask = self._feats_to_mask(features)
        order = 0
        if obj_size:
            order = int(round(math.log(float(obj_size), 2)))
        with self._ioctx(pool, namespace) as ioctx:
            self._inst.create(ioctx, image, int(size), order=order,
                              old_format=False, features=mask)

    @_map_rbd_errors
    def remove(self, pool, image, namespace=""):
        with self._ioctx(pool, namespace) as ioctx:
            self._inst.remove(ioctx, image)

    @_map_rbd_errors
    def resize(self, pool, image, size, features=None, namespace=""):
        # librbd resize allows shrink by default -> matches the REST backend.
        with self._ioctx(pool, namespace) as ioctx:
            with self._rbd.Image(ioctx, image) as img:
                img.resize(int(size))

    @_map_rbd_errors
    def image_meta_set(self, pool, image, metadata, size, namespace=""):
        # REST replaces the whole image-meta dict; emulate that.
        metadata = metadata or {}
        with self._ioctx(pool, namespace) as ioctx:
            with self._rbd.Image(ioctx, image) as img:
                cur = {k: v for k, v in img.metadata_list()}
                for k in cur:
                    if k not in metadata:
                        img.metadata_remove(k)
                for k, v in metadata.items():
                    img.metadata_set(k, "" if v is None else str(v))

    @_map_rbd_errors
    def snap_create(self, pool, image, snap, namespace=""):
        with self._ioctx(pool, namespace) as ioctx:
            with self._rbd.Image(ioctx, image) as img:
                img.create_snap(snap)

    @_map_rbd_errors
    def snap_remove(self, pool, image, snap, namespace=""):
        with self._ioctx(pool, namespace) as ioctx:
            with self._rbd.Image(ioctx, image) as img:
                img.remove_snap(snap)

    @_map_rbd_errors
    def snap_set_protected(self, pool, image, snap, protected, namespace=""):
        with self._ioctx(pool, namespace) as ioctx:
            with self._rbd.Image(ioctx, image) as img:
                if protected:
                    img.protect_snap(snap)
                else:
                    img.unprotect_snap(snap)

    @_map_rbd_errors
    def snap_rollback(self, pool, image, snap, namespace=""):
        # Blocks until the rollback completes (can be long for a big image).
        with self._ioctx(pool, namespace) as ioctx:
            with self._rbd.Image(ioctx, image) as img:
                img.rollback_to_snap(snap)

    @_map_rbd_errors
    def clone(self, pool, image, snap, dst_pool, dst_image, features=None,
              namespace="", dst_namespace=""):
        mask = self._feats_to_mask(features)
        with self._ioctx(pool, namespace) as p_ioctx:
            with self._ioctx(dst_pool, dst_namespace) as c_ioctx:
                self._inst.clone(p_ioctx, image, snap, c_ioctx, dst_image,
                                 features=mask)

    @_map_rbd_errors
    def flatten(self, pool, image, namespace=""):
        # Blocks until flatten finishes; no HTTP timeout to worry about here.
        with self._ioctx(pool, namespace) as ioctx:
            with self._rbd.Image(ioctx, image) as img:
                img.flatten()

    @_map_rbd_errors
    def image_rename(self, pool, image, new_name, namespace=""):
        with self._ioctx(pool, namespace) as ioctx:
            self._inst.rename(ioctx, image, new_name)

    @_map_rbd_errors
    def snap_rename(self, pool, image, snap, new_snap, namespace=""):
        with self._ioctx(pool, namespace) as ioctx:
            with self._rbd.Image(ioctx, image) as img:
                img.rename_snap(snap, new_snap)

    @_map_rbd_errors
    def image_diff(self, pool, image, from_snap, to_snap, offset=0, length=None,
                   whole_object=True, namespace=""):
        diffs = []
        with self._ioctx(pool, namespace) as ioctx:
            # open read-only at the target state (a snapshot, or the live head)
            with self._rbd.Image(ioctx, image, snapshot=to_snap,
                                 read_only=True) as img:
                size = int(img.size())
                start = int(offset)
                dl = size - start if length is None else int(length)
                dl = max(0, min(dl, size - start))

                def _cb(_off, _len, exists):
                    diffs.append({"offset": _off, "length": _len,
                                  "exists": bool(exists)})
                if dl > 0:
                    img.diff_iterate(start, dl, from_snap, _cb,
                                     whole_object=whole_object)
        return diffs

    @_map_rbd_errors
    def pool_stats(self, pool):
        ret, buf, errs = self._cluster.mon_command(
            json.dumps({"prefix": "df", "format": "json"}), b"")
        if ret != 0:
            raise RbdBackendError("`ceph df` failed: %s" % errs)
        df = json.loads(buf.decode() if isinstance(buf, bytes) else buf)
        for p in df.get("pools", []) or []:
            if p.get("name") == pool:
                st = p.get("stats", {}) or {}
                used = int(st.get("bytes_used", st.get("stored", 0)) or 0)
                free = int(st.get("max_avail", 0) or 0)
                return {"used": used, "free": free, "total": used + free}
        raise RbdBackendError("pool %r not found in `ceph df`" % pool,
                              not_found=True)

    @_map_rbd_errors
    def namespace_list(self, pool):
        with self._cluster.open_ioctx(pool) as ioctx:
            return list(self._inst.namespace_list(ioctx))

    @_map_rbd_errors
    def namespace_create(self, pool, namespace):
        try:
            with self._cluster.open_ioctx(pool) as ioctx:
                self._inst.namespace_create(ioctx, namespace)
        except self._rbd.ObjectExists:
            return None  # idempotent

    @_map_rbd_errors
    def namespace_remove(self, pool, namespace):
        with self._cluster.open_ioctx(pool) as ioctx:
            self._inst.namespace_remove(ioctx, namespace)
