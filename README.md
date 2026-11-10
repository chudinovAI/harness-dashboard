# Harness Session Dashboard

A local dashboard for finding sessions by project and harness, reading history, browsing storage locations, and viewing confirmed relationships. Local sources are not modified. When explicitly configured, Hermes sessions on a VPS are read over existing SSH access; no other external services or telemetry are used.

## Running on macOS

From the repository root:

```sh
python3 server.py
```

Open the address printed in the terminal: http://127.0.0.1:8765/ by default. No Python library installation or build is needed. Reading compressed DeepSeek Harness sessions requires `zstd` in `PATH`; if it is missing, the dashboard displays a source error. Stop the server with `Ctrl+C`. To use another port or an automatically selected free port: `python3 server.py --port 8766` or `--port 0`. Use `python3 server.py --open` to open the browser automatically.

OMP, DeepSeek Harness, Claude Code, ZCode, Codex, OpenCode, local Hermes, and optional Hermes on a VPS are supported. Without configuration, the remote source displays an error and no connection is made; the other sources continue to work. For local Claude, the default folder and `CLAUDE_CONFIG_DIR/projects` are used if the variable is set. A missing source does not prove that no other profiles or paths exist.

### Local Launchers

The local `launchers/` folder contains this machine's dashboard app and Hermes UI shortcut. It is ignored by Git and is not included when cloning the repository.

## Theme

The header offers System, Light, and Dark themes. System is the default and follows live OS appearance changes. An explicit choice is saved in this browser for the current origin; choosing System clears the override. The theme is applied before the page is rendered.

## Optional Hermes VPS Configuration

Create a local `.local/hermes.json` file with owner-only permissions (`chmod 600 .local/hermes.json`). Substitute your own configured SSH alias and the absolute path to the Hermes directory on the remote machine. The example uses dummy values:

```json
{
  "ssh_alias": "example-hermes",
  "hermes_root": "/home/example/.hermes"
}
```

The root must be absolute, with no `.` or `..` components and no trailing `/`. Start the dashboard with `python3 server.py --hermes-config .local/hermes.json`. **When the page is opened, the dashboard automatically loads the source list and attempts to connect to the specified VPS over SSH**; refreshing the list also initiates a connection. Without this argument, the file is not read automatically and SSH is not started. The configuration is read at startup; restart the dashboard after changing it. An invalid or inaccessible file stops startup with an error. The `.local/` folder is excluded from Git, but configuration files elsewhere must be protected separately.

The SSH alias must already be configured for batch login without a password prompt; the host key must have been verified beforehand. Only a fixed worker is run over SSH, with request data passed through stdin. On the VPS, only `state.db`, its WAL, and files under `sessions/` within the selected root are read; symlink path components are not accepted.

Select a project or source in the navigation; open “Files” next to a source. Names, paths, sizes, and dates within known roots are shown for inventory purposes. Content is available only in permitted session and artifact areas; configuration files and files whose names suggest secrets cannot be opened. This is a path and filename filter, not content analysis: sessions and permitted files may themselves contain secrets. Symbolic links are not followed. Remote Hermes files are accessible only in the `sessions/` folder within the selected root; Finder opens only local paths.

History, large text files, and directories are loaded in pages. The history cursor is the position of the displayed block: if a previously read part of the source changes between requests, items may shift; refresh the list and reopen the session. PNG, JPEG, GIF, and WebP images within supported entries are displayed if valid and no larger than 8 MiB; unsupported data and unavailable files are indicated. Hidden internal Hermes and ZCode messages are not shown. Current activity status cannot be inferred from a file's modification date.

## Privacy and Limitations

The server is accessible only at `127.0.0.1`; the API requires a random token for the current run and checks the local Host/Origin. Markdown is rendered using safe DOM elements, without executing HTML from sessions; external links and media are not loaded automatically. Local applications running with the same user's permissions still have access to local data.

To read SQLite with WAL correctly, the application temporarily creates a private snapshot of the database and WAL on the same machine as the source: locally or on the VPS. The snapshot may include other tables containing sensitive data; it is deleted after normal closure or an error, but may remain on disk if the process is forcibly terminated or crashes. The source database is not modified. A database with a rollback journal, including inactive PERSIST, cannot be read; if files change while the copy is being made, try refreshing the list. The snapshot size is limited to 2 GiB.

## Layout

| Path | Responsibility |
|---|---|
| `server.py` | Command-line entry point. |
| `harness_dashboard/server.py` | Loopback HTTP server, API protection, session index, and Finder integration. |
| `harness_dashboard/file_adapters.py`, `file_history.py`, `source_files.py` | File-backed sources, history pages, and permitted file previews. |
| `harness_dashboard/db_adapters.py`, `db_details.py` | SQLite-backed sources, private snapshots, and history pages. |
| `harness_dashboard/remote_adapter.py`, `remote_worker.py` | Optional Hermes VPS access over SSH; the worker remains a standalone script. |
| `static/` | Local HTML, CSS, and JavaScript; no build step. |
| `launchers/` | Machine-local launchers, excluded from Git. |

There is no persistent application database: the index is kept in memory, and history pages use session IDs from the latest index. A project is identified by storage location and path, so identical paths on the local machine and VPS are not combined. Sharing a project does not prove a parent-child session relationship.

## Third-Party Materials

The interface uses app-screen guidance and the Almanac theme from [Hallmark](https://github.com/Nutlope/hallmark), without network-hosted fonts or images. The MIT notice for these materials is preserved below:

```text
MIT License

Copyright (c) 2026 Hallmark contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
