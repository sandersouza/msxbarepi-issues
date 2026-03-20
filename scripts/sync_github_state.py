#!/usr/bin/env python3
"""Synchronize GitHub issues, milestones, labels and project fields.

This script mirrors the repository state from a source repository/project to a
destination repository/project using the GitHub CLI (`gh`).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any


ISSUE_MARKER_PREFIX = "msx-sync-source-issue"
MILESTONE_MARKER_PREFIX = "msx-sync-source-milestone"
RETRYABLE_PATTERNS = (
    "secondary rate limit",
    "temporarily blocked from content creation",
)


class SyncError(RuntimeError):
    """Raised when an external GitHub command fails."""


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Mirror issues, milestones, labels and project single-select fields "
            "between two GitHub repositories/projects."
        )
    )
    parser.add_argument("--source-owner", default="sandersouza")
    parser.add_argument("--source-repo", default="msxbarepi")
    parser.add_argument("--target-owner", default="sandersouza")
    parser.add_argument("--target-repo", default="msxbarepi-issues")
    parser.add_argument("--source-project", type=int, default=3)
    parser.add_argument("--target-project", type=int, default=5)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the operations that would be executed.",
    )
    parser.add_argument(
        "--skip-labels",
        action="store_true",
        help="Do not synchronize repository labels.",
    )
    parser.add_argument(
        "--skip-project",
        action="store_true",
        help="Do not synchronize GitHub Project items and single-select fields.",
    )
    return parser.parse_args()


def run_command(
    cmd: list[str],
    *,
    input_text: str | None = None,
    expect_json: bool = False,
    dry_run: bool = False,
    mutating: bool = False,
) -> Any:
    if dry_run and mutating:
        print(f"[dry-run] {' '.join(cmd)}")
        if input_text:
            print(f"[dry-run] payload: {input_text}")
        if expect_json:
            return None
        return ""

    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SyncError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    output = proc.stdout.strip()
    if expect_json:
        if not output:
            return None
        return json.loads(output)
    return output


def is_retryable_error(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in RETRYABLE_PATTERNS)


def gh_api(
    endpoint: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    paginate: bool = False,
    dry_run: bool = False,
) -> Any:
    cmd = ["gh", "api", "-H", "Accept: application/vnd.github+json"]
    if paginate:
        cmd.extend(["--paginate", "--slurp"])
    if method != "GET":
        cmd.extend(["-X", method])
    if data is not None:
        cmd.extend(["--input", "-"])
    cmd.append(endpoint)
    payload = json.dumps(data) if data is not None else None
    runner = lambda: run_command(
        cmd,
        input_text=payload,
        expect_json=True,
        dry_run=dry_run,
        mutating=(method != "GET"),
    )
    if method != "GET":
        response = run_with_retries(
            runner,
            description=f"gh api {method} {endpoint}",
            dry_run=dry_run,
        )
    else:
        response = runner()
    if paginate and response is not None:
        flattened: list[Any] = []
        for page in response:
            if isinstance(page, list):
                flattened.extend(page)
            else:
                flattened.append(page)
        return flattened
    return response


def gh_json(cmd: list[str], *, dry_run: bool = False) -> Any:
    return run_command(cmd, expect_json=True, dry_run=dry_run)


def run_with_retries(
    fn,
    *,
    description: str,
    dry_run: bool,
    max_attempts: int = 6,
) -> Any:
    delay_seconds = 15
    attempt = 1
    while True:
        try:
            return fn()
        except SyncError as exc:
            if dry_run or attempt >= max_attempts or not is_retryable_error(str(exc)):
                raise
            print(
                f"{description} hit GitHub secondary rate limit; retrying in {delay_seconds}s "
                f"(attempt {attempt}/{max_attempts})"
            )
            time.sleep(delay_seconds)
            attempt += 1
            delay_seconds = min(delay_seconds * 2, 300)


def normalize_text(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").strip()


def marker(pattern_prefix: str, repo: RepoRef, number: int) -> str:
    return f"<!-- {pattern_prefix}:{repo.slug}#{number} -->"


def extract_marker(pattern_prefix: str, text: str | None) -> int | None:
    body = text or ""
    patterns = [
        rf"<!--\s*{re.escape(pattern_prefix)}:[^#]+#(\d+)\s*-->",
        r"Mirror-Source-(?:Issue|Milestone):\s+[^#\s]+#(\d+)",
        r"Source:\s+https://github\.com/[^/]+/[^/]+/(?:issues|milestone)/(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def milestone_body(source_repo: RepoRef, milestone: dict[str, Any]) -> str:
    source_number = milestone["number"]
    source_url = f"https://github.com/{source_repo.slug}/milestone/{source_number}"
    description = normalize_text(milestone.get("description"))
    parts = [
        f"> Mirror of `{source_repo.slug}` milestone #{source_number}",
        f"> Source: {source_url}",
        f"Mirror-Source-Milestone: {source_repo.slug}#{source_number}",
    ]
    if description:
        parts.append("")
        parts.append(description)
    parts.append("")
    parts.append(marker(MILESTONE_MARKER_PREFIX, source_repo, source_number))
    return "\n".join(parts).strip() + "\n"


def compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def issue_body(source_repo: RepoRef, issue: dict[str, Any]) -> str:
    source_number = issue["number"]
    source_url = issue["html_url"]
    body = normalize_text(issue.get("body"))
    parts = [
        f"> Mirror of `{source_repo.slug}` issue #{source_number}",
        f"> Source: {source_url}",
        f"Mirror-Source-Issue: {source_repo.slug}#{source_number}",
    ]
    if body:
        parts.append("")
        parts.append(body)
    parts.append("")
    parts.append(marker(ISSUE_MARKER_PREFIX, source_repo, source_number))
    return "\n".join(parts).strip() + "\n"


def label_map(labels: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {label["name"]: label for label in labels}


def issue_assignees(issue: dict[str, Any]) -> list[str]:
    return [assignee["login"] for assignee in issue.get("assignees", [])]


def project_field_value(item: dict[str, Any], field_name: str) -> Any:
    candidates = [
        field_name,
        field_name.lower(),
        field_name.replace(" ", "_"),
        field_name.lower().replace(" ", "_"),
        field_name.lower().replace(" ", "-"),
    ]
    for key in candidates:
        if key in item:
            return item[key]
    return None


def fetch_repo_labels(repo: RepoRef, *, dry_run: bool = False) -> list[dict[str, Any]]:
    return gh_api(
        f"repos/{repo.slug}/labels?per_page=100",
        paginate=True,
        dry_run=dry_run,
    ) or []


def sync_labels(source_repo: RepoRef, target_repo: RepoRef, *, dry_run: bool = False) -> None:
    source_labels = fetch_repo_labels(source_repo, dry_run=dry_run)
    target_labels = label_map(fetch_repo_labels(target_repo, dry_run=dry_run))

    for source_label in source_labels:
        existing = target_labels.get(source_label["name"])
        payload = {
            "name": source_label["name"],
            "color": source_label["color"],
            "description": source_label.get("description") or "",
        }
        if existing is None:
            print(f"Creating label {source_label['name']}")
            gh_api(
                f"repos/{target_repo.slug}/labels",
                method="POST",
                data=payload,
                dry_run=dry_run,
            )
            continue

        needs_update = (
            existing.get("color") != payload["color"]
            or normalize_text(existing.get("description")) != payload["description"]
        )
        if not needs_update:
            continue

        print(f"Updating label {source_label['name']}")
        encoded_name = urllib.parse.quote(source_label["name"], safe="")
        gh_api(
            f"repos/{target_repo.slug}/labels/{encoded_name}",
            method="PATCH",
            data=payload,
            dry_run=dry_run,
        )


def fetch_milestones(repo: RepoRef, *, dry_run: bool = False) -> list[dict[str, Any]]:
    return gh_api(
        f"repos/{repo.slug}/milestones?state=all&per_page=100",
        paginate=True,
        dry_run=dry_run,
    ) or []


def sync_milestones(
    source_repo: RepoRef,
    target_repo: RepoRef,
    *,
    dry_run: bool = False,
) -> tuple[dict[int, int], dict[int, str]]:
    source_milestones = sorted(fetch_milestones(source_repo, dry_run=dry_run), key=lambda m: m["number"])
    target_milestones = fetch_milestones(target_repo, dry_run=dry_run)

    target_by_source_number: dict[int, dict[str, Any]] = {}
    target_by_title = {milestone["title"]: milestone for milestone in target_milestones}
    for milestone in target_milestones:
        source_number = extract_marker(MILESTONE_MARKER_PREFIX, milestone.get("description"))
        if source_number is not None:
            target_by_source_number[source_number] = milestone

    milestone_number_map: dict[int, int] = {}
    desired_states: dict[int, str] = {}

    for source_milestone in source_milestones:
        desired_description = milestone_body(source_repo, source_milestone)
        desired_due_on = source_milestone.get("due_on")
        existing = target_by_source_number.get(source_milestone["number"]) or target_by_title.get(
            source_milestone["title"]
        )

        payload = {
            "title": source_milestone["title"],
            "description": desired_description,
            "due_on": desired_due_on,
            "state": "open",
        }
        payload = compact_payload(payload)

        if existing is None:
            print(f"Creating milestone {source_milestone['title']}")
            created = gh_api(
                f"repos/{target_repo.slug}/milestones",
                method="POST",
                data=payload,
                dry_run=dry_run,
            ) or {"number": source_milestone["number"]}
            target_number = created["number"]
        else:
            current_description = normalize_text(existing.get("description"))
            needs_update = (
                existing.get("title") != payload["title"]
                or current_description != normalize_text(payload["description"])
                or existing.get("due_on") != payload.get("due_on")
                or existing.get("state") != payload["state"]
            )
            if needs_update:
                print(f"Updating milestone {source_milestone['title']}")
                gh_api(
                    f"repos/{target_repo.slug}/milestones/{existing['number']}",
                    method="PATCH",
                    data=payload,
                    dry_run=dry_run,
                )
            target_number = existing["number"]

        milestone_number_map[source_milestone["number"]] = target_number
        desired_states[target_number] = source_milestone["state"]

    return milestone_number_map, desired_states


def finalize_milestone_states(
    target_repo: RepoRef,
    desired_states: dict[int, str],
    *,
    dry_run: bool = False,
) -> None:
    current_target_milestones = {m["number"]: m for m in fetch_milestones(target_repo, dry_run=dry_run)}
    for number, source_state in desired_states.items():
        desired_state = "closed" if source_state == "closed" else "open"
        current_state = current_target_milestones.get(number, {}).get("state")
        if current_state is None and dry_run and desired_state == "open":
            continue
        if current_state == desired_state:
            continue
        print(f"Setting milestone #{number} to {desired_state}")
        gh_api(
            f"repos/{target_repo.slug}/milestones/{number}",
            method="PATCH",
            data={"state": desired_state},
            dry_run=dry_run,
        )


def fetch_issues(repo: RepoRef, *, dry_run: bool = False) -> list[dict[str, Any]]:
    issues = gh_api(
        f"repos/{repo.slug}/issues?state=all&per_page=100&sort=created&direction=asc",
        paginate=True,
        dry_run=dry_run,
    ) or []
    return [issue for issue in issues if "pull_request" not in issue]


def sync_issues(
    source_repo: RepoRef,
    target_repo: RepoRef,
    milestone_map: dict[int, int],
    *,
    dry_run: bool = False,
) -> dict[int, int]:
    source_issues = fetch_issues(source_repo, dry_run=dry_run)
    target_issues = fetch_issues(target_repo, dry_run=dry_run)

    target_by_source_number: dict[int, dict[str, Any]] = {}
    for issue in target_issues:
        source_number = extract_marker(ISSUE_MARKER_PREFIX, issue.get("body"))
        if source_number is not None:
            target_by_source_number[source_number] = issue

    issue_number_map: dict[int, int] = {}

    for source_issue in source_issues:
        source_number = source_issue["number"]
        milestone = source_issue.get("milestone")
        milestone_number = None
        if milestone is not None:
            milestone_number = milestone_map.get(milestone["number"])

        desired_body = issue_body(source_repo, source_issue)
        desired_labels = [label["name"] for label in source_issue.get("labels", [])]
        desired_assignees = issue_assignees(source_issue)
        payload = {
            "title": source_issue["title"],
            "body": desired_body,
            "labels": desired_labels,
            "assignees": desired_assignees,
            "milestone": milestone_number,
        }
        payload = compact_payload(payload)
        existing = target_by_source_number.get(source_number)

        if existing is None:
            print(f"Creating issue #{source_number} -> {source_issue['title']}")
            created = gh_api(
                f"repos/{target_repo.slug}/issues",
                method="POST",
                data=payload,
                dry_run=dry_run,
            ) or {"number": source_number}
            target_number = created["number"]
            if source_issue["state"] == "closed":
                gh_api(
                    f"repos/{target_repo.slug}/issues/{target_number}",
                    method="PATCH",
                    data={"state": "closed"},
                    dry_run=dry_run,
                )
            issue_number_map[source_number] = target_number
            continue

        current_labels = sorted(label["name"] for label in existing.get("labels", []))
        current_assignees = sorted(issue_assignees(existing))
        current_milestone_number = existing["milestone"]["number"] if existing.get("milestone") else None
        desired_state = source_issue["state"]

        needs_update = (
            existing.get("title") != payload["title"]
            or normalize_text(existing.get("body")) != normalize_text(payload["body"])
            or current_labels != sorted(desired_labels)
            or current_assignees != sorted(desired_assignees)
            or current_milestone_number != payload.get("milestone")
            or existing.get("state") != desired_state
        )

        if needs_update:
            print(f"Updating mirrored issue for source #{source_number}")
            update_payload = dict(payload)
            update_payload["state"] = desired_state
            gh_api(
                f"repos/{target_repo.slug}/issues/{existing['number']}",
                method="PATCH",
                data=update_payload,
                dry_run=dry_run,
            )

        issue_number_map[source_number] = existing["number"]

    return issue_number_map


def fetch_project_fields(project_number: int, owner: str, *, dry_run: bool = False) -> dict[str, Any]:
    return gh_json(
        ["gh", "project", "field-list", str(project_number), "--owner", owner, "--format", "json"],
        dry_run=dry_run,
    ) or {"fields": []}


def fetch_project_items(project_number: int, owner: str, *, dry_run: bool = False) -> list[dict[str, Any]]:
    data = gh_json(
        [
            "gh",
            "project",
            "item-list",
            str(project_number),
            "--owner",
            owner,
            "--limit",
            "1000",
            "--format",
            "json",
        ],
        dry_run=dry_run,
    ) or {"items": []}
    return data.get("items", [])


def fetch_project_id(project_number: int, owner: str, *, dry_run: bool = False) -> str | None:
    data = gh_json(
        ["gh", "project", "view", str(project_number), "--owner", owner, "--format", "json"],
        dry_run=dry_run,
    )
    if data is None:
        return None
    return data["id"]


def sync_project(
    source_repo: RepoRef,
    target_repo: RepoRef,
    source_project_number: int,
    target_project_number: int,
    issue_number_map: dict[int, int],
    *,
    dry_run: bool = False,
) -> None:
    source_fields_data = fetch_project_fields(source_project_number, source_repo.owner, dry_run=dry_run)
    target_fields_data = fetch_project_fields(target_project_number, target_repo.owner, dry_run=dry_run)
    source_items = fetch_project_items(source_project_number, source_repo.owner, dry_run=dry_run)
    target_items = fetch_project_items(target_project_number, target_repo.owner, dry_run=dry_run)
    target_project_id = fetch_project_id(target_project_number, target_repo.owner, dry_run=dry_run)

    source_fields = {
        field["name"]: field
        for field in source_fields_data["fields"]
        if field["type"] == "ProjectV2SingleSelectField"
    }
    target_fields = {
        field["name"]: field
        for field in target_fields_data["fields"]
        if field["type"] == "ProjectV2SingleSelectField"
    }
    shared_field_names = sorted(set(source_fields) & set(target_fields))

    target_items_by_issue_number = {
        item["content"]["number"]: item
        for item in target_items
        if item.get("content", {}).get("type") == "Issue"
        and item.get("content", {}).get("repository") == target_repo.slug
    }

    for source_item in source_items:
        content = source_item.get("content", {})
        if content.get("type") != "Issue":
            continue
        if content.get("repository") != source_repo.slug:
            continue

        source_issue_number = content["number"]
        target_issue_number = issue_number_map.get(source_issue_number)
        if target_issue_number is None:
            continue

        target_item = target_items_by_issue_number.get(target_issue_number)
        if target_item is None:
            issue_url = f"https://github.com/{target_repo.slug}/issues/{target_issue_number}"
            print(f"Adding target issue #{target_issue_number} to project {target_project_number}")
            run_with_retries(
                lambda: run_command(
                    [
                        "gh",
                        "project",
                        "item-add",
                        str(target_project_number),
                        "--owner",
                        target_repo.owner,
                        "--url",
                        issue_url,
                    ],
                    dry_run=dry_run,
                    mutating=True,
                ),
                description=f"gh project item-add {target_issue_number}",
                dry_run=dry_run,
            )

    target_items = fetch_project_items(target_project_number, target_repo.owner, dry_run=dry_run)
    target_items_by_issue_number = {
        item["content"]["number"]: item
        for item in target_items
        if item.get("content", {}).get("type") == "Issue"
        and item.get("content", {}).get("repository") == target_repo.slug
    }

    for source_item in source_items:
        content = source_item.get("content", {})
        if content.get("type") != "Issue" or content.get("repository") != source_repo.slug:
            continue

        source_issue_number = content["number"]
        target_issue_number = issue_number_map.get(source_issue_number)
        target_item = target_items_by_issue_number.get(target_issue_number)
        if target_item is None or target_project_id is None:
            continue

        for field_name in shared_field_names:
            source_field = source_fields[field_name]
            target_field = target_fields[field_name]
            source_value = project_field_value(source_item, field_name)
            target_value = project_field_value(target_item, field_name)

            if isinstance(source_value, dict) and "name" in source_value:
                source_value = source_value["name"]
            if isinstance(target_value, dict) and "name" in target_value:
                target_value = target_value["name"]

            if source_value == target_value:
                continue

            target_option_map = {option["name"]: option["id"] for option in target_field["options"]}
            print(
                f"Updating project field {field_name!r} for source issue #{source_issue_number} "
                f"-> target issue #{target_issue_number}: {target_value!r} -> {source_value!r}"
            )

            if not source_value:
                run_with_retries(
                    lambda: run_command(
                        [
                            "gh",
                            "project",
                            "item-edit",
                            "--id",
                            target_item["id"],
                            "--project-id",
                            target_project_id,
                            "--field-id",
                            target_field["id"],
                            "--clear",
                        ],
                        dry_run=dry_run,
                        mutating=True,
                    ),
                    description=f"gh project item-edit clear {target_issue_number}",
                    dry_run=dry_run,
                )
                continue

            option_id = target_option_map.get(str(source_value))
            if option_id is None:
                print(
                    f"Skipping field {field_name!r} for issue #{source_issue_number}: "
                    f"target project does not have option {source_value!r}",
                    file=sys.stderr,
                )
                continue

            run_with_retries(
                lambda: run_command(
                    [
                        "gh",
                        "project",
                        "item-edit",
                        "--id",
                        target_item["id"],
                        "--project-id",
                        target_project_id,
                        "--field-id",
                        target_field["id"],
                        "--single-select-option-id",
                        option_id,
                    ],
                    dry_run=dry_run,
                    mutating=True,
                ),
                description=f"gh project item-edit {field_name} {target_issue_number}",
                dry_run=dry_run,
            )


def main() -> int:
    args = parse_args()
    source_repo = RepoRef(args.source_owner, args.source_repo)
    target_repo = RepoRef(args.target_owner, args.target_repo)

    print(f"Source repo: {source_repo.slug}")
    print(f"Target repo: {target_repo.slug}")

    if not args.skip_labels:
        sync_labels(source_repo, target_repo, dry_run=args.dry_run)

    milestone_map, desired_milestone_states = sync_milestones(
        source_repo,
        target_repo,
        dry_run=args.dry_run,
    )
    issue_map = sync_issues(
        source_repo,
        target_repo,
        milestone_map,
        dry_run=args.dry_run,
    )

    if not args.skip_project:
        sync_project(
            source_repo,
            target_repo,
            args.source_project,
            args.target_project,
            issue_map,
            dry_run=args.dry_run,
        )

    finalize_milestone_states(target_repo, desired_milestone_states, dry_run=args.dry_run)
    print("Synchronization complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
