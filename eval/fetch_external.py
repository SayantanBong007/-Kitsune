"""
Fetch the public microservice systems used by eval/run_system_eval.py.

They are cloned at pinned commits into eval/external/ (git-ignored), so results
are reproducible and the Kitsune repository stays small.

    python kitsune/eval/fetch_external.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXTERNAL = Path(__file__).resolve().parent / "external"

# (folder, repository, commit)
REPOS = [
    ("sockshop/front-end", "https://github.com/microservices-demo/front-end.git", "52dee65"),
    ("sockshop/catalogue", "https://github.com/microservices-demo/catalogue.git", "925e08e"),
    ("sockshop/carts", "https://github.com/microservices-demo/carts.git", "f4e8005"),
    ("sockshop/orders", "https://github.com/microservices-demo/orders.git", "546a10c"),
    ("sockshop/payment", "https://github.com/microservices-demo/payment.git", "384e334"),
    ("sockshop/user", "https://github.com/microservices-demo/user.git", "e1a79e7"),
    ("sockshop/shipping", "https://github.com/microservices-demo/shipping.git", "9c0fbfa"),
    ("sockshop/queue-master", "https://github.com/microservices-demo/queue-master.git", "7dc3372"),
    ("boutique", "https://github.com/GoogleCloudPlatform/microservices-demo.git", "38e7348"),
    ("petclinic-ms", "https://github.com/spring-petclinic/spring-petclinic-microservices.git", "e8b747a"),
    ("robot-shop", "https://github.com/instana/robot-shop.git", "55292e2"),
]


def run(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def main():
    for folder, url, commit in REPOS:
        dest = EXTERNAL / folder
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"cloning {url} -> {dest}")
            run("git", "clone", "--quiet", "--filter=blob:none", url, str(dest))
        run("git", "-c", "advice.detachedHead=false", "checkout", "--quiet", commit, cwd=dest)
        print(f"{folder:28} @ {commit}")


if __name__ == "__main__":
    sys.exit(main())
