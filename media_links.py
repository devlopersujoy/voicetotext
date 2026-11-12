import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit

import urllib3

LIMIT = 48 * 1024**2


def parse_link(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 for c in url):
        raise ValueError("Enter a public HTTP or HTTPS media URL.")
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
            or parts.port not in {None, 80, 443} or "\\" in url or "%" in parts.hostname):
        raise ValueError("Use a public media URL on a standard web port.")
    return parts


def download(url, destination):
    deadline = time.monotonic() + 120
    for _ in range(6):
        parts = parse_link(url)
        host = parts.hostname.encode("idna").decode()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(item[4][0]) for item in records]
        if not addresses or any(not addr.is_global or addr.is_multicast or
                                (addr.version == 6 and (addr.sixtofour or addr.teredo or addr.ipv4_mapped))
                                for addr in addresses):
            raise ValueError("This link must point to a public media server.")
        # Connect to the validated address; retain the hostname for TLS and HTTP.
        address = str(addresses[0])
        if parts.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(address, port, server_hostname=host, assert_hostname=host)
        else:
            pool = urllib3.HTTPConnectionPool(address, port)
        try:
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            with pool.urlopen("GET", target, headers={"Host": parts.netloc, "Accept-Encoding": "identity"},
                              redirect=False, retries=False, preload_content=False,
                              timeout=urllib3.Timeout(connect=8, read=12)) as response:
                if response.status in {301, 302, 303, 307, 308} and response.headers.get("Location"):
                    url = urljoin(url, response.headers["Location"])
                    continue
                if response.status != 200:
                    raise ValueError("Could not fetch this file. Use a public direct download link.")
                total = 0
                with destination.open("wb") as file:
                    for chunk in response.stream(65536, decode_content=False):
                        total += len(chunk)
                        if total > LIMIT or time.monotonic() > deadline:
                            raise ValueError("Download exceeded 48 MiB or timed out.")
                        file.write(chunk)
                return
        finally:
            pool.close()
    raise ValueError("Too many redirects in the media link.")
