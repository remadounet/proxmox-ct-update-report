# proxmox-ct-update-report

Check package updates in Proxmox LXC containers (apt/nala, dnf, apk/Alpine)
and publish HTML reports directly in the Proxmox Notes of the node and,
optionally, of each container.

The script uses only the Python 3 standard library plus tools already present
on the Proxmox host and inside the containers. It installs nothing.

## Features

- Scans every running LXC container via `pct exec`
- Supports **apt** (and **nala**), **dnf** (incl. DNF5 quirks) and **apk** (Alpine Linux)
- Per-container report and a global report grouped by distribution and package manager
- Reports are written into the Proxmox Notes (node and/or each CT), preserving your personal notes between auto-generated markers
- Handles Proxmox's 8192-character Notes limit by trimming table rows gracefully
- Parallel scanning (`--max-parallel`), container filtering (`--only` / `--exclude`), optional metadata refresh skip (`--skip-refresh`), dry-run mode
- 9 UI languages: en, fr, es, de, it, pt, pl, ru, uk

## Usage

Run as **root** on the Proxmox host:

```bash
# Dry run: print reports without touching any Notes
./check-ct-updates.py --dry-run

# Full run: global report on the node Notes
./check-ct-updates.py

# Also write each container's Notes, in French
./check-ct-updates.py --ct-notes --lang fr

# Only CTs 100 and 105-110, skip none
./check-ct-updates.py --only 100,105-110

# Exclude some CTs, 8 parallel scans, don't refresh metadata
./check-ct-updates.py --exclude 101,200-210 --max-parallel 8 --skip-refresh
```

### Options

| Option | Description |
| --- | --- |
| `--lang LANG` | Report language: `en`, `fr`, `es`, `de`, `it`, `pt`, `pl`, `ru`, `uk` (default `en`) |
| `--ct-notes` | Also write an individual report to each running container's Notes |
| `--skip-node-notes` | Do not write the global report to the node Notes |
| `--dry-run` | Print reports without modifying any Notes |
| `--only IDS` | Scan only these containers, e.g. `100,105-110` |
| `--exclude IDS` | Skip these containers, e.g. `101,200-210` |
| `--skip-refresh` | Do not refresh package metadata (faster, results may be stale) |
| `--max-parallel N` | Number of containers scanned concurrently (default 4) |

### Scheduling

Example cron entry (every Monday at 06:00):

```cron
0 6 * * 1 root /usr/local/bin/check-ct-updates.py --ct-notes --lang fr >/var/log/ct-update-report.log 2>&1
```

## Notes format

Generated reports are enclosed in markers so your own notes are never lost:

```
Your personal notes...

<!-- BEGIN AUTO UPDATE REPORT -->
...generated HTML report...
<!-- END AUTO UPDATE REPORT -->
```

Legacy `--- BEGIN/END AUTO UPDATE REPORT ---` markers from older versions are
also recognized and migrated.

## Tests

Run the unit tests for the parsing and selection helpers:

```bash
python3 -m unittest discover -s tests -v
```

## License

CC0 1.0 Universal (public domain).
`
