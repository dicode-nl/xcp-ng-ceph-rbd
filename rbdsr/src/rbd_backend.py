"""
rbd_backend.py - control-plane backends for the Ceph RBD SR.

The SR driver never shells out to `ceph`/`rbd` (el7 dom0 has no aes256k-capable
userspace). All volume-management ops go through an RbdBackend. The default and
only implemented backend talks to the ceph-mgr *dashboard* REST API over HTTPS
(JWT auth), which needs nothing on dom0 but python stdlib. The cephx key is used
ONLY for the datapath (kernel rbd map, see rbd_sysfs.py), never here.

Contract validated against Ceph 20.2.4 (Tentacle); see the endpoint/version
table in RestBackend.VERSIONS.

Stdlib only (urllib), python 3.6+ (matches XCP-ng 8.3 dom0 sm).
"""

import json
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
    def image_diff(self, pool, image, from_snap, to_snap, offset=0, length=None,
                   whole_object=True, namespace=""):
        """Changed extents between two states of an image (for CBT).
        from_snap=None -> from image creation; to_snap=None -> the live head.
        -> [{'offset','length','exists'}]. May raise not_supported."""
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
    """Factory from SR device-config. backend=rest (default)."""
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
    raise RbdBackendError("unknown backend %r (only 'rest' implemented)" % kind)


class RestBackend(RbdBackend):
    """ceph-mgr dashboard REST API backend. Validated on Ceph 20.2.4."""

    # Default Accept-version per endpoint template (from live probe). The client
    # still auto-negotiates on 415, so these are fast-paths, not hard-coded walls.
    VERSIONS = {
        "GET /api/block/image": "2.0",
        "POST /api/block/image": "1.0",
        "GET /api/block/image/{spec}": "1.0",
        "GET /api/block/image/{spec}/diff": "1.0",
        "PUT /api/block/image/{spec}": "1.0",
        "DELETE /api/block/image/{spec}": "1.0",
        "POST /api/block/image/{spec}/snap": "1.0",
        "PUT /api/block/image/{spec}/snap/{name}": "1.0",
        "DELETE /api/block/image/{spec}/snap/{name}": "1.0",
        "POST /api/block/image/{spec}/snap/{name}/rollback": "1.0",
        "POST /api/block/image/{spec}/snap/{name}/clone": "1.0",
        "POST /api/block/image/{spec}/flatten": "1.0",
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
