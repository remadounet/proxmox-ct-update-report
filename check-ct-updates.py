#!/usr/bin/env python3
"""
Check package updates in Proxmox LXC containers and publish HTML reports.

The script uses only Python's standard library and tools already available on
the Proxmox host and inside the containers. It does not install packages.

Supported package managers: apt (and nala), dnf, apk (Alpine Linux).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

BEGIN_MARKER = "<!-- BEGIN AUTO UPDATE REPORT -->"
END_MARKER = "<!-- END AUTO UPDATE REPORT -->"
LEGACY_BEGIN_MARKER = "--- BEGIN AUTO UPDATE REPORT ---"
LEGACY_END_MARKER = "--- END AUTO UPDATE REPORT ---"
MAX_CT_DESCRIPTION_LENGTH = 8192
DEFAULT_LANGUAGE = "en"
AVAILABLE_LANGUAGES = ["en", "fr", "es", "de", "it", "pt", "pl", "ru", "uk"]
DEFAULT_MAX_PARALLEL = 4
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
LOCALE_DIRECTORY = SCRIPT_DIRECTORY / "locales"


def load_translations(language: str) -> dict[str, str]:
    """Load the requested locale, falling back to English for missing keys."""
    english_path = LOCALE_DIRECTORY / "en.json"
    selected_path = LOCALE_DIRECTORY / f"{language}.json"

    try:
        with english_path.open("r", encoding="utf-8") as file_handle:
            translations = json.load(file_handle)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot load English translation file: {error}") from error

    if language != "en":
        try:
            with selected_path.open("r", encoding="utf-8") as file_handle:
                selected = json.load(file_handle)
            translations.update(selected)
        except FileNotFoundError:
            print(f"WARNING: Translation '{language}' not found; using English.", file=sys.stderr)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Invalid translation file '{selected_path}': {error}") from error

    return translations


TRANSLATIONS: dict[str, str] = {}


def translate(key: str, **values: Any) -> str:
    """Return a translated message and interpolate its values."""
    template = TRANSLATIONS.get(key, key)
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return template


def run_command(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 300,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a host command and capture its output."""
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        stdin=subprocess.DEVNULL if input_text is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=check,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )


