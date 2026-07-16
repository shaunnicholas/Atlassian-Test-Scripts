"""
Show the number of issues mapped to each issue type in a Jira Cloud
instance (e.g. Bug: 120, Story: 340, Task: 87, Sub-task: 56 ...).

Approach:
    1. Fetch the list of issue types available on the instance via
       GET /rest/api/3/issuetype
    2. For each issue type, get a count of matching issues via the
       lightweight POST /rest/api/3/search/approximate-count endpoint
       (much faster than paging through every issue just to count them).

Requirements:
    pip install requests python-dotenv

Setup:
    1. Generate a Jira API token: https://id.atlassian.com/manage-profile/security/api-tokens
    2. Set JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN as environment
       variables (e.g. in a .env file).

Usage:
    python issuetypecount.py
    python issuetypecount.py --project MYPROJ
    python issuetypecount.py --jql "created >= -365d"
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


def get_issue_types(base_url: str, auth: HTTPBasicAuth):
    """Fetch all issue types visible to this account."""
    url = f"{base_url.rstrip('/')}/rest/api/3/issuetype"
    headers = {"Accept": "application/json"}

    try:
        response = requests.get(url, headers=headers, auth=auth, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise RuntimeError("Request timed out while fetching issue types.")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"Connection error while fetching issue types: {e}")

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch issue types: {response.status_code} - {response.text}"
        )

    return response.json()


def get_issue_count(base_url: str, auth: HTTPBasicAuth, jql: str) -> int:
    """Get an approximate count of issues matching a JQL query."""
    url = f"{base_url.rstrip('/')}/rest/api/3/search/approximate-count"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    body = {"jql": jql}

    try:
        response = requests.post(
            url, headers=headers, auth=auth, json=body, timeout=REQUEST_TIMEOUT
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Request timed out while counting issues for JQL: {jql}")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"Connection error while counting issues: {e}")

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to get count for JQL '{jql}': "
            f"{response.status_code} - {response.text}"
        )

    return response.json().get("count", 0)


def build_scope_clause(base_jql: str) -> str:
    """Wrap a base scope restriction (project/date/etc.) for combining with issuetype."""
    return f"({base_jql})" if base_jql else ""


def main():
    parser = argparse.ArgumentParser(
        description="Count Jira issues grouped by issue type."
    )
    parser.add_argument("--project", help="Limit to a single project key, e.g. MYPROJ")
    parser.add_argument(
        "--jql",
        help="Custom JQL restriction to scope the counts (combined with each issue type), "
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

    if args.jql:
        scope = args.jql
    elif args.project:
        scope = f"project = {args.project}"
    else:
        scope = ""  # no extra restriction beyond issuetype itself

    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)

    try:
        issue_types = get_issue_types(JIRA_BASE_URL, auth)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    if not issue_types:
        print("No issue types found.")
        return

    scope_clause = build_scope_clause(scope)
    results = []

    # Team-managed ("next-gen") projects create their own copy of each issue
    # type (same name, different id) — e.g. three separate "Task" issue
    # types across three projects. JQL's `issuetype = "X"` matches by name,
    # not id, so querying each duplicate would just repeat the same count.
    # Dedupe by name first.
    seen_names = set()
    unique_issue_types = []
    for it in issue_types:
        name = it.get("name", "Unknown")
        if name in seen_names:
            continue
        seen_names.add(name)
        unique_issue_types.append(it)

    skipped = len(issue_types) - len(unique_issue_types)
    if skipped:
        print(
            f"Note: {skipped} duplicate issue type id(s) with repeated names "
            f"were collapsed (common with team-managed projects).\n",
            file=sys.stderr,
        )

    print(f"Counting issues across {len(unique_issue_types)} unique issue type(s)...\n", file=sys.stderr)

    for it in unique_issue_types:
        name = it.get("name", "Unknown")
        is_subtask = it.get("subtask", False)

        # Escape double quotes in the issue type name, just in case.
        safe_name = name.replace('"', '\\"')
        type_clause = f'issuetype = "{safe_name}"'
        jql = f"{scope_clause} AND {type_clause}" if scope_clause else type_clause

        try:
            count = get_issue_count(JIRA_BASE_URL, auth, jql)
        except RuntimeError as e:
            print(f"Warning: skipping '{name}' -- {e}", file=sys.stderr)
            continue

        results.append({"name": name, "subtask": is_subtask, "count": count})
        print(f"  ...{name}: {count}", file=sys.stderr)

    if not results:
        print("No counts could be retrieved.")
        return

    results.sort(key=lambda x: x["count"], reverse=True)
    total = sum(r["count"] for r in results)

    print(f"\n{'ISSUE TYPE':<30} {'SUBTASK':<10} {'COUNT'}")
    print("-" * 55)
    for r in results:
        print(f"{r['name']:<30} {str(r['subtask']):<10} {r['count']}")
    print("-" * 55)
    print(f"Total issues across all types: {total}")


if __name__ == "__main__":
    main()