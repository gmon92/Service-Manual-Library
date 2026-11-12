# Service Manual Library

Offline search and reading of local HTML vehicle manuals using `manual.py`.
Requires Python **3.9 or newer** with SQLite **FTS5** support; no third-party
Python packages are required. Run commands from this repository. Examples use
PowerShell and assume `python` selects that interpreter (substitute `py -3` or
an explicit interpreter path if needed).

### Manuals in the repository library

The CLI defaults to `Vehicle Manuals/` beside `manual.py`, regardless of the
current working directory. This folder is ignored by Git to keep manual data
and indexes out of the repository history. The root must exist before discovery;
the CLI does not create it automatically. The following examples assume a
Cadillac manual and index have been installed there.

```powershell
python manual.py vehicles
$Vehicle = '2013 Cadillac XTS FWD V6-3.6L'
python manual.py search --vehicle $Vehicle 'How do I replace the water pump?' --limit 5
python manual.py read --vehicle $Vehicle pages/2044.html
python manual.py broken --vehicle $Vehicle --limit 20
```

`read` requires an exact indexed relative page path; copy a `path` from search
results to read a matching page. Citations are `file:` URIs pointing to original
HTML or local linked assets. Open the original HTML to see layout and diagrams.

`vehicles` recursively discovers schema-1 indexes with `.sqlite`, `.sqlite3`,
`.db`, or `.db3` extensions. It skips directory symlinks, warns about skipped
index symlinks and invalid databases, and reports whether each source exists.
The root must exist. Override it with `--library-root`:

```powershell
python manual.py vehicles --library-root 'D:/Manuals' --json
```

For an existing library in the previous location, pass
`--library-root 'C:/Users/gabem/Documents/Vehicle Manuals'` to discovery commands.
Changing the default does not move or copy any existing manual data.

Every `search`, `read`, and `broken` call requires the **exact, case-sensitive**
`--vehicle` label, even when only one index is available. Duplicate labels are
ambiguous; select an explicit database with `--db`. An explicit database bypasses
discovery and `--library-root`, but still requires the matching vehicle label.

```powershell
$Db = './Vehicle Manuals/2013 Cadillac XTS FWD/Index/manual.sqlite'
python manual.py search --db $Db --vehicle $Vehicle 'water pump' --json
python manual.py read --db $Db --vehicle $Vehicle pages/2044.html --json
```

### Search and command defaults

Plain search is the default: punctuation separates words, common question and
connector words are removed, and all remaining words must match as literal FTS
tokens. It is not semantic search or exact phrase search. For example, `water
pump` requires both words, and `brake OR rotor` requires both `brake` and `rotor`
because `or` is removed. An empty query or one with no searchable words is an
error. Quote the whole query as one shell argument.

Use `--fts` to opt into SQLite FTS5 syntax for phrases, prefixes and operators:

```powershell
python manual.py search --vehicle $Vehicle '"water pump"' --fts --limit 5
python manual.py search --vehicle $Vehicle 'brak* OR rotor' --fts
```

Search orders matches by FTS5 BM25, weighting title matches more heavily than
body matches, then by path. Search defaults to `--limit 10`; `broken` defaults
to `--limit 20`. Limits must be positive integers. Search has no pagination or
total-match count; `result_count` counts returned results. No matches is a
successful response with `status: "no_results"`.

`--library-root` and `--json` work before or after the `vehicles`, `search`,
`read`, and `broken` subcommands. `--db` and `--vehicle` follow their subcommand.
Successful commands, including an empty vehicle listing, exit 0. Handled errors
exit 1; help exits 0. Without `--json`, handled errors go to stderr. With
`--json`, handled errors are a JSON object on stdout. `extract` and `index`
produce text output only; a global `--json` is rejected for those commands.

### JSON schema overview

`--json` emits one object. This is an overview of the implemented response
shape, not a separate JSON Schema validation file.

| Response | Fields |
| --- | --- |
| Handled error | `status: "error"`, `error` string |
| `vehicles` | `command`, `status` (`ok` or `no_indexes`), absolute `library_root`, `vehicles` array, `warnings` string array |
| Vehicle entry | `vehicle`, absolute `db`, absolute `source`, `source_exists` boolean (source directory exists) |
| `search`, `read`, `broken` common fields | `command`, `status`, `vehicle`, absolute `db`, absolute `source`, `warnings` string array |
| `search` additions | Original `query`, `query_mode` (`plain` or `fts`), `results` page records with `snippet`, `result_count`, `limit`; status is `ok` or `no_results` |
| `read` additions | Page record fields at the top level, plus complete cached `body`; status is `ok` |
| `broken` additions | `missing_occurrences`, `distinct_targets`, `targets` array of `{target, occurrences}`, `limit`; status is `ok` |