def run_container_command(container_id: int, command: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run a shell command inside an LXC container.

    A non-login shell is used on purpose: community-scripts and similar
    templates print a colored banner from /etc/profile.d on login shells,
    which would pollute the command's stdout.
    """
    return run_command(
        ["pct", "exec", str(container_id), "--", "bash", "-c", command],
        timeout=timeout,
    )


def escape_html(value: Any) -> str:
    """Escape text for safe HTML output."""
    return html.escape(str(value), quote=True)


ANSI_ESCAPE_PATTERN = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]|\x1b][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[P^_].*?\x1b\\",
    re.DOTALL,
)


def strip_ansi(value: Any) -> str:
    """Remove ANSI escape sequences (colors, cursor moves, OSC titles)."""
    cleaned = ANSI_ESCAPE_PATTERN.sub("", str(value))
    # Also drop emoji variation selectors and zero-width characters left
    # over from partially stripped colored banners.
    return re.sub(r"[\ufe00-\ufe0f\u200b-\u200f\u2060]", "", cleaned)


def parse_ct_selection(spec: str) -> set[int]:
    """Parse a CT selection such as '100,105-110' into a set of IDs."""
    selected: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if range_match:
            low, high = int(range_match.group(1)), int(range_match.group(2))
            if low > high:
                raise ValueError(f"Invalid CT range: {part}")
            selected.update(range(low, high + 1))
        elif part.isdigit():
            selected.add(int(part))
        else:
            raise ValueError(f"Invalid CT selector: {part}")
    return selected


def filter_container_ids(
    container_ids: list[int],
    only: str | None,
    exclude: str | None,
) -> list[int]:
    """Apply --only and --exclude selections to the container ID list."""
    selected_ids = set(container_ids)
    if only:
        wanted = parse_ct_selection(only)
        missing = wanted - selected_ids
        if missing:
            print(
                translate("warning_unknown_containers", ids=", ".join(str(i) for i in sorted(missing))),
                file=sys.stderr,
            )
        selected_ids &= wanted
    if exclude:
        selected_ids -= parse_ct_selection(exclude)
    return sorted(selected_ids)


def get_container_ids() -> list[int]:
    """Return all container IDs known to pct."""
    result = run_command(["pct", "list"], timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or translate("error_container_list"))

    container_ids: list[int] = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if fields and fields[0].isdigit():
            container_ids.append(int(fields[0]))
    return sorted(container_ids)


def get_container_status(container_id: int) -> str:
    """Return a container status such as running or stopped."""
    result = run_command(["pct", "status", str(container_id)], timeout=30)
    if result.returncode != 0:
        return "unknown"
    match = re.search(r"status:\s*(\S+)", result.stdout, re.IGNORECASE)
    return match.group(1).lower() if match else "unknown"


def get_distribution(container_id: int) -> dict[str, str]:
    """Read distribution metadata from os-release."""
    command = (
        "if [ -r /etc/os-release ]; then "
        ". /etc/os-release; "
        "printf '%s\\n' \"${ID:-unknown}\" \"${VERSION_ID:-unknown}\" "
        "\"${PRETTY_NAME:-unknown}\"; "
        "else exit 2; fi"
    )
    result = run_container_command(container_id, command, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or translate("error_os_release"))

    values = [strip_ansi(line) for line in result.stdout.strip().splitlines() if line.strip()]
    # Defensive: if a login banner still leaks into stdout, our three
    # values are the last lines printed by the command.
    values = values[-3:]
    while len(values) < 3:
        values.append("unknown")
    return {
        "id": values[0].strip().lower(),
        "version": values[1].strip(),
        "name": values[2].strip(),
    }


def detect_package_manager(container_id: int, distribution: dict[str, str]) -> str:
    """Detect a supported package manager in the container."""
    checks = [
        ("nala", "command -v nala >/dev/null 2>&1"),
        ("apt", "command -v apt-get >/dev/null 2>&1"),
        ("dnf", "command -v dnf >/dev/null 2>&1"),
        ("apk", "command -v apk >/dev/null 2>&1"),
    ]
    for manager, check_command in checks:
        result = run_container_command(container_id, check_command, timeout=20)
        if result.returncode == 0:
            if manager == "nala":
                return "apt"
            return manager

    distribution_id = distribution.get("id", "")
    if distribution_id in {"debian", "ubuntu", "linuxmint", "pop"}:
        return "apt"
    if distribution_id in {"fedora", "rhel", "centos", "rocky", "almalinux"}:
        return "dnf"
    if distribution_id in {"alpine"}:
        return "apk"
    return "unknown"


def parse_apt_updates(output: str) -> list[dict[str, str]]:
    """Parse apt list --upgradable output."""
    packages: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("listing"):
            continue
        match = re.match(r"^([^/\s]+)/\S+\s+(\S+)", line)
        if match:
            packages.append({"package": match.group(1), "candidate": match.group(2)})
    return packages


def get_apt_updates(container_id: int, skip_refresh: bool = False) -> list[dict[str, str]]:
    """Refresh APT metadata and retrieve available package updates."""
    if not skip_refresh:
        nala_check = run_container_command(container_id, "command -v nala >/dev/null 2>&1", timeout=20)
        if nala_check.returncode == 0:
            refresh = run_container_command(container_id, "nala update", timeout=900)
        else:
            refresh = run_container_command(container_id, "apt-get update -qq", timeout=900)

        if refresh.returncode != 0:
            raise RuntimeError(refresh.stderr.strip() or translate("error_package_refresh"))

    result = run_container_command(container_id, "apt list --upgradable 2>/dev/null", timeout=180)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or translate("error_update_list"))
    return parse_apt_updates(result.stdout)


def parse_dnf_query_output(output: str) -> list[dict[str, str]]:
    """Parse DNF repoquery output, including DNF5 escaped-newline variants."""
    normalized = output.replace("\\r\\n", "\n").replace("\\n", "\n")
    packages: list[dict[str, str]] = []

    for line in normalized.splitlines():
        line = line.strip().strip("\\")
        if not line or "|" not in line:
            continue
        package_name, candidate = line.split("|", 1)
        package_name = package_name.strip()
        candidate = candidate.strip().strip("\\")
        if package_name and candidate:
            packages.append({"package": package_name, "candidate": candidate})

    # Some DNF5 builds concatenate formatted records. Recover records using
    # the package|EVR boundary without depending on output line breaks.
    if not packages:
        matches = re.findall(
            r"([A-Za-z0-9_+:.@-]+\.[A-Za-z0-9_+-]+)\|"
            r"([A-Za-z0-9:._+~^%-]+)",
            normalized,
        )
        packages = [{"package": name, "candidate": evr} for name, evr in matches]

    unique: dict[tuple[str, str], dict[str, str]] = {}
    for package in packages:
        unique[(package["package"], package["candidate"])] = package
    return list(unique.values())


def get_dnf_updates(container_id: int, skip_refresh: bool = False) -> list[dict[str, str]]:
    """Refresh DNF metadata and retrieve package upgrades."""
    if not skip_refresh:
        refresh = run_container_command(container_id, "dnf -q makecache --refresh", timeout=900)
        if refresh.returncode != 0:
            raise RuntimeError(refresh.stderr.strip() or translate("error_package_refresh"))

    query = r"""dnf -q repoquery --upgrades --queryformat '%{name}.%{arch}|%{evr}\n'"""
    result = run_container_command(container_id, query, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or translate("error_update_list"))

    packages = parse_dnf_query_output(result.stdout)
    if not packages and result.stdout.strip():
        # Fallback to DNF's check-update output when repoquery formatting differs.
        fallback = run_container_command(container_id, "dnf -q check-update", timeout=600)
        if fallback.returncode in (0, 100):
            for line in fallback.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 2 and "." in fields[0]:
                    packages.append({"package": fields[0], "candidate": fields[1]})
    return packages


APK_PACKAGE_PATTERN = re.compile(r"^(.+?)-([0-9][^-]*(?:-r[0-9]+)?)$")
APK_UPGRADABLE_FROM_PATTERN = re.compile(r"\[upgradable from:\s*([^]]+)]")


def split_apk_name_version(token: str) -> tuple[str, str] | None:
    """Split an apk token such as 'openssl-3.3.1-r0' into (name, version)."""
    match = APK_PACKAGE_PATTERN.match(token.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_apk_updates(output: str) -> list[dict[str, str]]:
    """Parse 'apk list --upgradable' output.

    Typical line:
      openssl-3.3.1-r0 x86_64 {main} [upgradable from: openssl-3.3.0-r0]
    """
    packages: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("WARNING") or line.startswith("OK:"):
            continue
        fields = line.split()
        if not fields:
            continue
        split = split_apk_name_version(fields[0])
        if not split:
            continue
        package_name, candidate = split

        installed = ""
        from_match = APK_UPGRADABLE_FROM_PATTERN.search(line)
        if from_match:
            installed_split = split_apk_name_version(from_match.group(1).split()[-1])
            if installed_split and installed_split[0] == package_name:
                installed = installed_split[1]

        key = (package_name, candidate)
        if key not in seen:
            seen.add(key)
            packages.append({"package": package_name, "candidate": candidate, "installed": installed})
    return packages


def get_apk_updates(container_id: int, skip_refresh: bool = False) -> list[dict[str, str]]:
    """Refresh APK metadata and retrieve package upgrades."""
    if not skip_refresh:
        refresh = run_container_command(container_id, "apk update", timeout=300)
        if refresh.returncode != 0:
            raise RuntimeError(refresh.stderr.strip() or translate("error_package_refresh"))

    result = run_container_command(container_id, "apk list --upgradable 2>/dev/null", timeout=180)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or translate("error_update_list"))
    return parse_apk_updates(result.stdout)


def get_installed_versions(
    container_id: int,
    manager: str,
    package_names: list[str],
) -> dict[str, str]:
    """Retrieve installed versions for a list of package names."""
    if not package_names:
        return {}

    quoted_names = " ".join(shlex.quote(name) for name in package_names)

    if manager == "apt":
        command = (
            f"dpkg-query -W -f='${{Package}}|${{Version}}\\n' {quoted_names} "
            "2>/dev/null || true"
        )
    elif manager == "dnf":
        command = (
            "for package in " + quoted_names + "; do "
            "version=$(rpm -q --qf '%{EVR}' \"$package\" 2>/dev/null || true); "
            "if [ -n \"$version\" ]; then printf '%s|%s\\n' \"$package\" \"$version\"; fi; "
            "done"
        )
    elif manager == "apk":
        # One pass over the installed list, then match locally: apk has no
        # efficient batch query for arbitrary package lists.
        command = "apk list -I 2>/dev/null || true"
    else:
        return {}

    result = run_container_command(container_id, command, timeout=180)
    wanted = set(package_names)
    installed: dict[str, str] = {}

    if manager == "apk":
        for line in result.stdout.splitlines():
            fields = line.split()
            if not fields:
                continue
            split = split_apk_name_version(fields[0])
            if split and split[0] in wanted:
                installed[split[0]] = split[1]
        return installed

    for line in result.stdout.splitlines():
        if "|" in line:
            package_name, version = line.split("|", 1)
            package_name = package_name.strip()
            if package_name in wanted:
                installed[package_name] = version.strip()
    return installed


def collect_container_updates(container_id: int, skip_refresh: bool = False) -> dict[str, Any]:
    """Collect update information for one running container."""
    container_status = get_container_status(container_id)
    record: dict[str, Any] = {
        "id": container_id,
        "status": container_status,
        "distribution": {},
        "manager": "unknown",
        "updates": [],
        "error": "",
    }

    if container_status != "running":
        return record

    try:
        distribution = get_distribution(container_id)
        record["distribution"] = distribution
        manager = detect_package_manager(container_id, distribution)
        record["manager"] = manager

        if manager == "apt":
            updates = get_apt_updates(container_id, skip_refresh)
        elif manager == "dnf":
            updates = get_dnf_updates(container_id, skip_refresh)
        elif manager == "apk":
            updates = get_apk_updates(container_id, skip_refresh)
        else:
            raise RuntimeError(translate("error_unsupported_manager"))

        installed_versions = get_installed_versions(
            container_id,
            manager,
            [item["package"] for item in updates],
        )

        for update in updates:
            update.setdefault("installed", "")
            if not update["installed"]:
                update["installed"] = installed_versions.get(update["package"], "")
            update["container_id"] = str(container_id)

        record["updates"] = updates
    except Exception as error:
        record["status"] = "error"
        record["error"] = str(error)

    return record


def build_html_document(title: str, content: str) -> str:
    """Wrap report content in a compact HTML document fragment."""
    return (
        '<div style="font-family:Arial,sans-serif;font-size:13px;line-height:1.4;">'
        f"<h2>{escape_html(title)}</h2>"
        f"{content}"
        "</div>"
    )


def build_container_report(container: dict[str, Any], generated_at: str) -> str:
    """Build the per-container HTML update report."""
    container_id = container["id"]
    distribution = container.get("distribution") or {}
    rows = []

    for update in container.get("updates", []):
        rows.append(
            "<tr>"
            f"<td>{escape_html(update['package'])}</td>"
            f"<td>{escape_html(update.get('installed') or '-')}</td>"
            f"<td>{escape_html(update['candidate'])}</td>"
            "</tr>"
        )

    if rows:
        table = (
            '<table border="1" cellspacing="0" cellpadding="4" '
            'style="border-collapse:collapse;width:100%;">'
            "<thead><tr>"
            f"<th>{escape_html(translate('column_package'))}</th>"
            f"<th>{escape_html(translate('column_installed'))}</th>"
            f"<th>{escape_html(translate('column_available'))}</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    else:
        table = f"<p>{escape_html(translate('no_updates'))}</p>"

    status_text = translate("status_running") if container["status"] == "running" else container["status"]
    summary = (
        f"<p><strong>{escape_html(translate('label_container'))}:</strong> {container_id}<br>"
        f"<strong>{escape_html(translate('label_status'))}:</strong> {escape_html(status_text)}<br>"
        f"<strong>{escape_html(translate('label_distribution'))}:</strong> "
        f"{escape_html(distribution.get('name', translate('unknown')))}<br>"
        f"<strong>{escape_html(translate('label_manager'))}:</strong> "
        f"{escape_html(container.get('manager', 'unknown'))}<br>"
        f"<strong>{escape_html(translate('label_generated'))}:</strong> {escape_html(generated_at)}"
        "</p>"
    )

    if container.get("error"):
        summary += (
            f"<p><strong>{escape_html(translate('label_error'))}:</strong> "
            f"{escape_html(container['error'])}</p>"
        )

    return build_html_document(
        translate("container_report_title", container_id=container_id),
        summary + table,
    )


def build_global_report(containers: list[dict[str, Any]], generated_at: str) -> str:
    """Build a global report grouped by distribution and package manager."""
    update_groups: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    container_ids_by_group: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    total_updates = 0

    for container in containers:
        if container["status"] != "running" or container.get("error"):
            continue
        distribution = container.get("distribution") or {}
        group_key = (
            str(distribution.get("name", translate("unknown"))),
            str(distribution.get("version", "unknown")),
            str(container.get("manager", "unknown")),
        )
        container_ids_by_group[group_key].add(container["id"])

        for update in container.get("updates", []):
            total_updates += 1
            package_name = update["package"]
            package_record = update_groups[group_key].setdefault(
                package_name,
                {"candidate": update["candidate"], "installed": {}, "candidates": set()},
            )
            package_record["candidates"].add(update["candidate"])
            package_record["installed"][container["id"]] = update.get("installed", "")

    content = (
        f"<p><strong>{escape_html(translate('label_generated'))}:</strong> "
        f"{escape_html(generated_at)}<br>"
        f"<strong>{escape_html(translate('label_containers'))}:</strong> {len(containers)}<br>"
        f"<strong>{escape_html(translate('label_total_updates'))}:</strong> {total_updates}</p>"
    )

    if not update_groups:
        content += f"<p>{escape_html(translate('no_updates'))}</p>"

    for group_key in sorted(update_groups):
        distribution_name, distribution_version, manager = group_key
        package_records = update_groups[group_key]
        group_container_ids = sorted(container_ids_by_group[group_key])
        content += (
            f"<h3>{escape_html(distribution_name)}"
            + (
                f" {escape_html(distribution_version)}"
                if distribution_version.lower() not in ("unknown", "")
                and distribution_version not in distribution_name
                else ""
            )
            + f" - {escape_html(manager)}</h3>"
        )

        headers = (
            "<th>" + escape_html(translate("column_package")) + "</th>"
            "<th>" + escape_html(translate("column_available")) + "</th>"
        )
        headers += "".join(f"<th>CT {container_id}</th>" for container_id in group_container_ids)

        rows = []
        for package_name in sorted(package_records):
            package_record = package_records[package_name]
            candidate_values = sorted(package_record["candidates"])
            candidate_text = ", ".join(str(candidate_values))
            if len(candidate_values) > 1:
                candidate_text += " [!]]"
            cells = [
                f"<td>{escape_html(package_name)}</td>",
                f"<td>{escape_html(candidate_text)}</td>",
            ]
            for container_id in group_container_ids:
                installed = package_record["installed"].get(container_id, "")
                cells.append(f"<td>{escape_html(installed or '-')}</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")

        content += (
            '<table border="1" cellspacing="0" cellpadding="4" '
            'style="border-collapse:collapse;width:100%;margin-bottom:16px;">'
            "<thead><tr>" + headers + "</tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>"
        )

    error_containers = [item for item in containers if item.get("error")]
    if error_containers:
        content += f"<h3>{escape_html(translate('section_errors'))}</h3><ul>"
        for container in error_containers:
            content += (
                f"<li>CT {container['id']}: {escape_html(container['error'])}</li>"
            )
        content += "</ul>"

    return build_html_document(translate("global_report_title"), content)


def remove_auto_report_block(description: str) -> str:
    """Remove generated report blocks while retaining user-written notes."""
    result = description or ""
    marker_pairs = [
        (BEGIN_MARKER, END_MARKER),
        (LEGACY_BEGIN_MARKER, LEGACY_END_MARKER),
    ]

    for begin_marker, end_marker in marker_pairs:
        pattern = re.compile(
            re.escape(begin_marker) + r".*?" + re.escape(end_marker),
            re.DOTALL,
        )
        result = pattern.sub("", result)

    return result.strip()


def replace_auto_report_block(description: str, report: str) -> str:
    """Replace the generated report block and preserve all personal notes."""
    personal_notes = remove_auto_report_block(description)
    if personal_notes:
        return f"{personal_notes}\n\n{BEGIN_MARKER}\n{report}\n{END_MARKER}"
    return f"{BEGIN_MARKER}\n{report}\n{END_MARKER}"


def limit_html_report(report: str, max_length: int) -> str:
    """Trim table data rows to fit the remaining Proxmox description capacity."""
    notice = (
        f"<p><strong>{escape_html(translate('report_shortened_title'))}</strong> "
        f"{escape_html(translate('report_shortened_message'))}</p>"
    )

    if len(report) <= max_length:
        return report
    if max_length <= len(notice):
        raise RuntimeError(translate("error_notes_capacity"))

    # Locate data rows only, preserving table headings and report structure.
    row_spans: list[tuple[int, int]] = []
    for body_match in re.finditer(r"<tbody\b[^>]*>.*?</tbody>", report, re.IGNORECASE | re.DOTALL):
        body_content = body_match.group(0)
        for row_match in re.finditer(r"<tr\b.*?</tr>", body_content, re.IGNORECASE | re.DOTALL):
            row_spans.append(
                (body_match.start() + row_match.start(), body_match.start() + row_match.end())
            )

    # Remove rows from the end; earlier offsets remain valid.
    while len(report) + len(notice) > max_length and row_spans:
        start, end = row_spans.pop()
        report = report[:start] + report[end:]

    if len(report) + len(notice) > max_length:
        raise RuntimeError(translate("error_report_base_too_large"))

    closing_division = re.search(r"</div>\s*$", report, re.IGNORECASE)
    if closing_division:
        report = report[:closing_division.start()] + notice + report[closing_division.start():]
    else:
        report += notice

    if len(report) > max_length:
        raise RuntimeError(translate("error_report_limit_failed"))
    return report


def get_proxmox_node_name() -> str:
    """Return the local Proxmox node name."""
    result = run_command(["hostname"], timeout=15)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(translate("error_hostname"))
    return result.stdout.strip()


def get_proxmox_description(config_path: str) -> str:
    """Read a Proxmox description field using pvesh."""
    result = run_command(["pvesh", "get", config_path, "--output-format", "json"], timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or translate("error_read_description"))
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(translate("error_parse_description")) from error
    # Community-scripts and similar tools store ANSI-colored banners in the
    # Notes; strip them so Proxmox renders clean text.
    return strip_ansi(config.get("description") or "")


def set_proxmox_description(config_path: str, description: str) -> None:
    """Write a Proxmox description field using pvesh."""
    result = run_command(
        ["pvesh", "set", config_path, "--description", description],
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or translate("error_write_description"))


def update_container_description(container_id: int, report: str, node_name: str) -> None:
    """Update a container description within Proxmox's 8192-character limit."""
    config_path = f"/nodes/{node_name}/lxc/{container_id}/config"
    old_description = get_proxmox_description(config_path)
    description_without_report = replace_auto_report_block(old_description, "")
    available_length = MAX_CT_DESCRIPTION_LENGTH - len(description_without_report)

    if available_length <= 0:
        raise RuntimeError(translate("error_notes_no_space"))

    limited_report = limit_html_report(report, available_length)
    new_description = replace_auto_report_block(old_description, limited_report)

    if len(new_description) > MAX_CT_DESCRIPTION_LENGTH:
        raise RuntimeError(translate("error_notes_limit_exceeded"))

    result = run_command(
        ["pct", "set", str(container_id), "--description", new_description],
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or translate("error_write_description"))


def update_node_description(report: str, node_name: str) -> None:
    """Update the Proxmox node description while preserving personal notes."""
    config_path = f"/nodes/{node_name}/config"
    old_description = get_proxmox_description(config_path)
    new_description = replace_auto_report_block(old_description, report)
    set_proxmox_description(config_path, new_description)


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Check package updates in Proxmox LXC containers."
    )
    parser.add_argument(
        "--lang",
        choices=AVAILABLE_LANGUAGES,
        default=DEFAULT_LANGUAGE,
        help=f"Report language (default: {DEFAULT_LANGUAGE}).",
    )
    parser.add_argument(
        "--ct-notes",
        action="store_true",
        help="Also write an individual report to each running container's Notes.",
    )
    parser.add_argument(
        "--skip-node-notes",
        action="store_true",
        help="Do not write the global report to the Proxmox node Notes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and print reports without changing Proxmox Notes.",
    )
    parser.add_argument(
        "--only",
        metavar="IDS",
        help="Scan only these containers, e.g. '100,105-110'.",
    )
    parser.add_argument(
        "--exclude",
        metavar="IDS",
        help="Skip these containers, e.g. '101,200-210'.",
    )
    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help="Do not refresh package metadata (faster, but lists may be stale).",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=DEFAULT_MAX_PARALLEL,
        metavar="N",
        help=f"Number of containers scanned concurrently (default: {DEFAULT_MAX_PARALLEL}).",
    )
    return parser.parse_args()


