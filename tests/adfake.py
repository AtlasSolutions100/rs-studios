"""A fake transport for adhttp (tests only): answers ad-platform calls from tests/fixtures/<platform>/*.json
and records every call. No request leaves the machine.

    fake = FakeTransport()
    fake.on("GET", "/me/adaccounts", "meta/adaccounts_page1.json", "meta/adaccounts_page2.json")
    adhttp.TRANSPORT = fake

A route answers its responses in order and then repeats the last one. A response is a fixture
name, a dict (the JSON body), (status, body) or (status, body, headers), an exception instance
(raised, like requests would), or a callable(method, url, params, data, headers) returning one
of those. The longest matching path suffix wins. A call with no route fails the test.
"""
import json
import os
from urllib.parse import urlsplit

import adhttp

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class FakeTransport:
    def __init__(self):
        self.routes, self.calls = {}, []

    def on(self, method, path_suffix, *responses):
        self.routes[(method.upper(), path_suffix)] = list(responses)
        return self

    def find(self, method=None, suffix=""):
        return [c for c in self.calls if (method is None or c["method"] == method) and c["path"].endswith(suffix)]

    def __call__(self, method, url, params=None, data=None, headers=None, timeout=None, files=None):
        path = urlsplit(url).path
        if isinstance(data, (str, bytes)):          # a JSON body (adhttp.call(json_body=...))
            try:
                data = json.loads(data)
            except ValueError:
                data = {"_raw": data}
        self.calls.append({"method": method, "url": url, "path": path, "params": dict(params or {}),
                           "data": dict(data or {}), "headers": dict(headers or {}), "timeout": timeout})
        best = None
        for (m, suffix), queue in self.routes.items():
            if m == method and path.endswith(suffix) and (best is None or len(suffix) > len(best[0])):
                best = (suffix, queue)
        if best is None:
            raise AssertionError(f"no fake answer for {method} {path}")
        queue = best[1]
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(r) and not isinstance(r, BaseException):
            r = r(method, url, params, data, headers)
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, adhttp.Response):
            return r
        status, hdrs = 200, {}
        if isinstance(r, tuple):
            status, hdrs, r = r[0], (r[2] if len(r) > 2 else {}), (r[1] if len(r) > 1 else {})
        if isinstance(r, str):
            r = fixture(r)
        if status == 200 and isinstance(r, dict) and "error" in r:
            status = 400
        return adhttp.Response(status, hdrs, r)
