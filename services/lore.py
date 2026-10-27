"""
Kitsune's lore — what it learns as it ages.

A kitsune grows a new tail with every age of wisdom. This is where Kitsune keeps
what it has learned, so every system it reads makes it better at the next one:

  taught       lessons a person gave it, for one system: "this link is wrong",
               "host checkout-api is the checkout service", "env var PAY_GW points
               to payment". Trusted at once.
  learned      habits it noticed by itself, across systems: env-var naming
               conventions (CART_GATEWAY → cart) and in-house HTTP helpers
               (callService(url)). A habit is used only after it was seen in two
               different services, so one odd project can't teach a bad habit.
  blind spots  what it could not connect last time, so a person knows what to teach.

Stored as one JSON file (KITSUNE_LORE, default kitsune/data/lore.json).
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path

# A habit becomes a rule once seen in this many different services
EVIDENCE_NEEDED = 2
_MAX_BLIND_SPOTS = 200


def default_lore_path() -> Path:
    env = os.getenv("KITSUNE_LORE")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "data" / "lore.json"


def link_key(client: str, server: str, kind: str, method: str, path: str) -> str:
    return " ".join((client, server, kind, method or "-", path or "-"))


class Lore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_lore_path()
        self.data = {"version": 1, "systems": {}, "taught": {}, "observed": {}, "blind_spots": {}}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass   # unreadable lore: start young rather than fail indexing

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)   # atomic: a crash never leaves half a file

    # ── experience ───────────────────────────────────────────────────────

    def record_system(self, system: str):
        s = self.data["systems"].setdefault(system, {"first_seen": time.strftime("%Y-%m-%d"), "runs": 0})
        s["runs"] += 1
        s["last_seen"] = time.strftime("%Y-%m-%d")

    def observe(self, kind: str, value: str, evidence: str):
        """A habit seen once: `evidence` is "system/service" where it was seen."""
        seen = self.data["observed"].setdefault(kind, {}).setdefault(value, [])
        if evidence not in seen:
            seen.append(evidence)

    def learned(self, kind: str) -> set[str]:
        return {value for value, seen in self.data["observed"].get(kind, {}).items()
                if len(seen) >= EVIDENCE_NEEDED}

    def set_blind_spots(self, system: str, spots: list[dict]):
        self.data["blind_spots"][system] = spots[:_MAX_BLIND_SPOTS]

    def blind_spots(self, system: str) -> list[dict]:
        return self.data["blind_spots"].get(system, [])

    # ── lessons from a person ────────────────────────────────────────────

    def _taught(self, system: str) -> dict:
        return self.data["taught"].setdefault(system, {"links": {}, "hosts": {}, "env": {}})

    def teach_link(self, system: str, key: str, correct: bool):
        self._taught(system)["links"][key] = "confirm" if correct else "reject"

    def teach_host(self, system: str, alias: str, service: str):
        self._taught(system)["hosts"][alias.lower()] = service

    def teach_env(self, system: str, var: str, service: str):
        self._taught(system)["env"][var.upper()] = service

    def taught(self, system: str) -> dict:
        return self.data["taught"].get(system, {"links": {}, "hosts": {}, "env": {}})

    # ── what the extractor uses ──────────────────────────────────────────

    def hints(self, system: str) -> dict:
        t = self.taught(system)
        return {"hosts": dict(t.get("hosts", {})), "env": dict(t.get("env", {})),
                "env_suffixes": self.learned("env_suffix"),
                "http_clients": self.learned("http_client")}

    def apply_verdicts(self, system: str, links: list) -> list:
        """Drop links a person rejected; confirm the "possible" ones they confirmed."""
        verdicts = self.taught(system).get("links", {})
        if not verdicts:
            return links
        kept = []
        for link in links:
            verdict = verdicts.get(link_key(link.client_service, link.server_service, link.kind,
                                            link.method, link.path))
            if verdict == "reject":
                continue
            if verdict == "confirm" and link.confidence != "confirmed":
                link.confidence = "confirmed"
                link.reason += " (confirmed by a person)"
            kept.append(link)
        return kept

    # ── age ──────────────────────────────────────────────────────────────

    def age(self) -> dict:
        taught = sum(len(t.get(k, {})) for t in self.data["taught"].values()
                     for k in ("links", "hosts", "env"))
        learned = {kind: sorted(self.learned(kind)) for kind in self.data["observed"]}
        lessons = taught + sum(len(v) for v in learned.values()) + len(self.data["systems"])
        return {
            # one tail at birth, then one more each time its lessons double; nine at most
            "tails": min(9, 1 + int(math.log2(1 + lessons))),
            "lessons": lessons,
            "systems_seen": sorted(self.data["systems"]),
            "taught_lessons": taught,
            "learned_habits": learned,
            "habits_forming": {kind: sorted(v for v, seen in values.items() if len(seen) < EVIDENCE_NEEDED)
                               for kind, values in self.data["observed"].items()},
        }