Each page record contains `path`, `title`, original-file `citation`,
`source_exists` (page file exists), `warnings`, `safety_notices`, `links`,
`diagrams`, `missing_links_at_index`, and `missing_links_now`. `diagrams` repeats
the image entries from `links`. Search records have a short `snippet` with
matches bracketed; only `read` returns the full extracted `body`.

Each link contains `kind` (`a`, `img`, `link`, or `script`), original `url`,
`alt`, resolved relative `target` (or an outside-manual marker or null),
`citation` (file URI or null), `missing_at_index` boolean, `exists_now` (boolean
or null), and `status` (`present`, `missing`, `outside_manual`, or `unchecked`).
External URLs and fragment-only links are unchecked. Link status uses current
local file checks; `broken` totals use the indexing snapshot, including outside
targets marked missing at indexing. Counts are link occurrences or distinct
targets, not counts of missing procedures.

### Add a manual and create its index

New extraction destinations and new SQLite indexes are allowed inside the
default library, `Vehicle Manuals/` beside `manual.py`, so future vehicles
can be added there. Other locations are also supported. Both destination paths
must be new: extraction refuses any existing file or directory (even an empty
directory), and indexing refuses any existing database path. The database must
be outside its source tree; use sibling `Original` and `Index` directories.
Original archives, existing manual files, and existing indexes are never
overwritten or edited. `--library-root` controls discovery, not write permission;
`--dest` and `--db` explicitly select the new paths to create.

```powershell
$NewVehicle = './Vehicle Manuals/Example Vehicle'
python manual.py extract --zip 'D:/Downloads/vehicle.zip' --dest "$NewVehicle/Original"
python manual.py index --source "$NewVehicle/Original" --db "$NewVehicle/Index/manual.sqlite" --vehicle 'EXACT VEHICLE LABEL'
python manual.py vehicles
```

Extraction requires one archive directory root and removes that root. It checks
CRC and rejects traversal, symlinks, reserved Windows names, duplicate
case-insensitive paths, and file/directory collisions. Use trusted archives;
there is no malware scan, authenticity verification, or resource limit for
archive expansion. A failure during extraction can leave a reported partial
destination; inspect it and choose a fresh destination before retrying. The tool
does not delete or modify existing manual files.

Indexing rejects symlinks in the source tree and requires at least one `.html`
or `.htm` file. It writes SQLite schema version `1`: `meta` stores vehicle,
absolute source path and schema; `pages` stores relative paths, titles and
extracted bodies; `search` is the FTS5 index; `links` stores link metadata and
missing-target snapshots. Images and original files are not copied into SQLite.
A failed build removes the newly created database. Rebuild to a new database
path after moving or changing the source; exclude stale indexes from discovery
or use `--db` when their labels collide.

### Limitations

Manuals are third-party material. Verify vehicle, safety warnings, procedures
and values against authoritative service information. Search snippets and
keyword-extracted safety notices are incomplete aids; consult the full original
procedure, diagrams and warnings. Only open trusted local HTML, which may run
scripts in a browser.

HTML parsing is best effort, assumes UTF-8 with replacement for invalid bytes,
and loses formatting, table structure and image context. Script/style text is
excluded. Images are not OCR indexed; PDFs, CSS references, JavaScript-generated
links, frames and other uncollected HTML attributes are not indexed. Link
resolution does not emulate a browser, check external sites, or validate
fragment anchors.

Reads use cached index text, so a missing or changed source does not update the
body. Missing source directories/pages produce warnings and file-existence
flags. Search/read/broken and discovery open indexes read-only in SQLite
immutable mode, avoiding journal/SHM creation. Use completed, stable databases;
immutable reads are unsuitable for a database being concurrently modified.

### Tests

```powershell
python -m unittest discover -s tests -v
```

On this workstation, an accessible Python 3.13 interpreter is bundled with
pgAdmin; the verified invocation is:

```powershell
& 'C:/Program Files/PostgreSQL/18/pgAdmin 4/python/python.exe' -m unittest discover -s tests -v
```

The suite verifies the script-relative default from another working directory
and uses temporary fixture manuals to cover discovery, vehicle isolation,
plain/advanced search, JSON and argument errors, limits, missing sources, current
link status, read-only queries, new destinations under a simulated default
library root, existing destination/index protection, source-tree index rejection,
extraction validation, and failed-build cleanup. All extraction and index-write
tests use temporary fixtures, never the real library. No third-party test runner
is needed. Python cache
files are ignored by `.gitignore`.

`RealCadillacTests` additionally searches `water pump`, reads the first result,
and checks discovery with an explicit legacy library root,
`C:/Users/gabem/Documents/Vehicle Manuals`. It verifies the
index hash and modification time, index-directory file list, and read source
page bytes remain unchanged. This test skips when the legacy library root is
absent; if that root exists, the expected Cadillac database must be available.
Fixture tests remain usable without that library. The real test performs no
extraction or indexing in the existing library.
