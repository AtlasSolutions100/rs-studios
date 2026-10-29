"""Fetch a member-supplied URL without letting it reach anything internal.

Rules: http(s) only, ports 80/443 (or the URL's explicit standard port), no
credentials in the URL, every hostname resolved and every address checked
(no private, loopback, link-local, multicast, reserved, CGNAT or unspecified
addresses, IPv4 or IPv6), and the connection is made to the address we
checked (so a DNS answer can't change between the check and the request).
Redirects are followed by hand (at most 4), each hop checked again. Bodies
are capped at MAX_BYTES and timeouts are short.
"""
import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit

import certifi
import urllib3

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 4
TIMEOUT = urllib3.Timeout(connect=5, read=10)
UA = "Mozilla/5.0 (compatible; CampaignifyBot/1.0; +https://campaignify.ai)"


class FetchError(ValueError):
    pass


def _public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved
                or a.is_unspecified or not a.is_global)


def resolve(host: str, port: int) -> str:
    """One public address for host, or FetchError."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchError("That address doesn't resolve.") from None
    addrs = [i[4][0] for i in infos]
    if not addrs:
        raise FetchError("That address doesn't resolve.")
    if not all(_public(a) for a in addrs):
        raise FetchError("That address points to a private or internal network.")
    return addrs[0]


def check_url(url: str):
    """(scheme, host, port, path) for an allowed URL, or FetchError."""
    url = (url or "").strip()
    if not url:
        raise FetchError("Add a URL.")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("Only http and https links.")
    if parts.username or parts.password:
        raise FetchError("Links with a username or password aren't allowed.")
    host = (parts.hostname or "").strip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".internal") \
            or host.endswith(".local"):
        raise FetchError("That address points to a private or internal network.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise FetchError("That port isn't valid.") from None
    if port not in (80, 443):
        raise FetchError("Only standard web ports (80 and 443).")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, host, port, path, url


def _open(scheme, host, port, ip):
    if scheme == "https":
        return urllib3.HTTPSConnectionPool(ip, port, server_hostname=host, assert_hostname=host,
                                           cert_reqs="CERT_REQUIRED", ca_certs=os.environ.get("RS_CA_BUNDLE") or certifi.where(),
                                           timeout=TIMEOUT, retries=False, maxsize=1)
    return urllib3.HTTPConnectionPool(ip, port, timeout=TIMEOUT, retries=False, maxsize=1)


def fetch(url: str, accept: str = "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5") -> dict:
    """{url, status, content_type, text} for a public page. Raises FetchError."""
    r = _get(url, accept, MAX_BYTES)
    ctype = r["content_type"]
    charset = "utf-8"
    if "charset=" in ctype:
        charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    try:
        text = r["data"].decode(charset, errors="replace")
    except LookupError:
        text = r["data"].decode("utf-8", errors="replace")
    return {"url": r["url"], "status": r["status"], "content_type": ctype, "text": text}


def fetch_bytes(url: str, max_bytes: int = 15 * 1024 * 1024, accept: str = "image/*") -> dict:
    """{url, status, content_type, data} with the same guards as fetch(); a
    body over max_bytes raises FetchError instead of being cut."""
    r = _get(url, accept, max_bytes + 1)
    if len(r["data"]) > max_bytes:
        raise FetchError("That file is too big.")
    return r


def _get(url: str, accept: str, max_bytes: int) -> dict:
    seen = 0
    while True:
        scheme, host, port, path, full = check_url(url)
        ip = resolve(host, port)
        pool = _open(scheme, host, port, ip)
        try:
            r = pool.urlopen("GET", path, headers={"Host": host if port in (80, 443) else f"{host}:{port}",
                                                   "User-Agent": UA, "Accept": accept,
                                                   "Accept-Encoding": "gzip, deflate"},
                             redirect=False, preload_content=False, decode_content=True)
        except urllib3.exceptions.SSLError:
            raise FetchError("The site's HTTPS certificate isn't valid.") from None
        except (urllib3.exceptions.HTTPError, OSError):
            raise FetchError("The site didn't answer in time.") from None
        try:
            if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                seen += 1
                if seen > MAX_REDIRECTS:
                    raise FetchError("Too many redirects.")
                url = urljoin(full, r.headers["Location"])
                continue
            data = b""
            for chunk in r.stream(65536, decode_content=True):
                data += chunk
                if len(data) > max_bytes:
                    break
            return {"url": full, "status": r.status, "content_type": r.headers.get("Content-Type", ""),
                    "data": data[:max_bytes]}
        finally:
            r.release_conn()
            pool.close()
