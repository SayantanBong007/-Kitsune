"""
Library Catalog — Semantic Knowledge Base & Dynamic Discovery Engine for External Libraries.

Categorizes external packages (Python, JavaScript/TypeScript, Java, Go) into
architectural roles and side-effects without downloading third-party source code.
Inspired by GitHub CodeQL's Models-as-Data (MaD) and Sourcegraph SCIP.

Now features Dynamic Autonomous Discovery:
Even if a company uses proprietary internal libraries (e.g. `@mycorp/zeus`, `com.acme.pay`,
`internal_auth_vault`), Kitsune analyzes manifest files, call sites, method semantics,
caller contexts, and variable names across the codebase to deduce what the library does
and dynamically generate the library catalog entry with risk assessment and side effects.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class ExternalLibraryInfo:
    package: str
    category: str
    side_effect: str
    risk_level: str = "medium"
    confidence: float = 1.0
    discovered_methods: list[str] = field(default_factory=list)
    callers: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


# Curated table for well-known open-source libraries (instant O(1) baseline)
LIBRARY_REGISTRY: dict[str, ExternalLibraryInfo] = {
    # ── Payment Gateways ──────────────────────────────────────────────────
    "stripe": ExternalLibraryInfo("stripe", "Payment Gateway", "Network I/O (Financial)", "high"),
    "paypal": ExternalLibraryInfo("paypal", "Payment Gateway", "Network I/O (Financial)", "high"),
    "paypalcheckoutsdk": ExternalLibraryInfo("paypal", "Payment Gateway", "Network I/O (Financial)", "high"),
    "braintree": ExternalLibraryInfo("braintree", "Payment Gateway", "Network I/O (Financial)", "high"),
    "razorpay": ExternalLibraryInfo("razorpay", "Payment Gateway", "Network I/O (Financial)", "high"),
    "square": ExternalLibraryInfo("square", "Payment Gateway", "Network I/O (Financial)", "high"),

    # ── Databases & ORM ───────────────────────────────────────────────────
    "sqlalchemy": ExternalLibraryInfo("sqlalchemy", "Database ORM", "Persistence (SQL)", "medium"),
    "psycopg2": ExternalLibraryInfo("psycopg2", "Database Driver", "Persistence (PostgreSQL)", "medium"),
    "asyncpg": ExternalLibraryInfo("asyncpg", "Database Driver", "Persistence (PostgreSQL)", "medium"),
    "pymongo": ExternalLibraryInfo("pymongo", "Database Driver", "Persistence (MongoDB)", "medium"),
    "prisma": ExternalLibraryInfo("prisma", "Database ORM", "Persistence (SQL/NoSQL)", "medium"),
    "typeorm": ExternalLibraryInfo("typeorm", "Database ORM", "Persistence (SQL)", "medium"),
    "mongoose": ExternalLibraryInfo("mongoose", "Database ODM", "Persistence (MongoDB)", "medium"),
    "hibernate": ExternalLibraryInfo("hibernate", "Database ORM", "Persistence (SQL)", "medium"),
    "gorm": ExternalLibraryInfo("gorm", "Database ORM", "Persistence (SQL)", "medium"),
    "sqlx": ExternalLibraryInfo("sqlx", "Database Library", "Persistence (SQL)", "medium"),
    "sqlite3": ExternalLibraryInfo("sqlite3", "Database Driver", "Persistence (SQLite)", "low"),

    # ── Cache & In-Memory Stores ──────────────────────────────────────────
    "redis": ExternalLibraryInfo("redis", "Cache Store", "In-Memory State (Key-Value)", "medium"),
    "ioredis": ExternalLibraryInfo("ioredis", "Cache Store", "In-Memory State (Key-Value)", "medium"),
    "memcached": ExternalLibraryInfo("memcached", "Cache Store", "In-Memory State (Key-Value)", "low"),
    "cachelib": ExternalLibraryInfo("cachelib", "Cache Store", "In-Memory State (Cache)", "low"),

    # ── Message Queues & Event Streaming ─────────────────────────────────
    "kafka": ExternalLibraryInfo("kafka", "Message Broker", "Async Event (Queue Publish/Consume)", "medium"),
    "confluent_kafka": ExternalLibraryInfo("confluent_kafka", "Message Broker", "Async Event (Queue Publish/Consume)", "medium"),
    "kafkajs": ExternalLibraryInfo("kafkajs", "Message Broker", "Async Event (Queue Publish/Consume)", "medium"),
    "pika": ExternalLibraryInfo("pika", "Message Broker", "Async Event (RabbitMQ AMQP)", "medium"),
    "amqplib": ExternalLibraryInfo("amqplib", "Message Broker", "Async Event (RabbitMQ AMQP)", "medium"),
    "celery": ExternalLibraryInfo("celery", "Task Queue", "Async Background Execution", "medium"),
    "nats": ExternalLibraryInfo("nats", "Message Broker", "Async Pub/Sub", "medium"),

    # ── HTTP & Network API Clients ────────────────────────────────────────
    "requests": ExternalLibraryInfo("requests", "HTTP Client", "Outbound Network I/O", "medium"),
    "httpx": ExternalLibraryInfo("httpx", "HTTP Client", "Outbound Network I/O", "medium"),
    "aiohttp": ExternalLibraryInfo("aiohttp", "HTTP Client", "Outbound Network I/O", "medium"),
    "urllib": ExternalLibraryInfo("urllib", "HTTP Client", "Outbound Network I/O", "low"),
    "axios": ExternalLibraryInfo("axios", "HTTP Client", "Outbound Network I/O", "medium"),
    "fetch": ExternalLibraryInfo("fetch", "HTTP Client", "Outbound Network I/O", "low"),
    "got": ExternalLibraryInfo("got", "HTTP Client", "Outbound Network I/O", "medium"),
    "resttemplate": ExternalLibraryInfo("resttemplate", "HTTP Client", "Outbound Network I/O", "medium"),
    "webclient": ExternalLibraryInfo("webclient", "HTTP Client", "Outbound Network I/O", "medium"),

    # ── Cloud & Storage ───────────────────────────────────────────────────
    "boto3": ExternalLibraryInfo("boto3", "Cloud SDK (AWS)", "Cloud Infrastructure / S3 / SQS", "high"),
    "google.cloud": ExternalLibraryInfo("google.cloud", "Cloud SDK (GCP)", "Cloud Infrastructure / Storage", "high"),
    "azure": ExternalLibraryInfo("azure", "Cloud SDK (Azure)", "Cloud Infrastructure / Storage", "high"),
    "minio": ExternalLibraryInfo("minio", "Object Storage", "Blob Storage (S3 API)", "medium"),

    # ── Security & Cryptography ───────────────────────────────────────────
    "bcrypt": ExternalLibraryInfo("bcrypt", "Security / Crypto", "Password Hashing (CPU Bound)", "low"),
    "jwt": ExternalLibraryInfo("jwt", "Security / Auth", "Token Encoding/Verification", "medium"),
    "jsonwebtoken": ExternalLibraryInfo("jsonwebtoken", "Security / Auth", "Token Encoding/Verification", "medium"),
    "cryptography": ExternalLibraryInfo("cryptography", "Security / Crypto", "Encryption / Decryption", "medium"),
    "passlib": ExternalLibraryInfo("passlib", "Security / Crypto", "Password Hashing", "low"),
    "passport": ExternalLibraryInfo("passport", "Security / Auth", "User Authentication", "medium"),

    # ── Standard Output / System (Common sinks) ───────────────────────────
    "logging": ExternalLibraryInfo("logging", "Logging", "Log I/O", "low"),
    "winston": ExternalLibraryInfo("winston", "Logging", "Log I/O", "low"),
    "os": ExternalLibraryInfo("os", "System / OS", "System Calls & Environment", "low"),
    "fs": ExternalLibraryInfo("fs", "Filesystem", "File I/O", "low"),
    "smtplib": ExternalLibraryInfo("smtplib", "Email", "Outbound Network I/O (SMTP)", "medium"),
    "subprocess": ExternalLibraryInfo("subprocess", "System / OS", "Process Execution", "medium"),
    "shutil": ExternalLibraryInfo("shutil", "Filesystem", "File I/O", "low"),
    "http": ExternalLibraryInfo("http", "HTTP (stdlib)", "Network I/O", "medium"),
}

# Standard libraries: well-known, no external side effects worth flagging by default
_STDLIB = {
    # Python
    "json", "re", "math", "time", "datetime", "collections", "itertools", "functools",
    "typing", "pathlib", "sys", "uuid", "random", "string", "hashlib", "hmac", "secrets",
    "base64", "copy", "enum", "dataclasses", "abc", "asyncio", "threading", "io",
    "contextlib", "operator", "decimal", "struct", "textwrap", "inspect", "warnings",
    # Go
    "fmt", "strings", "strconv", "errors", "sort", "sync", "context", "bytes", "bufio",
    "encoding", "unicode",
    # Java / JS runtime namespaces
    "java", "javax", "kotlin", "console", "math_js",
}
for _name in _STDLIB:
    LIBRARY_REGISTRY.setdefault(
        _name, ExternalLibraryInfo(_name, "Standard Library", "None (in-process)", "low"))


# ── Dynamic Semantic Archetype Rules ──────────────────────────────────────────

_ARCHETYPES = [
    {
        "category": "Payment Gateway",
        "keywords": {
            "pay", "payment", "payments", "charge", "charges", "bill", "billing",
            "invoice", "invoices", "checkout", "refund", "refunds", "card", "cards",
            "stripe", "paypal", "braintree", "razorpay", "square", "transaction",
            "transactions", "ledger", "payout", "settle", "wallet"
        },
        "side_effect": "Network I/O (Financial Mutation: {})",
        "risk_level": "critical",
    },
    {
        "category": "Authentication & Security",
        "keywords": {
            "auth", "authentication", "authorize", "authorization", "login", "token",
            "tokens", "jwt", "oauth", "session", "sessions", "verify", "verification",
            "hash", "crypto", "cryptography", "encrypt", "decrypt", "vault", "secrets",
            "secret", "identity", "rbac", "iam", "password", "signer", "passlib", "bcrypt"
        },
        "side_effect": "Security Boundary (Identity & Token Verification)",
        "risk_level": "high",
    },
    {
        "category": "Database ORM",
        "keywords": {
            "db", "database", "sql", "orm", "query", "queries", "table", "tables",
            "model", "models", "find", "insert", "save", "update", "delete", "migrate",
            "repository", "store", "dao", "postgres", "mysql", "mongo", "dynamo",
            "cassandra", "sqlite", "prisma", "typeorm", "hibernate", "gorm", "sequelize"
        },
        "side_effect": "Data Persistence (Database Mutation / Query)",
        "risk_level": "high",
    },
    {
        "category": "Cache Store",
        "keywords": {
            "cache", "caching", "redis", "memcache", "memcached", "ttl", "expire",
            "evict", "mget", "invalidation", "cachelib", "inmemory"
        },
        "side_effect": "In-Memory State (Cache Key-Value Store)",
        "risk_level": "low",
    },
    {
        "category": "Message Broker",
        "keywords": {
            "queue", "mq", "kafka", "rabbit", "rabbitmq", "pubsub", "publish",
            "subscribe", "topic", "topics", "consumer", "producer", "broker",
            "event", "events", "eventbus", "nats", "amqp", "pika", "celery", "dispatch"
        },
        "side_effect": "Async Event Stream (Message Broker Pub/Sub)",
        "risk_level": "medium",
    },
    {
        "category": "HTTP Client",
        "keywords": {
            "http", "https", "rest", "rpc", "grpc", "client", "request", "requests",
            "fetch", "axios", "httpx", "aiohttp", "endpoint", "api", "curl", "webhook"
        },
        "side_effect": "Outbound Network I/O (Remote API Call)",
        "risk_level": "medium",
    },
    {
        "category": "Cloud Storage",
        "keywords": {
            "cloud", "aws", "gcp", "azure", "s3", "blob", "bucket", "upload",
            "download", "storage", "minio", "objectstore"
        },
        "side_effect": "Cloud Infrastructure (Blob / Object Storage I/O)",
        "risk_level": "high",
    },
    {
        "category": "Logging & Observability",
        "keywords": {
            "log", "logger", "logging", "trace", "tracing", "telemetry", "otel",
            "metric", "metrics", "monitor", "monitoring", "alert", "sentry",
            "datadog", "prometheus", "winston"
        },
        "side_effect": "Non-blocking Telemetry (Log/Metric Emission)",
        "risk_level": "low",
    },
]


@dataclass
class _PackageEvidence:
    package: str
    methods: set[str] = field(default_factory=set)
    caller_nodes: set[str] = field(default_factory=set)
    caller_files: set[str] = field(default_factory=set)
    receiver_vars: set[str] = field(default_factory=set)
    manifest_desc: str = ""


def _tokenize(text: str) -> list[str]:
    """Tokenize package names, method names, and paths into lowercase constituent words."""
    # Split on non-alphanumeric, camelCase, snake_case, kebab-case, and path separators
    parts = re.findall(r'[A-Za-z]+|\d+', text)
    tokens: list[str] = []
    for p in parts:
        # Split camelCase
        sub = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|\d+', p)
        for s in sub:
            tokens.append(s.lower())
    return tokens


class DynamicLibraryCatalog:
    """Dynamic discovery engine that identifies external libraries and infers their roles."""

    def __init__(self):
        self._evidence: dict[str, _PackageEvidence] = {}
        self._manifest_packages: dict[str, dict] = {}
        self._inferred_cache: dict[str, ExternalLibraryInfo] = {}

    def clear(self):
        self._evidence.clear()
        self._manifest_packages.clear()
        self._inferred_cache.clear()

    def record_call(self, package: str, method: str, caller_node: str,
                    caller_file: str, receiver_var: str = ""):
        """Record an observed invocation of an external library symbol."""
        clean_pkg = package.strip()
        if not clean_pkg:
            return
        if clean_pkg not in self._evidence:
            self._evidence[clean_pkg] = _PackageEvidence(package=clean_pkg)

        ev = self._evidence[clean_pkg]
        if method:
            ev.methods.add(method)
        if caller_node:
            ev.caller_nodes.add(caller_node)
        if caller_file:
            ev.caller_files.add(caller_file)
        if receiver_var:
            ev.receiver_vars.add(receiver_var)

        # Invalidate cached classification if new methods or contexts appear
        self._inferred_cache.pop(clean_pkg, None)

    def scan_manifests(self, workspace_root: str | Path):
        """Scan package manifests (package.json, pom.xml, pyproject.toml, go.mod, etc.)."""
        root = Path(workspace_root)
        if not root.exists():
            return

        # 1. package.json
        for pkg_json in root.glob("**/package.json"):
            if "node_modules" in pkg_json.parts:
                continue
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
                desc = data.get("description", "")
                for dep_map in ["dependencies", "devDependencies", "peerDependencies"]:
                    for pkg_name in data.get(dep_map, {}).keys():
                        self._manifest_packages[pkg_name] = {"description": desc}
                        if pkg_name in self._evidence:
                            self._evidence[pkg_name].manifest_desc = desc
            except Exception as e:
                logger.debug(f"Could not parse {pkg_json}: {e}")

        # 2. requirements.txt / pyproject.toml
        for req_txt in root.glob("**/requirements*.txt"):
            try:
                for line in req_txt.read_text(encoding="utf-8").splitlines():
                    line = line.strip().split("#")[0].split("==")[0].split(">=")[0].strip()
                    if line:
                        self._manifest_packages[line] = {"description": ""}
            except Exception:
                pass

        # 3. pom.xml (Maven / Java)
        for pom_xml in root.glob("**/pom.xml"):
            try:
                import xml.etree.ElementTree as ET
                content = pom_xml.read_text(encoding="utf-8")
                cleaned_xml = re.sub(r'\sxmlns="[^"]+"', '', content, count=1)
                tree = ET.fromstring(cleaned_xml)
                for dep in tree.findall(".//dependency"):
                    gid = dep.findtext("groupId", "").strip()
                    aid = dep.findtext("artifactId", "").strip()
                    desc = dep.findtext("description", "").strip()
                    for key in [f"{gid}:{aid}", aid, gid]:
                        if key:
                            self._manifest_packages[key] = {"description": desc}
                            if key in self._evidence:
                                self._evidence[key].manifest_desc = desc
            except Exception as e:
                logger.debug(f"Could not parse {pom_xml}: {e}")

        # 4. go.mod (Go)
        for go_mod in root.glob("**/go.mod"):
            try:
                for line in go_mod.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("//") and not line.startswith("module") and not line.startswith("go "):
                        parts = line.split()
                        if parts:
                            pkg = parts[0]
                            self._manifest_packages[pkg] = {"description": ""}
            except Exception:
                pass

    def classify(self, package_or_receiver: str, method: str = "") -> ExternalLibraryInfo:
        """Classify an external library using curated registries and autonomous semantic inference."""
        raw = package_or_receiver.strip()
        clean = raw.lower().split(".")[0].strip()
        if "/" in clean:
            # Handle scoped npm packages e.g. @company/vault -> company/vault
            clean = clean.lstrip("@")

        # 1. Fast-path: curated registry (known libraries and standard libraries)
        if clean in LIBRARY_REGISTRY:
            return LIBRARY_REGISTRY[clean]
        for key, info in LIBRARY_REGISTRY.items():
            if clean == key or clean.endswith(f"/{key}") or clean.startswith(f"{key}-"):
                return info

        # 2. Check if already inferred and cached
        if raw in self._inferred_cache:
            return self._inferred_cache[raw]

        # 3. Dynamic Semantic Inference from Evidence
        ev = self._evidence.get(raw) or self._evidence.get(clean) or _PackageEvidence(package=raw)
        all_methods = set(ev.methods)
        if method:
            all_methods.add(method)

        # Collect evidence tokens
        pkg_tokens = _tokenize(raw)
        method_tokens = [t for m in all_methods for t in _tokenize(m)]
        # Caller file names are deliberately *not* evidence: json.dumps called
        # from payments.py is still JSON encoding, not a payment gateway.
        var_tokens = [t for v in ev.receiver_vars for t in _tokenize(v)]
        desc_tokens = _tokenize(ev.manifest_desc)

        # Score across archetypes
        best_archetype = None
        best_score = 0
        matched_indicators = []

        for arch in _ARCHETYPES:
            keywords = arch["keywords"]
            score = 0
            indicators = []

            # Method matches carry highest weight (3x)
            for t in method_tokens:
                if t in keywords:
                    score += 3
                    indicators.append(f"method:{t}")

            # Package name tokens carry strong weight (2x)
            for t in pkg_tokens:
                if t in keywords:
                    score += 2
                    indicators.append(f"package:{t}")

            # Receiver variable tokens (2x)
            for t in var_tokens:
                if t in keywords:
                    score += 2
                    indicators.append(f"var:{t}")

            # Manifest description tokens (1x)
            for t in desc_tokens:
                if t in keywords:
                    score += 1
                    indicators.append(f"manifest:{t}")

            # A generic method name alone (update, save, get) is not enough:
            # require evidence from the package, the variable, or the manifest.
            if not any(not i.startswith("method:") for i in indicators):
                continue

            if score > best_score:
                best_score = score
                best_archetype = arch
                matched_indicators = indicators

        if best_archetype and best_score >= 2:
            methods_str = ", ".join(sorted(all_methods)[:4]) if all_methods else "call"
            side_effect = best_archetype["side_effect"].format(methods_str)
            confidence = min(1.0, 0.5 + (best_score * 0.1))
            info = ExternalLibraryInfo(
                package=raw,
                category=best_archetype["category"],
                side_effect=side_effect,
                risk_level=best_archetype["risk_level"],
                confidence=round(confidence, 2),
                discovered_methods=sorted(all_methods),
                callers=sorted(ev.caller_files),
                evidence=matched_indicators,
            )
        else:
            # Fallback to standard external code execution
            info = ExternalLibraryInfo(
                package=raw,
                category="Third-Party Library",
                side_effect="External Code Execution",
                risk_level="medium",
                confidence=0.4,
                discovered_methods=sorted(all_methods),
                callers=sorted(ev.caller_files),
                evidence=[],
            )

        self._inferred_cache[raw] = info
        return info

    def get_catalog(self) -> dict[str, dict]:
        """Return the complete dynamically generated library catalog."""
        catalog = {}
        # Classify every package that was observed in the codebase
        for pkg in sorted(self._evidence.keys()):
            info = self.classify(pkg)
            catalog[pkg] = {
                "package": info.package,
                "category": info.category,
                "side_effect": info.side_effect,
                "risk_level": info.risk_level,
                "confidence": info.confidence,
                "discovered_methods": info.discovered_methods,
                "callers": info.callers,
                "evidence": info.evidence,
            }
        return catalog

    def export_catalog_json(self, output_path: str | Path) -> str:
        """Export the dynamically generated catalog to a JSON file."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        catalog = self.get_catalog()
        path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        return str(path)


# Global singleton catalog instance
GLOBAL_CATALOG = DynamicLibraryCatalog()


def classify_library(package_or_receiver: str, method: str = "") -> ExternalLibraryInfo:
    """Classify an external library or receiver into its architectural role."""
    return GLOBAL_CATALOG.classify(package_or_receiver, method=method)
