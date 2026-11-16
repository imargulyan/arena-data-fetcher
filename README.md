# Arena PDF downloader

Python 3.10+ command-line app that reads `pn.json`, looks up each unique part
number, and downloads PDFs attached directly to that item into `data/`.
It does not follow linked document items or enumerate older item revisions.
The item revision returned by Arena's normal item search is used.

## Setup

```powershell
python -m pip install -r requirements.txt
```

Fill in your existing `.env` using the variable names from `.env.example`:

```dotenv
ARENA_API_BASE_URL=https://api.arenasolutions.com/v1
ARENA_EMAIL=your-arena-email
ARENA_PASSWORD="your-arena-password"
ARENA_WORKSPACE_ID=123456789
```

Use the API URL for your region from
[Arena's documentation](https://ptc-arena.github.io/arena-dev-help/).
Workspace ID is optional but recommended. The account must have REST API access
and permission to read the parts and files. Environment variables take precedence
over `.env`. `ARENA_BASE_URL` is also accepted as an alias. The existing
`ARENA_USAGE_REASON` setting is not required by this downloader.
Credentials and downloaded files are ignored by Git. If `.env` was already
tracked, remove it from Git's index with `git rm --cached .env` (this keeps the
local credentials file).

## Run

```powershell
python fetch_pdfs.py
```

Preview the first part using the live API without downloading:

```powershell
python fetch_pdfs.py --dry-run --limit 1
```

The input must be a JSON array of strings. Duplicates are processed once, keeping
the input order. Numbers are matched exactly, including embedded spaces.

Files are named `PART_NUMBER_01_Original Document Title.pdf`, then `02`, etc.,
starting at `01` for each part. The Arena file title is used when present, with
the original filename as a fallback. Windows-invalid characters are replaced
with underscores; long names are shortened. Numbering follows title then
association GUID order, so it is stable while attachments stay unchanged.

Downloads are streamed to temporary files, checked for a PDF header, then moved
into place. A rerun downloads current files again and replaces matching output
names only after a successful download. Old files whose names no longer appear
in Arena are retained. The original empty `data` placeholder, if present, is
preserved as `data.empty-file` when the folder is first prepared.

`download-report.json` records the item and attachment GUIDs, output filenames,
successful downloads, missing parts, parts without PDFs, and failures. It is
replaced each run, including dry runs. Failures on a part/file do not stop the
remaining parts. Exit code is `1` for errors or missing parts, otherwise `0`;
a part with no PDFs is reported without failing the run.

Optional flags: `--parts PATH`, `--output PATH`, `--env-file PATH`,
`--report PATH`, `--limit N`, and `--dry-run`. Default paths are relative to the
script directory. Explicit relative paths are relative to your current directory.

The app paginates searches and attachments, retries temporary HTTP errors on
GET requests, and logs out after processing. HTTP requests time out after 60
seconds. Persistent API errors and exhausted rate limits are recorded for retry
in a later run.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests use simulated Arena responses and temporary directories; they require no
credentials and do not contact Arena.
