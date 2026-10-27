"""Multi-repo / microservice support: discovery, contracts, cross-service linking."""
from kitsune.services.discovery import discover_services
from kitsune.services.contracts import extract_contracts
from kitsune.services.linker import link_contracts, service_map

__all__ = ["discover_services", "extract_contracts", "link_contracts", "service_map"]
