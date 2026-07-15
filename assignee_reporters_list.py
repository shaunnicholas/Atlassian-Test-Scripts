"""
List all distinct reporters and assignees found across issues in a
Jira Cloud instance.

Note: Jira has no single endpoint that returns "all reporters/assignees" ---
these are just users referenced on issues. This script pages through every
issue (optionally scoped to a project or JQL query), pulls the `reporter`
and `assignee` fields, and prints the de-duplicated set of people along
with how many issues each one reported / is assigned to.

Requirements:
    pip install requests python-dotenv

Setup:
    1. Generate a Jira API token: https://id.atlassian.com/manage-profile/security/api-tokens
    2. Set JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN as environment
       variables (e.g. in a .env file), or fill them in below.

Usage:
    python get_jira_people.py
    python get_jira_people.py --project MYPROJ
    python get_jira_people.py --jql "project = MYPROJ AND created >= -90d"
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


def get_people(base_url: str, email: str, api_token: str, jql: str = ""):
    """
    Page through issues matching `jql` (default: all issues) and collect
    distinct reporters and assignees.

    Returns (reporters, assignees) where each is a dict keyed by accountId:
        { accountId: {"displayName": ..., "emailAddress": ..., "count": N} }
    """
    # Jira deprecated /rest/api/3/search (removed) in favor of
    # /rest/api/3/search/jql, which uses cursor-based pagination
    # (nextPageToken) instead of startAt/total.
    # See: https://developer.atlassian.com/changelog/#CHANGE-2046
    url = f"{base_url.rstrip('/')}/rest/api/3/search/jql"
    auth = HTTPBasicAuth(email, api_token)
    headers = {"Accept": "application/json"}

    reporters = {}
    assignees = {}

    max_results = 100
    next_page_token = None
    issue_count = 0

    while True:
        params = {
            "jql": jql,
            "maxResults": max_results,
            "fields": "reporter,assignee",
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        try:
            response = requests.get(
                url, headers=headers, auth=auth, params=params, timeout=30
            )
        except requests.exceptions.Timeout:
            raise RuntimeError(
                "Request timed out after 30s. Your Jira instance may be slow "
                "to respond, or the query is too broad. Try narrowing --jql "
                "or --project, or re-run (transient network issues happen)."
            )
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"Connection error while contacting Jira: {e}")

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch issues: {response.status_code} - {response.text}"
            )

        data = response.json()
        issues = data.get("issues", [])
        issue_count += len(issues)
        print(f"  ...fetched {issue_count} issue(s) so far", file=sys.stderr)

        for issue in issues:
            fields = issue.get("fields", {})

            reporter = fields.get("reporter")
            if reporter:
                _record_person(reporters, reporter)

            assignee = fields.get("assignee")
            if assignee:
                _record_person(assignees, assignee)

        # The new endpoint signals the end via isLast / absence of a token,
        # or simply by returning fewer issues than maxResults / none at all.
        next_page_token = data.get("nextPageToken")
        if data.get("isLast", not next_page_token) or not issues:
            break

    return reporters, assignees, issue_count


def _record_person(bucket: dict, person: dict):
    account_id = person.get("accountId") or person.get("name") or person.get("displayName")
    if account_id is None:
        return
    if account_id not in bucket:
        bucket[account_id] = {
            "displayName": person.get("displayName", "Unknown"),
            "emailAddress": person.get("emailAddress", ""),
            "count": 0,
        }
    bucket[account_id]["count"] += 1


def print_people_table(title: str, people: dict):
    print(f"\n{title} ({len(people)} unique):\n")
    print(f"{'NAME':<30} {'EMAIL':<35} {'ISSUE COUNT'}")
    print("-" * 80)
    for info in sorted(people.values(), key=lambda x: x["displayName"].lower()):
        print(f"{info['displayName']:<30} {info['emailAddress']:<35} {info['count']}")


def main():
    parser = argparse.ArgumentParser(description="List Jira reporters and assignees.")
    parser.add_argument("--project", help="Limit to a single project key, e.g. MYPROJ")
    parser.add_argument(
        "--jql",
        help="Custom JQL to scope the issue search (overrides --project if both given)",
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
        jql = args.jql
    elif args.project:
        jql = f"project = {args.project}"
    else:
        # The new /rest/api/3/search/jql endpoint rejects "unbounded" queries
        # (i.e. no restriction at all) with a 400 error. To scan every issue
        # in the instance without specifying a project, we fall back to a
        # wide-but-bounded date restriction. Adjust the window below if you
        # have issues older than this, or pass --project / --jql instead.
        jql = "created >= -3650d ORDER BY created DESC"  # last ~10 years
        print(
            "No --project or --jql given. Defaulting to a bounded query "
            f"covering the last 10 years: \"{jql}\"\n"
            "Use --project or --jql to scope this differently.\n",
            file=sys.stderr,
        )

    try:
        reporters, assignees, issue_count = get_people(
            JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, jql
        )
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    print_people_table("REPORTERS", reporters)
    print_people_table("ASSIGNEES", assignees)

    print(f"\nIssues scanned: {issue_count}")
    print(f"Total unique reporters: {len(reporters)}")
    print(f"Total unique assignees: {len(assignees)}")


if __name__ == "__main__":
    main()