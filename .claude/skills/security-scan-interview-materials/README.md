# security-scan-interview-materials

A static, read-only scanner and Claude Code skill for interview materials such as take-home zips, "review this repo" requests and coding challenges. It flags fake-recruiter malware before anything is opened or run. `SKILL.md` holds the agent instructions. This file is for whoever audits or maintains the code.

The detection logic follows `fake-interview-malware-research.md` (compiled 2026-10-05). The behavioral rules come from its sections 7 and 8, and the time-sensitive indicators from section 9.

## Running

```bash
uv run --no-project --no-config --offline --python /app/venv/bin/python -- \
  python -I ~/.claude/skills/security-scan-interview-materials/scan.py <zip|tar|dir|git-url> [--registry-lookup] [--password PW] [--out DIR]
uv run --no-project --no-config --offline --python /app/venv/bin/python -- \
  python -I ~/.claude/skills/security-scan-interview-materials/scan.py --host-check
```

| Flag | Why it is there |
|---|---|
| `--no-project` | uv never reads a `pyproject.toml` from the current directory |
| `--no-config` | uv never reads a `uv.toml` or `[tool.uv]` from the current directory |
| `--offline` | uv never resolves or installs anything |
| `--python /app/venv/bin/python` | the existing venv interpreter (Python 3.13) |
| `python -I` | isolated mode: ignores `PYTHON*` environment variables and user site-packages, and keeps the current directory off `sys.path` |

`scan.py` adds its own directory to `sys.path` because `-I` would otherwise drop it.

Exit codes: `0` no HIGH or CRITICAL findings, `2` HIGH, `3` CRITICAL, `1` usage or scanner error.

Output goes to `~/.cache/security-scan-interview-materials/<name>-<timestamp>/` unless `--out` is given:
- `extracted/`: the inert analysis copy
- `report.txt`: the human-readable report
- `findings.json`: the machine-readable report
- `git-sanitized/`: the sanitized copy of `.git` used to read history

## Safety design

- **Standard library only.** No third-party dependencies, so the scanner itself can't be supply-chained.
- **Never executes target code.** The only subprocesses are `git clone` (for URL targets, with hooks disabled, LFS smudge skipped and no submodules) and `git log` against a sanitized copy of `.git`. That copy has `hooks/` and `config` removed, a minimal config written in their place, and `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_NOSYSTEM` neutralized. A shipped `.git/config` (`core.fsmonitor`, filters, aliases and similar) is reported, never honored.
- **Safe extraction.** Entries with `..`, absolute or drive-letter paths, symlinks, hardlinks and device files are rejected and reported. Size, entry-count and compression-ratio limits stop archive bombs. Files are written with `O_EXCL|O_NOFOLLOW` and mode `0600`, and every execute bit is removed afterwards. Nested archives (zip, tar, gz, so also `.vsix`, `.whl`, `.jar`, `.docx`, `.pt`) are extracted into `<name>.__extracted__/` up to 3 levels deep.
- **Agent config quarantine.** `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.claude/`, `.mcp.json` and similar are renamed `*.UNTRUSTED` in the extracted copy, so an AI agent reading files there can't auto-load them. Reports still use the original paths.
- **Sanitized evidence.** URLs are defanged (`hxxps://evil[.]com`), and invisible or format Unicode characters are shown as `\u{...}`. Snippets are truncated, so hidden instructions don't reach whoever reads the report.
- **Offline by default.** `--registry-lookup` contacts only `registry.npmjs.org`, `api.npmjs.org` and `pypi.org` to read metadata. URLs found in the target are never contacted.

## Code map (`interview_scan/`)

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing, work directory, entry point |
| `pipeline.py` | Runs the phases in order. A failing phase is recorded as a `SCANNER-ERROR` finding, so the rest of the scan still runs. |
| `config.py` | Loads `rules.toml` and `iocs.toml` into typed rule objects |
| `context.py` | `ScanContext` (shared state), `FileRecord`, `Dependency`, `ScanOptions` |
| `findings.py` | `Severity`, `Finding`, `FindingCollector` (deduplicates by rule, path and decode chain) |
| `textutil.py` | Line index, snippets, defanging, JSONC parsing, glob matching, entropy |
| `acquisition.py` | Safe clone, copy and extraction, nested archives, permission stripping, quarantine |
| `inventory.py` | Walks the copy without following links. Hashes, magic bytes, text decoding |
| `surfaces.py` | Presence-based execution surfaces (`[[surface]]` rules) |
| `editor_configs.py` | `.vscode/tasks.json`, `settings.json`, `launch.json`, `extensions.json`, `*.code-workspace`, `devcontainer.json` |
| `command_analysis.py` | Detects an interpreter being run on a non-code file (fonts, images, `.dat`) |
| `agent_configs.py` | AI-agent instruction files, `.claude/settings*.json`, MCP configs |
| `content_rules.py` | `[[pattern]]`, `[[combo]]` and IOC string rules over text (and decoded text) |
| `network.py` | URL/IP extraction, host classes, IOC domains/IPs, split-string URLs, wallet extension IDs |
| `decoding.py` | Base64, reversed base64, hex, `\x` escapes, char-code arrays, then zlib/gzip/bz2/lzma, recursively up to 5 layers, rescanning each layer |
| `obfuscation.py` | Long lines, entropy, invisible Unicode and tag-character text, magic-byte mismatch, code in data files, binaries, DLL side-loading |
| `typosquat.py` | Edit distance and suffix-based impersonation checks |
| `npm_manifests.py` | `package.json` scripts and dependencies, lockfiles, `.npmrc`/`.yarnrc*`, unused dependencies |
| `python_manifests.py` | `requirements*.txt`, `pyproject.toml`, `Pipfile`, vendored copies of popular libraries |
| `pickle_scan.py` | Lists pickle imports with `pickletools.genops`, which parses without loading |
| `git_provenance.py` | Dangerous `.git/config` keys, commit history heuristics, repo and company name IOCs |
| `registry_lookup.py` | Optional npm/PyPI metadata checks |
| `host_check.py` | Optional read-only Linux host check for infection artifacts |
| `report.py` | Verdict, text report, JSON report, exit code |

