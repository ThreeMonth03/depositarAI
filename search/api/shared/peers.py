"""Resolve reviewed Docker peers to one private IPv4 address."""

import ipaddress
import socket


def service_address(host: str, port: int) -> str:
    if (host, port) not in {
        ("fess-search", 8080),
        ("scope-model", 11434),
        ("answer-model", 11434),
        ("reranker", 8001),
        ("opensearch-search", 9200),
        ("filtered-embedding", 11434),
    }:
        raise ValueError("unapproved service")
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(
            host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    }
    if len(addresses) != 1:
        raise ValueError("ambiguous service address")
    address = ipaddress.ip_address(addresses.pop())
    if not any(
        address in ipaddress.ip_network(cidr)
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise ValueError("service is outside the private network")
    return f"http://{address}:{port}"
