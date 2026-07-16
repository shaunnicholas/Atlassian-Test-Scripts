"""
Jira instance research script -- covers five areas in one run:

    1. Workflows per project        (which workflow scheme/workflow each project uses)
    2. Statuses in use vs. orphaned (defined globally but not referenced by any workflow)
    3. Custom fields inventory      (name, type, and which projects/contexts use them)
    4. Screens & field configurations (what's defined, and which project uses which)
    5. Priorities & resolutions in use (with issue counts; flags unused ones)

Each section requires *admin* access to config-level Jira endpoints (schemes,
field configs, contexts). If your account lacks permission for a section,
that section will print a warning and the script will continue with the rest
rather than crashing entirely.

Requirements:
    pip install requests python-dotenv

Setup:
    Same JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN as the other scripts
    (.env file), admin account recommended for full coverage.

Usage:
    python jira_instance_audit.py                     # run everything
    python jira_instance_audit.py --sections workflows,statuses
    python jira_instance_audit.py --sections fields --project MYPROJ
    python jira_instance_audit.py --sections priorities
"""

import os
import sys
import argparse
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")

REQUEST_TIMEOUT = 30
ALL_SECTIONS = ["workflows", "statuses", "fields", "screens", "priorities"]


# --------------------------------------------------------------------------
# Generic HTTP helpers
# --------------------------------------------------------------------------

def api_call(method, base_url, path, auth, params=None, json_body=None, accept=None):
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Accept": accept or "application/json"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    try:
        response = requests.request(
            method, url, headers=headers, auth=auth,
            params=params, json=json_body, timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Timed out calling {path}")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"Connection error calling {path}: {e}")

    if response.status_code in (401, 403):
        raise PermissionError(
            f"{response.status_code} calling {path} -- likely needs admin permissions."
        )
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code} calling {path}: {response.text[:300]}")

    if not response.text:
        return {}
    return response.json()


def paginate(base_url, path, auth, params=None):
    """Generic pagination for Jira's PageBean-style endpoints (startAt/maxResults/isLast/values)."""
    params = dict(params or {})
    params.setdefault("maxResults", 50)
    start_at = 0
    results = []
    while True:
        params["startAt"] = start_at
        data = api_call("GET", base_url, path, auth, params=params)
        values = data.get("values", [])
        results.extend(values)
        if data.get("isLast", True) or not values:
            break
        start_at += len(values)
    return results


def section_header(title):
    print(f"\n{'=' * 90}")
    print(f"  {title}")
    print(f"{'=' * 90}")


def get_projects(base_url, auth, project_filter=None):
    projects = paginate(base_url, "/rest/api/3/project/search", auth)
    if project_filter:
        projects = [p for p in projects if p.get("key") == project_filter]
    return [{"id": p["id"], "key": p["key"], "name": p["name"]} for p in projects]


# --------------------------------------------------------------------------
# 1. Workflows per project
# --------------------------------------------------------------------------

def run_workflows_section(base_url, auth, projects):
    section_header("1. WORKFLOWS PER PROJECT")
    print(f"{'PROJECT':<15} {'WORKFLOW SCHEME':<35} {'DEFAULT WORKFLOW':<30} {'OVERRIDES'}")
    print("-" * 110)

    for p in projects:
        try:
            data = api_call(
                "GET", base_url, "/rest/api/3/workflowscheme/project", auth,
                params={"projectId": p["id"]},
            )
        except PermissionError as e:
            print(f"{p['key']:<15} Skipped -- {e}")
            continue
        except RuntimeError as e:
            print(f"{p['key']:<15} Error -- {e}")
            continue

        values = data.get("values", [])
        if not values:
            print(f"{p['key']:<15} (no workflow scheme found)")
            continue

        scheme = values[0].get("workflowScheme", {})
        scheme_name = scheme.get("name", "(default scheme)")
        default_wf = scheme.get("defaultWorkflow", "jira")
        overrides = scheme.get("issueTypeMappings", {}) or {}
        print(f"{p['key']:<15} {scheme_name:<35} {default_wf:<30} {len(overrides)} issue-type override(s)")


# --------------------------------------------------------------------------
# 2. Statuses in use vs orphaned
# --------------------------------------------------------------------------

def run_statuses_section(base_url, auth):
    section_header("2. STATUSES: IN USE vs ORPHANED")

    try:
        all_statuses = api_call("GET", base_url, "/rest/api/3/status", auth)
    except (PermissionError, RuntimeError) as e:
        print(f"Could not fetch global status list -- {e}")
        return

    try:
        workflows = paginate(
            base_url, "/rest/api/3/workflow/search", auth,
            params={"expand": "statuses"},
        )
    except (PermissionError, RuntimeError) as e:
        print(f"Could not fetch workflows to check status usage -- {e}")
        return

    used_status_ids = set()
    for wf in workflows:
        for s in wf.get("statuses", []) or []:
            sid = s.get("id")
            if sid:
                used_status_ids.add(sid)

    in_use = [s for s in all_statuses if s.get("id") in used_status_ids]
    orphaned = [s for s in all_statuses if s.get("id") not in used_status_ids]

    print(f"Total statuses defined: {len(all_statuses)}")
    print(f"Referenced by at least one workflow: {len(in_use)}")
    print(f"Orphaned (not referenced by any workflow): {len(orphaned)}\n")

    if orphaned:
        print(f"{'ORPHANED STATUS':<35} {'CATEGORY':<15} {'SCOPE'}")
        print("-" * 70)
        for s in sorted(orphaned, key=lambda x: x.get("name", "").lower()):
            category = (s.get("statusCategory") or {}).get("name", "")
            scope = (s.get("scope") or {}).get("type", "GLOBAL")
            print(f"{s.get('name', ''):<35} {category:<15} {scope}")
    else:
        print("No orphaned statuses found.")


