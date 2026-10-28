"""
Service discovery — find the deployable services in a system.

A system can be given as:
- one folder containing several services (a monorepo, or a folder of cloned repos):
  every directory holding a build/deploy manifest becomes a service;
- a list of repository paths (one service each, named after the folder);
- an explicit {service_name: path} mapping.
"""
from __future__ import annotations

import os
from pathlib import Path

# Files that mark a directory as a buildable/deployable unit
MANIFESTS = (
    "pom.xml", "build.gradle", "build.gradle.kts", "go.mod", "package.json",
    "pyproject.toml", "setup.py", "requirements.txt", "Dockerfile", "Cargo.toml",
)
_SKIP_DIRS = {
    ".git", ".github", "node_modules", "vendor", "target", "build", "dist", "__pycache__",
    ".venv", "venv", ".idea", ".vscode", "test", "tests", "docs", "doc", "examples",
    "old-docs", "documentation", "site-docs",
    # deployment/infrastructure folders (Prometheus/Grafana images, manifests), not services
    "docker", "deploy", "deployment", "deployments", "infra", "infrastructure", "k8s",
    "kubernetes", "helm", "helm-chart", "charts", "kustomize", "terraform", "scripts", "ops",
}
MAX_DEPTH = 4
# Folder names that say nothing about the service: use the parent's name instead
_GENERIC_NAMES = {"src", "app", "server", "main", "service", "source", "code", "backend"}


def discover_services(source) -> dict[str, Path]:
    """Return {service_name: path} for a system.

    `source` may be a path (str/Path), a list of paths, or a {name: path} dict.
    """
    if isinstance(source, dict):
        return {str(name): Path(path) for name, path in source.items()}
    if isinstance(source, (list, tuple)):
        return _unique_names([Path(p) for p in source])

    root = Path(source)
    # A folder of cloned repositories: every child repo is one service
    repos = [d for d in sorted(root.iterdir()) if d.is_dir() and (d / ".git").exists()] \
        if root.is_dir() else []
    if repos and not (root / ".git").exists():
        return _unique_names(repos)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        if depth >= MAX_DEPTH:
            dirnames[:] = []
        if here != root and any(m in filenames for m in MANIFESTS):
            found.append(here)
            dirnames[:] = []  # nested manifests belong to this service
    if not found:
        return {root.name: root}
    return _unique_names(found)


def declared_name(path: Path) -> str:
    """The service-discovery name a service declares for itself, if any.

    Spring `spring.application.name` is the name other services resolve it by
    (Eureka/Consul/k8s), so it is used as the service's name.
    """
    names = declared_names(path)
    return names[0][0] if names and names[0][1] == "discovery" else ""


def package_aliases(path: Path) -> set[str]:
    """Package names (npm, Go module, Python) — extra aliases, not the deploy name."""
    return {n for n, kind in declared_names(path) if kind == "package"}


def declared_names(path: Path) -> list[tuple[str, str]]:
    """[(name, kind)] — kind "discovery" (Spring app name) or "package"."""
    found: list[tuple[str, str]] = []
    import json as _json
    import re as _re
    for res in ("src/main/resources/application.yml", "src/main/resources/application.yaml",
                "src/main/resources/bootstrap.yml", "src/main/resources/application.properties"):
        f = path / res
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="ignore")
            m = _re.search(r"spring\.application\.name\s*[=:]\s*([\w.-]+)", text) or \
                _re.search(r"^spring:\s*\n(?:[ \t]+.*\n)*?[ \t]+application:\s*\n[ \t]+name:\s*([\w.-]+)",
                           text, _re.M)
            if m and "${" not in m.group(1):
                found.append((m.group(1), "discovery"))
                break
    pkg = path / "package.json"
    if pkg.is_file():
        try:
            name = _json.loads(pkg.read_text(encoding="utf-8", errors="ignore")).get("name", "")
            if name:
                found.append((name.split("/")[-1], "package"))
        except ValueError:
            pass
    gomod = path / "go.mod"
    if gomod.is_file():
        m = _re.search(r"^module\s+(\S+)", gomod.read_text(encoding="utf-8", errors="ignore"), _re.M)
        if m:
            found.append((m.group(1).rstrip("/").split("/")[-1], "package"))
    for meta in ("pyproject.toml", "setup.py"):
        f = path / meta
        if f.is_file():
            m = _re.search(r"""\bname\s*=\s*["']([\w.-]+)["']""", f.read_text(encoding="utf-8", errors="ignore"))
            if m:
                found.append((m.group(1), "package"))
    return found


def _unique_names(paths: list[Path]) -> dict[str, Path]:
    """Name each service by its declared name, else its folder; disambiguate clashes."""
    names: dict[str, Path] = {}
    for path in paths:
        name = declared_name(path) or path.name
        if name.lower() in _GENERIC_NAMES and path.parent.name:
            name = path.parent.name          # cartservice/src → "cartservice"
        if name in names:
            name = f"{path.parent.name}-{path.name}"
        names[name] = path
    return names


def service_aliases(name: str, path: Path | None = None) -> set[str]:
    """Host names a service is likely reachable under (k8s/docker-compose DNS names).

    "productcatalogservice" → {"productcatalogservice", "productcatalog"};
    "orders-service" → {"orders-service", "orders", "orders_service"}.
    With `path`, the folder name is an alias too ("spring-petclinic-customers-service").
    """
    if path is not None:
        extra = {Path(path).name, *package_aliases(Path(path))} - {name}
        aliases = service_aliases(name)
        for e in extra:
            aliases |= service_aliases(e)
        return aliases
    n = name.lower()
    aliases = {n, n.replace("-", "_"), n.replace("_", "-"), n.replace("-", "").replace("_", "")}
    for suffix in ("-service", "_service", "service", "-svc", "svc", "-api", "-server"):
        if n.endswith(suffix) and len(n) > len(suffix) + 1:
            aliases.add(n[: -len(suffix)].rstrip("-_"))
    return {a for a in aliases if a}
