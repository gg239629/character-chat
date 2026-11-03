"""Keep local model gateways off environment proxies; preserve public routing."""
import ipaddress
import os
from urllib.parse import urlsplit
from openai import DefaultHttpxClient,DefaultAsyncHttpxClient


def is_local_gateway(base_url):
    url=base_url or os.getenv('OPENAI_BASE_URL','')
    try:
        parsed=urlsplit(url)
        if parsed.scheme not in ('http','https'):return False
        host=(parsed.hostname or '').rstrip('.').lower()
        if host=='localhost' or host.endswith('.localhost'):return True
        address=ipaddress.ip_address(host)
        if isinstance(address,ipaddress.IPv6Address) and address.ipv4_mapped:
            address=address.ipv4_mapped
        return address.is_private or address.is_loopback or address.is_link_local
    except ValueError:return False


def client_options(base_url,*,asynchronous=False):
    """Use SDK default transports/limits, changing proxy routing only locally.

    Never change process-wide proxy variables: public providers must retain
    their configured proxy, certificate and network behavior.
    """
    if not is_local_gateway(base_url):return {}
    client=DefaultAsyncHttpxClient if asynchronous else DefaultHttpxClient
    return {'http_client':client(trust_env=False)}