# --------------------------------------------------------------------------
# 3. Custom fields inventory
# --------------------------------------------------------------------------

def run_fields_section(base_url, auth, projects_by_id):
    section_header("3. CUSTOM FIELDS INVENTORY")

    try:
        all_fields = api_call("GET", base_url, "/rest/api/3/field", auth)
    except (PermissionError, RuntimeError) as e:
        print(f"Could not fetch fields -- {e}")
        return

    custom_fields = [f for f in all_fields if f.get("custom")]
    print(f"Total custom fields: {len(custom_fields)}\n")
    print(f"{'NAME':<35} {'TYPE':<25} {'SCOPE'}")
    print("-" * 100)

    for f in sorted(custom_fields, key=lambda x: x.get("name", "").lower()):
        field_id = f.get("id")
        name = f.get("name", "Unknown")
        schema_type = (f.get("schema") or {}).get("type", "unknown")

        try:
            contexts = paginate(base_url, f"/rest/api/3/field/{field_id}/context", auth)
        except PermissionError:
            print(f"{name:<35} {schema_type:<25} (permission denied for context lookup)")
            continue
        except RuntimeError as e:
            print(f"{name:<35} {schema_type:<25} (error: {e})")
            continue

        if not contexts:
            print(f"{name:<35} {schema_type:<25} No context configured")
            continue

        scope_parts = []
        for ctx in contexts:
            if ctx.get("isGlobalContext"):
                scope_parts.append("All projects")
                continue
            ctx_id = ctx.get("id")
            try:
                mappings = paginate(
                    base_url,
                    f"/rest/api/3/field/{field_id}/context/{ctx_id}/projectmapping",
                    auth,
                )
            except (PermissionError, RuntimeError):
                scope_parts.append(f"(context {ctx_id}: lookup failed)")
                continue
            keys = [
                projects_by_id.get(m.get("projectId"), m.get("projectId"))
                for m in mappings
            ]
            if keys:
                scope_parts.append(", ".join(keys))

        scope_str = "; ".join(scope_parts) if scope_parts else "Unscoped"
        print(f"{name:<35} {schema_type:<25} {scope_str}")


# --------------------------------------------------------------------------
# 4. Screens & field configurations
# --------------------------------------------------------------------------

def run_screens_section(base_url, auth, projects):
    section_header("4. SCREENS")

    try:
        screens = paginate(base_url, "/rest/api/3/screens", auth)
    except (PermissionError, RuntimeError) as e:
        print(f"Could not fetch screens -- {e}")
        screens = None

    if screens is not None:
        print(f"Total screens defined: {len(screens)}\n")
        print(f"{'ID':<10} {'NAME'}")
        print("-" * 60)
        for s in sorted(screens, key=lambda x: x.get("name", "").lower()):
            print(f"{s.get('id', ''):<10} {s.get('name', '')}")

    section_header("4b. FIELD CONFIGURATIONS")

    try:
        field_configs = paginate(base_url, "/rest/api/3/fieldconfiguration", auth)
    except (PermissionError, RuntimeError) as e:
        print(f"Could not fetch field configurations -- {e}")
        field_configs = None

    if field_configs is not None:
        print(f"Total field configurations defined: {len(field_configs)}\n")
        print(f"{'ID':<10} {'NAME':<35} {'DEFAULT'}")
        print("-" * 70)
        for fc in sorted(field_configs, key=lambda x: x.get("name", "").lower()):
            print(f"{fc.get('id', ''):<10} {fc.get('name', ''):<35} {fc.get('isDefault', False)}")

    section_header("4c. FIELD CONFIGURATION SCHEME PER PROJECT")

    print(f"{'PROJECT':<15} {'FIELD CONFIG SCHEME'}")
    print("-" * 60)
    for p in projects:
        try:
            data = api_call(
                "GET", base_url, "/rest/api/3/fieldconfigurationscheme/project", auth,
                params={"projectId": p["id"]},
            )
        except PermissionError as e:
            print(f"{p['key']:<15} Skipped -- {e}")
            continue
        except RuntimeError as e:
            print(f"{p['key']:<15} Error -- {e}")
            continue

        values = data.get("values", [])
        if not values or not values[0].get("fieldConfigurationScheme"):
            print(f"{p['key']:<15} (default / system scheme)")
            continue
        scheme_name = values[0]["fieldConfigurationScheme"].get("name", "Unknown")
        print(f"{p['key']:<15} {scheme_name}")


