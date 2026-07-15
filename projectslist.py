"""
Fetch all Jira projects (aka "spaces") with their project keys
from a Jira Cloud instance using the REST API.

Requirements:
    pip install requests

Setup:
    1. Generate a Jira API token: https://id.atlassian.com/manage-profile/security/api-tokens
    2. Fill in JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN below,
       or set them as environment variables (recommended).

Usage:
    python get_jira_projects.py
"""

import os
import sys
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

# --- Configuration -----------------------------------------------------
# Prefer environment variables over hardcoding credentials.
# JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://your-domain.atlassian.net")
# JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "your-email@example.com")
# JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "your-api-token")
# ------------------------------------------------------------------------
load_dotenv()

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")


def get_all_projects(base_url: str, email: str, api_token: str):
    """
    Fetch all projects from a Jira Cloud instance using the
    /rest/api/3/project/search endpoint (paginated).

    Returns a list of dicts: [{"key": ..., "name": ..., "id": ...}, ...]
    """
    url = f"{base_url.rstrip('/')}/rest/api/3/project/search"
    auth = HTTPBasicAuth(email, api_token)
    headers = {"Accept": "application/json"}

    projects = []
    start_at = 0
    max_results = 50
    total_from_api = None  # Jira also reports a "total" field per page

    while True:
        params = {"startAt": start_at, "maxResults": max_results}
        response = requests.get(url, headers=headers, auth=auth, params=params)

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch projects: {response.status_code} - {response.text}"
            )

        data = response.json()

        # Jira reports the overall total count on each page of results.
        if total_from_api is None:
            total_from_api = data.get("total")

        for project in data.get("values", []):
            projects.append(
                {
                    "id": project.get("id"),
                    "key": project.get("key"),
                    "name": project.get("name"),
                    "projectTypeKey": project.get("projectTypeKey"),
                }
            )

        if data.get("isLast", True):
            break
        start_at += max_results

    return projects, total_from_api


def main():
    if "your-domain" in JIRA_BASE_URL or "your-api-token" in JIRA_API_TOKEN:
        print(
            "Please set JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN "
            "(as environment variables or directly in the script) before running.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        projects, total_from_api = get_all_projects(
            JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN
        )
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    total_count = len(projects)

    if not projects:
        print("No projects found. Total number of projects: 0")
        return

    print(f"Found {total_count} project(s):\n")
    print(f"{'KEY':<12} {'NAME':<40} {'TYPE'}")
    print("-" * 70)
    for p in sorted(projects, key=lambda x: x["key"] or ""):
        print(f"{p['key']:<12} {p['name']:<40} {p.get('projectTypeKey', '')}")

    print("-" * 70)
    print(f"Total number of projects: {total_count}")

    # Sanity check in case Jira's reported total ever disagrees with the
    # number of items actually returned (e.g. permission filtering).
    if total_from_api is not None and total_from_api != total_count:
        print(
            f"Note: Jira API reported a total of {total_from_api}, "
            f"but {total_count} project(s) were retrieved. This can happen "
            f"if you don't have access to view some projects.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()