## Updating the data files

Neither change touches the Python code. Run the tests afterwards.

### `iocs.toml` (time-sensitive indicators)

Update this when new campaign reports come out. Write values refanged (plain dots). Bump `updated`: the report warns when the list is older than `limits.ioc_max_age_days` (45).

| Key | Meaning |
|---|---|
| `domains`, `urls`, `ips` | Exact indicators. A match anywhere, even without a URL scheme, is CRITICAL. |
| `domain_patterns`, `url_patterns`, `c2_paths` | Regexes. `c2_paths` and `c2_ports` make raw-IP URLs CRITICAL. |
| `[packages] npm / pypi` | Known malicious package names, checked in manifests, lockfiles and npm aliases |
| `[hashes]` | `sha256 = "label"`, checked against every extracted file |
| `[wallet_extension_ids]` | Chrome extension IDs that infostealers target |
| `[[strings]]` | `value` (regex), `severity`, `label`: malware family strings |
| `repo_names`, `fake_companies` | Matched against project names and file text |
| `[host_artifacts] paths` | Used only by `--host-check` |

### `rules.toml` (behavioral detection)

These rules are the durable part and keep catching new variants. Regexes use TOML literal strings (`'''...'''`) and Python `re` syntax, compiled with `re.MULTILINE`. Put inline flags such as `(?i)` only at the very start of a pattern.

- `[[pattern]]`: `id`, `severity` (`info|low|medium|high|critical`), `title`, `category`, `regex`. Optional: `files` (globs; when set, the rule is not applied to decoded content), `exclude` (globs), `min_count` (report only after N matches), `explain`.
- `[[combo]]`: every regex in `all` must match somewhere in the same file. Optional `files`. Use this for "A and B in one file" behaviors such as network access plus exec.
- `[[host_class]]`: a regex over the full URL, with `id`, `severity` and `title`. Severities are capped at `hosts.docs_max_severity` in documentation files.
- `[[surface]]`: flags the presence of a file by path glob. Use `once = true` for directory-level surfaces.
- `[[vscode_setting]]`: regexes for setting keys (and optionally string values).
- `[[agent_file]]` and `[quarantine]`: which agent files are reported, and which are renamed.
- `[commands]`: shared regexes for dangerous shell commands, network fetches and interpreters (used by tasks, npm scripts, devcontainer and agent files).
- `[popular]`, `[vendored]`: names for typosquat and vendored-copy checks.
- `[limits]`: size caps, decode depth, entropy and long-line thresholds, registry age thresholds.

Path globs without `/` match the file name. Globs with `/` match the relative path, or any suffix of it. `*` also matches across `/`.

## Tests

```bash
cd ~/.claude/skills/security-scan-interview-materials
uv run --no-project --no-config --offline --python /app/venv/bin/python -- python -I tests/test_scan.py
```

`tests/test_scan.py` generates, in a temp directory, a synthetic malicious assignment. It is inert text that is never executed and whose URLs are never contacted. It includes:
- a `folderOpen` task with per-OS `curl | sh` and hidden output
- a "font" that is really JavaScript, launched by a task
- `.config.env` with a base64 key that decodes to a JSON-store URL
- an axios-then-eval loader, a URL split across string fragments, and a triple-base64 payload
- a wallet extension ID and a browser credential path
- Unicode tag-character prompt injection and a hidden README comment aimed at AI reviewers
- a CLAUDE.md asking the agent to run a command
- a malicious npm postinstall, an IOC package and a typosquat
- a pickle that imports `os.system`, and a `conftest.py` loader
- a backdated git history with a `.vscode`-only last commit and an `fsmonitor` in `.git/config`
- zip path-traversal and symlink entries

The tests assert that every expected rule fires, that the extracted copy has no executable files and no live CLAUDE.md, and that snippets are defanged. A benign Express app must produce no HIGH or CRITICAL findings. When you add a rule, add a sample line to the fixture and the rule ID to the matching test.

False-positive check used during development: zero CRITICAL findings on `/usr/share/nodejs` (about 800 real npm packages, 22k files) and on the installed `requests`/`aiohttp` packages.

## Known limits

- This is static analysis. A payload fetched at runtime from a clean-looking URL is only visible as that URL. Packages can turn malicious after the scan (delayed activation), so pin versions and rescan before any install.
- Compiled binaries are reported, not reverse engineered. Hashes are checked against `iocs.toml` only.
- Dependency source code is not downloaded. `--registry-lookup` checks metadata (age, downloads, install scripts, npm security placeholders) only.
- Files larger than 5 MB are scanned only up to the limit, and this is reported.
- `--host-check` covers Linux locations only.