# --------------------------------------------------------------------------
# 5. Priorities & resolutions in use
# --------------------------------------------------------------------------

def get_issue_count(base_url, auth, jql):
    data = api_call(
        "POST", base_url, "/rest/api/3/search/approximate-count", auth,
        json_body={"jql": jql},
    )
    return data.get("count", 0)


def run_priorities_section(base_url, auth, scope_clause=""):
    section_header("5. PRIORITIES IN USE")

    try:
        priorities = api_call("GET", base_url, "/rest/api/3/priority", auth)
    except (PermissionError, RuntimeError) as e:
        print(f"Could not fetch priorities -- {e}")
        priorities = []

    rows = []
    for pr in priorities:
        name = pr.get("name", "Unknown")
        jql = f'priority = "{name}"'
        if scope_clause:
            jql = f"({scope_clause}) AND {jql}"
        try:
            count = get_issue_count(base_url, auth, jql)
        except (PermissionError, RuntimeError) as e:
            print(f"  Warning: could not count '{name}' -- {e}", file=sys.stderr)
            count = None
        rows.append({"name": name, "count": count})

    print(f"{'PRIORITY':<25} {'ISSUE COUNT':<15} {'STATUS'}")
    print("-" * 60)
    for r in sorted(rows, key=lambda x: (x["count"] is None, -(x["count"] or 0))):
        status = "UNUSED" if r["count"] == 0 else ("" if r["count"] else "unknown")
        print(f"{r['name']:<25} {str(r['count']):<15} {status}")

    section_header("5b. RESOLUTIONS IN USE")

    try:
        resolutions = api_call("GET", base_url, "/rest/api/3/resolution", auth)
    except (PermissionError, RuntimeError) as e:
        print(f"Could not fetch resolutions -- {e}")
        resolutions = []

    rows = []
    for res in resolutions:
        name = res.get("name", "Unknown")
        jql = f'resolution = "{name}"'
        if scope_clause:
            jql = f"({scope_clause}) AND {jql}"
        try:
            count = get_issue_count(base_url, auth, jql)
        except (PermissionError, RuntimeError) as e:
            print(f"  Warning: could not count '{name}' -- {e}", file=sys.stderr)
            count = None
        rows.append({"name": name, "count": count})

    unresolved_jql = "resolution is EMPTY"
    if scope_clause:
        unresolved_jql = f"({scope_clause}) AND {unresolved_jql}"
    try:
        unresolved_count = get_issue_count(base_url, auth, unresolved_jql)
    except (PermissionError, RuntimeError):
        unresolved_count = None
    rows.append({"name": "(Unresolved)", "count": unresolved_count})

    print(f"{'RESOLUTION':<25} {'ISSUE COUNT':<15} {'STATUS'}")
    print("-" * 60)
    for r in sorted(rows, key=lambda x: (x["count"] is None, -(x["count"] or 0))):
        status = "UNUSED" if r["count"] == 0 else ""
        print(f"{r['name']:<25} {str(r['count']):<15} {status}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Jira instance research/audit script.")
    parser.add_argument(
        "--sections",
        default=",".join(ALL_SECTIONS),
        help=f"Comma-separated list of sections to run. Options: {', '.join(ALL_SECTIONS)}. "
        "Default: all.",
    )
    parser.add_argument("--project", help="Limit per-project sections to a single project key.")
    parser.add_argument(
        "--jql",
        help="Extra JQL restriction applied to the priorities/resolutions counts, "
        "e.g. \"created >= -365d\"",
    )
    args = parser.parse_args()

    if not JIRA_BASE_URL or not JIRA_EMAIL or not JIRA_API_TOKEN:
        print(
            "Please set JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN "
            "(as environment variables, e.g. in a .env file) before running.",
            file=sys.stderr,
        )
        sys.exit(1)

    requested = [s.strip() for s in args.sections.split(",") if s.strip()]
    invalid = [s for s in requested if s not in ALL_SECTIONS]
    if invalid:
        print(f"Unknown section(s): {invalid}. Valid options: {ALL_SECTIONS}", file=sys.stderr)
        sys.exit(1)

    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)

    projects = get_projects(JIRA_BASE_URL, auth, project_filter=args.project)
    if not projects:
        print("No matching project(s) found.", file=sys.stderr)
        sys.exit(1)
    projects_by_id = {p["id"]: p["key"] for p in projects}

    scope_clause = args.jql or (f"project = {args.project}" if args.project else "")

    if "workflows" in requested:
        run_workflows_section(JIRA_BASE_URL, auth, projects)

    if "statuses" in requested:
        run_statuses_section(JIRA_BASE_URL, auth)

    if "fields" in requested:
        run_fields_section(JIRA_BASE_URL, auth, projects_by_id)

    if "screens" in requested:
        run_screens_section(JIRA_BASE_URL, auth, projects)

    if "priorities" in requested:
        run_priorities_section(JIRA_BASE_URL, auth, scope_clause)

    print("\nDone.")


if __name__ == "__main__":
    main()