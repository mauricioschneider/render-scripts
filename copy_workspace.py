#!/usr/bin/env python3
"""
copy_workspace.py

Copies projects, environments, services, databases (Postgres + Redis), and
environment variables from one Render workspace to another via the REST API v1.

Usage:
    python copy_workspace.py \
        --src-owner-id tea_xxxx \
        --dst-owner-id tea_yyyy \
        [--execute]

    API keys are read from environment variables:
        RENDER_SRC_API_KEY   API key for the source workspace
        RENDER_DST_API_KEY   API key for the destination workspace

    You may also pass them as flags (--src-api-key / --dst-api-key), but prefer
    environment variables to avoid keys appearing in shell history or ps output.

    Default mode is dry-run. Pass --execute to make real changes.

SECURITY NOTES:
    - Environment variable VALUES are copied in plaintext (API keys, secrets, etc).
      Treat the output of this script as sensitive — do not log it or share it.
    - Pass API keys via environment variables, not CLI flags, to avoid exposure
      in shell history and process listings.
    - Running with --execute immediately writes to the destination workspace.
      Always do a dry-run first to review what will be copied.

OTHER NOTES:
    - Database data is NOT migrated — only the instance configuration.
    - Services that already exist in the destination (by name) are skipped
      unless --overwrite-env-vars is passed (which updates their env vars only).
    - Read replicas are not recreated.
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

API_BASE = "https://api.render.com/v1"

COPYABLE_SERVICE_TYPES = {
    "web_service",
    "private_service",
    "background_worker",
    "cron_job",
    "static_site",
}


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class RenderClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, params: dict = None) -> list | dict:
        resp = self.session.get(f"{API_BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        resp = self.session.post(f"{API_BASE}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, body: list | dict) -> list | dict:
        resp = self.session.put(f"{API_BASE}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    def get_owner(self, owner_id: str) -> dict:
        """Fetch a single owner by ID — used to validate API key has access to this workspace."""
        return self._get(f"/owners/{owner_id}")

    def _paginate(self, path: str, params: dict = None, item_key: str = None) -> list:
        results = []
        cursor = None
        params = dict(params or {})
        params.setdefault("limit", 100)

        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._get(path, params=params)
            if not data:
                break

            if item_key:
                results.extend(row[item_key] for row in data if item_key in row)
            else:
                results.extend(data)

            cursor = data[-1].get("cursor") if isinstance(data, list) and data else None
            if not cursor or len(data) < params["limit"]:
                break

        return results

    # -- Projects ------------------------------------------------------------

    def list_projects(self, owner_id: str) -> list[dict]:
        return self._paginate("/projects", params={"ownerId": owner_id}, item_key="project")

    def create_project(self, owner_id: str, name: str, first_env_name: str) -> dict:
        # The API requires at least one environment — it cannot be empty.
        return self._post("/projects", {
            "ownerId": owner_id,
            "name": name,
            "environments": [{"name": first_env_name}],
        })

    # -- Environments --------------------------------------------------------

    def list_environments(self, project_id: str) -> list[dict]:
        return self._paginate("/environments", params={"projectId": project_id}, item_key="environment")

    def create_environment(self, project_id: str, name: str) -> dict:
        return self._post("/environments", {"projectId": project_id, "name": name})

    def add_resources_to_environment(self, env_id: str, resource_ids: list[str]) -> dict:
        return self._post(f"/environments/{env_id}/resources", {"resourceIds": resource_ids})

    # -- Services ------------------------------------------------------------

    def list_services(self, owner_id: str) -> list[dict]:
        return self._paginate("/services", params={"ownerId": owner_id}, item_key="service")

    def create_service(self, body: dict) -> dict:
        return self._post("/services", body)

    # -- Postgres ------------------------------------------------------------

    def list_postgres(self, owner_id: str) -> list[dict]:
        return self._paginate("/postgres", params={"ownerId": owner_id}, item_key="postgres")

    def create_postgres(self, body: dict) -> dict:
        return self._post("/postgres", body)

    # -- Redis ---------------------------------------------------------------

    def list_redis(self, owner_id: str) -> list[dict]:
        return self._paginate("/redis", params={"ownerId": owner_id}, item_key="redis")

    def create_redis(self, body: dict) -> dict:
        return self._post("/redis", body)

    # -- Env vars ------------------------------------------------------------

    def list_env_vars(self, service_id: str) -> list[dict]:
        return self._paginate(f"/services/{service_id}/env-vars", item_key="envVar")

    def put_env_vars(self, service_id: str, env_vars: list[dict]) -> list:
        return self._put(f"/services/{service_id}/env-vars", env_vars)


# ---------------------------------------------------------------------------
# Copy result tracker
# ---------------------------------------------------------------------------

@dataclass
class CopyResult:
    projects_copied: int = 0
    environments_copied: int = 0
    services_copied: int = 0
    services_skipped: int = 0
    postgres_copied: int = 0
    redis_copied: int = 0
    env_vars_copied: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ip_allow_list(src: list[dict]) -> list[dict]:
    """Normalize ipAllowList entries for a POST body."""
    return [
        {"cidrBlock": e["cidrBlock"], "description": e.get("description", "")}
        for e in (src or [])
        if e.get("cidrBlock")
    ]


def build_service_body(svc: dict, owner_id: str, dst_env_id: Optional[str]) -> Optional[dict]:
    svc_type = svc.get("type")
    if svc_type not in COPYABLE_SERVICE_TYPES:
        return None

    body: dict = {
        "type": svc_type,
        "name": svc.get("name"),
        "ownerId": owner_id,
    }
    if dst_env_id:
        body["environmentId"] = dst_env_id

    repo = svc.get("repo") or {}
    if isinstance(repo, str):
        repo_url = repo
    else:
        repo_url = repo.get("url")
    if repo_url:
        body["repo"] = repo_url
    for key in ("branch", "rootDir", "autoDeploy", "region"):
        if svc.get(key) is not None:
            body[key] = svc[key]

    details = svc.get("serviceDetails") or {}
    svc_detail_keys = {
        "web_service":       ["env", "plan", "numInstances", "buildCommand", "startCommand",
                              "healthCheckPath", "dockerCommand", "dockerContext",
                              "dockerfilePath", "preDeployCommand"],
        "private_service":   ["env", "plan", "numInstances", "buildCommand", "startCommand",
                              "dockerCommand", "dockerContext", "dockerfilePath", "preDeployCommand"],
        "background_worker": ["env", "plan", "numInstances", "buildCommand", "startCommand",
                              "dockerCommand", "dockerContext", "dockerfilePath", "preDeployCommand"],
        "cron_job":          ["env", "plan", "buildCommand", "startCommand", "schedule",
                              "dockerCommand", "dockerContext", "dockerfilePath"],
        "static_site":       ["buildCommand", "publishPath", "pullRequestPreviewsEnabled",
                              "headers", "routes"],
    }
    service_details = {k: details[k] for k in svc_detail_keys.get(svc_type, []) if details.get(k) is not None}
    if service_details:
        body["serviceDetails"] = service_details

    return body


def build_postgres_body(pg: dict, owner_id: str, dst_env_id: Optional[str]) -> dict:
    body: dict = {
        "name": pg["name"],
        "ownerId": owner_id,
        "plan": pg["plan"],
        "version": pg["version"],
    }
    if dst_env_id:
        body["environmentId"] = dst_env_id
    for key in ("region", "databaseName", "databaseUser", "diskSizeGB"):
        if pg.get(key) is not None:
            body[key] = pg[key]
    if pg.get("highAvailabilityEnabled"):
        body["enableHighAvailability"] = True
    if pg.get("diskAutoscalingEnabled"):
        body["enableDiskAutoscaling"] = True
    ip_list = _ip_allow_list(pg.get("ipAllowList", []))
    if ip_list:
        body["ipAllowList"] = ip_list
    return body


def build_redis_body(rd: dict, owner_id: str, dst_env_id: Optional[str]) -> dict:
    body: dict = {
        "name": rd["name"],
        "ownerId": owner_id,
        "plan": rd["plan"],
    }
    if dst_env_id:
        body["environmentId"] = dst_env_id
    if rd.get("region"):
        body["region"] = rd["region"]
    options = rd.get("options") or {}
    if options.get("maxmemoryPolicy"):
        body["maxmemoryPolicy"] = options["maxmemoryPolicy"]
    ip_list = _ip_allow_list(rd.get("ipAllowList", []))
    if ip_list:
        body["ipAllowList"] = ip_list
    return body


def _confirm_execute(src_name: str, dst_name: str) -> None:
    """Prompt the user to confirm before writing to the destination workspace."""
    print()
    print("⚠️  WARNING: This will copy resources to the destination workspace.")
    print(f"   Source:      {src_name}")
    print(f"   Destination: {dst_name}")
    print()
    print("   Environment variable VALUES (secrets, API keys, etc.) will be")
    print("   copied in plaintext to the destination workspace.")
    print()
    answer = input("Type 'yes' to proceed: ").strip().lower()
    if answer != "yes":
        print("Aborted.")
        sys.exit(0)
    print()


# ---------------------------------------------------------------------------
# Main copy logic
# ---------------------------------------------------------------------------

def copy_workspace(
    src: RenderClient,
    dst: RenderClient,
    src_owner_id: str,
    dst_owner_id: str,
    dry_run: bool = True,
    overwrite_env_vars: bool = False,
) -> CopyResult:
    result = CopyResult()

    # ------------------------------------------------------------------ #
    # 1. Projects + Environments                                           #
    # ------------------------------------------------------------------ #
    print("\n=== Projects & Environments ===")
    src_projects = src.list_projects(src_owner_id)
    print(f"Found {len(src_projects)} project(s) in source workspace.")

    # src_env_id -> dst_env_id
    env_id_map: dict[str, str] = {}
    # src_project_id -> dst_project_id
    project_id_map: dict[str, str] = {}

    for proj in src_projects:
        proj_name = proj.get("name", "<unnamed>")
        proj_id = proj["id"]

        src_envs = src.list_environments(proj_id)
        first_env_name = src_envs[0].get("name", "production") if src_envs else "production"

        if dry_run:
            print(f"  [dry-run] Would create project: {proj_name!r}")
            dst_proj_id = f"<dry-run:{proj_id}>"
        else:
            print(f"  Creating project: {proj_name!r} ...", end=" ", flush=True)
            try:
                dst_proj = dst.create_project(dst_owner_id, proj_name, first_env_name)
                dst_proj_id = dst_proj["id"]
                print(f"created ({dst_proj_id})")
                result.projects_copied += 1
            except requests.HTTPError as e:
                _error(result, f"Failed to create project {proj_name!r}: {_http_err(e)}")
                continue

        project_id_map[proj_id] = dst_proj_id

        # The first environment was already created inline with the project.
        # Re-fetch the destination environments to get its assigned ID, then
        # create any remaining environments individually.
        if not dry_run:
            dst_envs = dst.list_environments(dst_proj_id)
            dst_env_by_name = {e.get("name"): e["id"] for e in dst_envs}

        for i, env in enumerate(src_envs):
            env_name = env.get("name", "<unnamed>")
            src_env_id = env["id"]

            if dry_run:
                print(f"    [dry-run] Would create environment: {env_name!r}")
                env_id_map[src_env_id] = f"<dry-run:{src_env_id}>"
            elif i == 0:
                # Already created inline with the project
                dst_env_id = dst_env_by_name.get(env_name)
                if dst_env_id:
                    env_id_map[src_env_id] = dst_env_id
                    print(f"    Environment {env_name!r} created inline with project ({dst_env_id})")
                    result.environments_copied += 1
            else:
                print(f"    Creating environment: {env_name!r} ...", end=" ", flush=True)
                try:
                    dst_env = dst.create_environment(dst_proj_id, env_name)
                    dst_env_id = dst_env["id"]
                    env_id_map[src_env_id] = dst_env_id
                    print(f"created ({dst_env_id})")
                    result.environments_copied += 1
                except requests.HTTPError as e:
                    _error(result, f"Failed to create environment {env_name!r}: {_http_err(e)}")

    # ------------------------------------------------------------------ #
    # 2. Postgres databases                                                #
    # ------------------------------------------------------------------ #
    print("\n=== Postgres Databases ===")
    src_postgres = src.list_postgres(src_owner_id)
    print(f"Found {len(src_postgres)} Postgres instance(s).")

    if not dry_run:
        dst_postgres_names = {pg.get("name") for pg in dst.list_postgres(dst_owner_id)}
    else:
        dst_postgres_names = set()

    for pg in src_postgres:
        pg_name = pg.get("name", "<unnamed>")
        src_env_id = pg.get("environmentId")
        dst_env_id = env_id_map.get(src_env_id) if src_env_id else None
        body = build_postgres_body(pg, dst_owner_id, dst_env_id)

        if dry_run:
            print(f"  [dry-run] Would create Postgres: {pg_name!r} "
                  f"(plan={pg.get('plan')}, version={pg.get('version')}, region={pg.get('region')})")
            continue

        if pg_name in dst_postgres_names:
            print(f"  Skipping Postgres {pg_name!r} — already exists in destination")
            continue

        print(f"  Creating Postgres: {pg_name!r} ...", end=" ", flush=True)
        try:
            created = dst.create_postgres(body)
            print(f"created ({created.get('id', '?')})")
            result.postgres_copied += 1
            time.sleep(0.3)
        except requests.HTTPError as e:
            _error(result, f"Failed to create Postgres {pg_name!r}: {_http_err(e)}")

    # ------------------------------------------------------------------ #
    # 3. Redis instances                                                   #
    # ------------------------------------------------------------------ #
    print("\n=== Redis Instances ===")
    src_redis = src.list_redis(src_owner_id)
    print(f"Found {len(src_redis)} Redis instance(s).")

    if not dry_run:
        dst_redis_names = {rd.get("name") for rd in dst.list_redis(dst_owner_id)}
    else:
        dst_redis_names = set()

    for rd in src_redis:
        rd_name = rd.get("name", "<unnamed>")
        src_env_id = rd.get("environmentId")
        dst_env_id = env_id_map.get(src_env_id) if src_env_id else None
        body = build_redis_body(rd, dst_owner_id, dst_env_id)

        if dry_run:
            print(f"  [dry-run] Would create Redis: {rd_name!r} "
                  f"(plan={rd.get('plan')}, region={rd.get('region')})")
            continue

        if rd_name in dst_redis_names:
            print(f"  Skipping Redis {rd_name!r} — already exists in destination")
            continue

        print(f"  Creating Redis: {rd_name!r} ...", end=" ", flush=True)
        try:
            created = dst.create_redis(body)
            print(f"created ({created.get('id', '?')})")
            result.redis_copied += 1
            time.sleep(0.3)
        except requests.HTTPError as e:
            _error(result, f"Failed to create Redis {rd_name!r}: {_http_err(e)}")

    # ------------------------------------------------------------------ #
    # 4. Services + env vars                                               #
    # ------------------------------------------------------------------ #
    print("\n=== Services ===")
    src_services = src.list_services(src_owner_id)
    print(f"Found {len(src_services)} service(s).")

    if not dry_run:
        dst_services = dst.list_services(dst_owner_id)
        dst_service_by_name: dict[str, dict] = {s.get("name"): s for s in dst_services}
    else:
        dst_service_by_name = {}

    for svc in src_services:
        svc_name = svc.get("name", "<unnamed>")
        svc_type = svc.get("type", "unknown")
        svc_id = svc["id"]

        if svc_type not in COPYABLE_SERVICE_TYPES:
            print(f"  Skipping {svc_name!r} (type={svc_type!r} — use Postgres/Redis sections above)")
            result.services_skipped += 1
            continue

        src_env_id = svc.get("environmentId")
        dst_env_id = env_id_map.get(src_env_id) if src_env_id else None
        body = build_service_body(svc, dst_owner_id, dst_env_id)
        if body is None:
            print(f"  Skipping {svc_name!r} (could not build creation body)")
            result.services_skipped += 1
            continue

        if dry_run:
            print(f"  [dry-run] Would create service: {svc_name!r} (type={svc_type!r})")
            env_vars = src.list_env_vars(svc_id)
            if env_vars:
                print(f"    [dry-run] Would copy {len(env_vars)} env var(s) "
                      f"(keys: {', '.join(ev['key'] for ev in env_vars)})")
            continue

        existing = dst_service_by_name.get(svc_name)
        if existing and not overwrite_env_vars:
            print(f"  Skipping {svc_name!r} — already exists (pass --overwrite-env-vars to update its env vars)")
            result.services_skipped += 1
            continue

        if existing:
            dst_svc_id = existing["id"]
            print(f"  {svc_name!r} already exists — updating env vars only")
        else:
            print(f"  Creating service: {svc_name!r} (type={svc_type!r}) ...", end=" ", flush=True)
            try:
                created = dst.create_service(body)
                # Response may be wrapped: {"service": {...}, "deployId": "..."}
                dst_svc_id = (created.get("service") or created).get("id")
                print(f"created ({dst_svc_id})")
                result.services_copied += 1
                time.sleep(0.3)
            except requests.HTTPError as e:
                _error(result, f"Failed to create service {svc_name!r}: {_http_err(e)}")
                continue

        _copy_env_vars(src, dst, svc_id, dst_svc_id, svc_name, result)

    return result


def _copy_env_vars(
    src: RenderClient,
    dst: RenderClient,
    src_svc_id: str,
    dst_svc_id: str,
    svc_name: str,
    result: CopyResult,
) -> None:
    try:
        env_vars = src.list_env_vars(src_svc_id)
    except requests.HTTPError as e:
        _error(result, f"Failed to read env vars for {svc_name!r}: {e}")
        return

    if not env_vars:
        return

    payload = [{"key": ev["key"], "value": ev.get("value", "")} for ev in env_vars]
    try:
        dst.put_env_vars(dst_svc_id, payload)
        print(f"    Copied {len(payload)} env var(s)")
        result.env_vars_copied += len(payload)
    except requests.HTTPError as e:
        _error(result, f"Failed to write env vars for {svc_name!r}: {_http_err(e)}")


def _error(result: CopyResult, msg: str) -> None:
    print(f"  ERROR — {msg}")
    result.errors.append(msg)


def _http_err(e: requests.HTTPError) -> str:
    return e.response.text if e.response is not None else str(e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Copy a Render workspace (projects, environments, services, databases, env vars) to another workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "API keys can also be supplied via environment variables:\n"
            "  RENDER_SRC_API_KEY   API key for the source workspace\n"
            "  RENDER_DST_API_KEY   API key for the destination workspace\n"
            "\n"
            "Using environment variables is preferred over --src-api-key / --dst-api-key\n"
            "to avoid exposing keys in shell history and process listings."
        ),
    )
    p.add_argument("--src-api-key",  default=None,
                   help="API key for the source workspace (overrides RENDER_SRC_API_KEY)")
    p.add_argument("--src-owner-id", required=True,
                   help="Owner ID (tea-/usr-) of the source workspace")
    p.add_argument("--dst-api-key",  default=None,
                   help="API key for the destination workspace (overrides RENDER_DST_API_KEY)")
    p.add_argument("--dst-owner-id", required=True,
                   help="Owner ID (tea-/usr-) of the destination workspace")
    p.add_argument("--execute",      action="store_true",
                   help="Actually perform the copy (default is dry-run)")
    p.add_argument("--overwrite-env-vars", action="store_true",
                   help="For services that already exist in the destination, overwrite their env vars")
    return p.parse_args()


def _resolve_api_key(flag_value: Optional[str], env_var: str, label: str) -> str:
    key = flag_value or os.environ.get(env_var)
    if not key:
        print(f"ERROR: {label} API key not provided. "
              f"Pass --{label.lower().replace(' ', '-')}-api-key or set {env_var}.")
        sys.exit(1)
    if flag_value:
        print(f"WARNING: {label} API key passed as a CLI flag. "
              f"Consider using {env_var} instead to avoid shell history exposure.")
    return key


def main() -> None:
    args = parse_args()
    dry_run = not args.execute

    src_api_key = _resolve_api_key(args.src_api_key, "RENDER_SRC_API_KEY", "source")
    dst_api_key = _resolve_api_key(args.dst_api_key, "RENDER_DST_API_KEY", "destination")

    src = RenderClient(src_api_key)
    dst = RenderClient(dst_api_key)

    # Validate credentials and owner IDs before doing anything
    print("Validating API keys and owner IDs...")
    owners = {}
    for label, client, owner_id in [("source", src, args.src_owner_id), ("destination", dst, args.dst_owner_id)]:
        try:
            owner = client.get_owner(owner_id)
            owners[label] = owner
            print(f"  {label.capitalize()} owner {owner_id!r} ({owner.get('name', '?')}) OK")
        except requests.HTTPError as e:
            print(f"ERROR: Could not access {label} owner {owner_id!r}: {_http_err(e)}")
            sys.exit(1)

    if dry_run:
        print("\n*** DRY RUN — no changes will be made. Pass --execute to run for real. ***")
        print("    Env var keys (but not values) will be shown in dry-run output.")
    else:
        _confirm_execute(
            src_name=f"{owners['source'].get('name', '?')} ({args.src_owner_id})",
            dst_name=f"{owners['destination'].get('name', '?')} ({args.dst_owner_id})",
        )

    result = copy_workspace(
        src=src,
        dst=dst,
        src_owner_id=args.src_owner_id,
        dst_owner_id=args.dst_owner_id,
        dry_run=dry_run,
        overwrite_env_vars=args.overwrite_env_vars,
    )

    print("\n=== Summary ===")
    print(f"  Projects copied:     {result.projects_copied}")
    print(f"  Environments copied: {result.environments_copied}")
    print(f"  Postgres copied:     {result.postgres_copied}")
    print(f"  Redis copied:        {result.redis_copied}")
    print(f"  Services copied:     {result.services_copied}")
    print(f"  Services skipped:    {result.services_skipped}")
    print(f"  Env vars copied:     {result.env_vars_copied}")

    if result.errors:
        print(f"\n  Errors ({len(result.errors)}):")
        for e in result.errors:
            print(f"    - {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