def main() -> int:
    """Run the update scan and publish reports."""
    global TRANSLATIONS

    arguments = parse_arguments()
    TRANSLATIONS = load_translations(arguments.lang)

    if os.geteuid() != 0:
        print(translate("error_root_required"), file=sys.stderr)
        return 1

    if arguments.max_parallel < 1:
        print("ERROR: --max-parallel must be at least 1.", file=sys.stderr)
        return 1

    try:
        node_name = get_proxmox_node_name()
        container_ids_all = get_container_ids()
        container_ids = filter_container_ids(container_ids_all, arguments.only, arguments.exclude)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(
        translate(
            "scan_start_parallel",
            count=len(container_ids),
            workers=min(arguments.max_parallel, max(len(container_ids), 1)),
        )
    )

    container_results: dict[int, dict[str, Any]] = {}
    print_lock = Lock()

    def scan_worker(scan_container_id: int) -> dict[str, Any]:
        scan_result = collect_container_updates(scan_container_id, arguments.skip_refresh)
        with print_lock:
            if scan_result.get("error"):
                print(translate("scan_error", container_id=scan_container_id, error=scan_result["error"]))
            else:
                print(
                    translate(
                        "scan_complete",
                        container_id=scan_container_id,
                        count=len(scan_result.get("updates", [])),
                    )
                )
        return scan_result

    with ThreadPoolExecutor(max_workers=arguments.max_parallel) as executor:
        futures = {executor.submit(scan_worker, cid): cid for cid in container_ids}
        for future in as_completed(futures):
            container_id = futures[future]
            try:
                container_results[container_id] = future.result()
            except Exception as error:
                container_results[container_id] = {
                    "id": container_id,
                    "status": "error",
                    "distribution": {},
                    "manager": "unknown",
                    "updates": [],
                    "error": str(error),
                }
            result = container_results[container_id]

            if arguments.ct_notes and result["status"] == "running":
                try:
                    container_report = build_container_report(result, generated_at)
                    if arguments.dry_run:
                        with print_lock:
                            print(f"\n--- CT {container_id} REPORT ---\n{container_report}\n")
                    else:
                        update_container_description(container_id, container_report, node_name)
                        print(translate("ct_notes_updated", container_id=container_id))
                except Exception as error:
                    print(
                        translate("ct_notes_error", container_id=container_id, error=error),
                        file=sys.stderr,
                    )

    ordered_results = [container_results[cid] for cid in container_ids if cid in container_results]
    global_report = build_global_report(ordered_results, generated_at)

    if arguments.dry_run:
        print("\n--- GLOBAL REPORT ---")
        print(global_report)
    elif not arguments.skip_node_notes:
        try:
            update_node_description(global_report, node_name)
            print(translate("node_notes_updated"))
        except Exception as error:
            print(translate("node_notes_error", error=error), file=sys.stderr)
            return 1

    total_updates = sum(len(item.get("updates", [])) for item in ordered_results)
    print(translate("scan_finished", count=len(ordered_results), updates=total_updates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
