"""Create/refresh the Render web service for THAI_VIET_WEBTALK and wait for the deploy.

Usage
-----
    python scripts/deploy_render.py --repo https://github.com/USER/THAI_VIET_WEBTALK
    python scripts/deploy_render.py --repo ... --apply        # actually create/deploy

Defaults to a dry run: it prints the exact API call it would make and stops.
The token is read from ``--token``, ``RENDER_API_TOKEN``, or
``../render_api.txt`` (the file kept outside the repo) — never from a tracked file.

Requires a Git repository that Render can access: either the Git provider is
already connected to the account, or the repo is public.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(errors="replace")  # owner names may be non-cp1252

API = "https://api.render.com/v1"
SERVICE_NAME = "thai-viet-webtalk"
TOKEN_FILE = pathlib.Path(__file__).resolve().parent.parent.parent / "render_api.txt"

BUILD = "pip install -r backend/requirements.txt"
START = (
    "uvicorn backend.main:app --host 0.0.0.0 --port $PORT "
    "--ws websockets --ws-max-size 1048576"
)


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("RENDER_API_TOKEN", "").strip()
    if env:
        return env
    if TOKEN_FILE.exists():
        for part in TOKEN_FILE.read_text(encoding="utf-8", errors="replace").split():
            if part.startswith("rnd_"):
                return part
    raise SystemExit(
        f"No token. Pass --token, set RENDER_API_TOKEN, or create {TOKEN_FILE}"
    )


def call(token: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")[:600]


def pick_owner(token: str) -> str:
    status, owners = call(token, "GET", "/owners?limit=10")
    if status != 200 or not owners:
        raise SystemExit(f"cannot list owners: {status} {owners}")
    owner = owners[0].get("owner", owners[0])
    print(f"owner: {owner.get('name')} ({owner.get('type')}) -> {owner.get('id')}")
    return owner["id"]


def existing_service(token: str) -> dict | None:
    status, services = call(token, "GET", f"/services?name={SERVICE_NAME}&limit=20")
    if status != 200 or not services:
        return None
    for entry in services:
        svc = entry.get("service", entry)
        if svc.get("name") == SERVICE_NAME:
            return svc
    return None


def service_payload(owner_id: str, repo: str, branch: str) -> dict:
    return {
        "type": "web_service",
        "name": SERVICE_NAME,
        "ownerId": owner_id,
        "repo": repo,
        "branch": branch,
        "autoDeploy": "yes",
        "serviceDetails": {
            "env": "python",
            "plan": "free",
            "region": "singapore",
            "numInstances": 1,
            "healthCheckPath": "/healthz",
            "envSpecificDetails": {
                "buildCommand": BUILD,
                "startCommand": START,
            },
        },
    }


def wait_for_deploy(token: str, service_id: str, deploy_id: str, minutes: int = 20) -> str:
    deadline = time.time() + minutes * 60
    last = None
    while time.time() < deadline:
        status, deploy = call(token, "GET", f"/services/{service_id}/deploys/{deploy_id}")
        if status != 200:
            time.sleep(5)
            continue
        state = deploy.get("status")
        if state != last:
            print(f"  deploy status: {state}")
            last = state
        if state in {"live", "build_failed", "canceled", "deactivated", "failed"}:
            return state
        time.sleep(10)
    return "timeout"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="https://github.com/USER/REPO")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--token")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually create the service and trigger a deploy (otherwise dry run)",
    )
    args = parser.parse_args()

    token = resolve_token(args.token)
    owner_id = pick_owner(token)

    service = existing_service(token)
    payload = service_payload(owner_id, args.repo, args.branch)

    if service is not None:
        print(f"service exists: {service.get('name')} -> {service.get('id')}")
        service_id = service["id"]
    else:
        print("service does not exist yet; would POST /services with:")
        print(json.dumps(payload, indent=2))
        if not args.apply:
            print("\nDRY RUN — rerun with --apply to create it.")
            return
        status, created = call(token, "POST", "/services", payload)
        if status not in (200, 201):
            raise SystemExit(f"create failed: {status} {created}")
        service = created.get("service", created)
        service_id = service["id"]
        print(f"created service {service_id}")

    if not args.apply:
        print("\nDRY RUN — would POST /services/{id}/deploys next.")
        return

    status, deploy = call(
        token, "POST", f"/services/{service_id}/deploys", {"clearCache": "do_not_clear"}
    )
    if status not in (200, 201, 202):
        raise SystemExit(f"deploy trigger failed: {status} {deploy}")

    deploy_id = None
    if isinstance(deploy, dict):
        deploy_id = deploy.get("id") or (deploy.get("deploy") or {}).get("id")

    if not deploy_id:
        # Render sometimes answers 202 with an empty body: fall back to the
        # newest deploy for this service.
        status, deploys = call(token, "GET", f"/services/{service_id}/deploys?limit=1")
        if status == 200 and deploys:
            entry = deploys[0]
            deploy_id = (entry.get("deploy", entry) or {}).get("id")
    if not deploy_id:
        raise SystemExit(f"could not find a deploy id (last body: {deploy!r})")

    print(f"deploy started: {deploy_id}")

    final = wait_for_deploy(token, service_id, deploy_id)
    url = service.get("serviceDetails", {}).get("url") or service.get("url")
    print(f"\nfinal status: {final}")
    print(f"url: {url}")
    if final != "live":
        print("check logs: python scripts/deploy_render.py --logs", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
