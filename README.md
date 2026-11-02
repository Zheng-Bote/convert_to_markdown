# Documents → Markdown

Batch-convert documents to GitHub-Flavored Markdown with [Docling](https://docling-project.github.io/docling/) as the primary engine and [Pandoc](https://pandoc.org/) as a fallback.

Designed for real-world document folders: it walks directories, picks the best available source format for each document, skips work that is already done, and keeps going when a single file is broken.

```bash
python3 convert_to_markdown.py                 # current directory -> ./md/
python3 convert_to_markdown.py library -r      # recursive -> library/md/
python3 convert_to_markdown.py book.epub       # a single file -> next to it
```

## Features

- **Two engines, one command.** Docling handles `.epub`, `.pdf`, `.docx`, `.odt`. If Docling fails or does not know the format, Pandoc takes over automatically.
- **Best-source selection.** If the same document exists in several formats, only the best one is converted (`epub` > `docx`/`odt` > `pdf`).
- **Idempotent runs.** A Markdown file that is newer than its source is skipped, so re-running is cheap. `--force` overrides this.
- **Output stays out of the way.** Converting a folder writes into a `md/` subfolder of that folder, so sources are never cluttered with generated files.
- **Robust batch behaviour.** One broken file does not abort the run; every failure is reported with both engine errors at the end, and the exit code reflects it.
- **Stable output names.** Documents with the same base name from different folders get deterministic, order-independent names instead of `-2` counters.
- **Optional image extraction** into `<name>_images/` with relative links.

## Why the source format matters

Docling produces noticeably better Markdown than a PDF pipeline usually does — real heading hierarchies, multi-line code blocks, internal links. Measured on a 16 MB technical book available as both EPUB and PDF:

| Metric | from EPUB | from PDF |
| --- | --- | --- |
| Conversion time | 4.3 s | 735 s |
| Headings | 967, nested to `####` | 1062, all flat at `##` |
| Code blocks, multi-line | 56 % of 328 | 0 % of 243 |
| Internal links | 1783 | 0 |
| Hyphenation artefacts | 0 | 21 |

That is why source-format preference exists, and why PDF is treated as the last resort in the ranking while still being fully supported.

## Requirements

- Python 3.10+
- [`docling`](https://pypi.org/project/docling/) 2.x
- [`pandoc`](https://pandoc.org/installing.html) — optional, but without it there is no fallback
- `odfdo` for native ODT support in Docling:

```bash
pip install docling
pip install 'docling-slim[format-opendocument]'   # ODT, needs odfdo
```

Docling downloads its layout, table and OCR models on first PDF use. That initial run needs network access and a writable cache directory (usually `~/.cache`); plan for a long first run.

## Usage

```
usage: convert_to_markdown.py [-h] [-o OUTDIR] [-r] [-f] [--with-images]
                              [--no-prefer-epub] [--no-pandoc-fallback]
                              [--pandoc-timeout SEK]
                              [paths ...]
```

| Option | Effect |
| --- | --- |
| `paths ...` | Files and/or directories. Default: current directory. |
| `-o`, `--outdir DIR` | Write Markdown here instead of the default location. Subfolders are preserved when recursing. |
| `-r`, `--recursive` | Descend into subdirectories. |
| `-f`, `--force` | Re-convert even if a newer Markdown file exists. |
| `--with-images` | Export images as files into `<name>_images/` and link them relatively. |
| `--no-prefer-epub`, `--all-formats` | Convert every available format, not just the best one per document. |
| `--no-pandoc-fallback` | Docling only; no fallback. |
| `--pandoc-timeout SEK` | Per-invocation Pandoc time limit (default: 300). |

### Examples

```bash
# Convert everything in the current folder into ./md/
python3 convert_to_markdown.py

# Recursive conversion of a library into library/md/, with images
python3 convert_to_markdown.py library -r --with-images

# Override the destination explicitly
python3 convert_to_markdown.py library -r -o ~/markdown/architecture

# Re-convert a single file, ignoring the freshness check
python3 convert_to_markdown.py report.pdf -f

# Keep both EPUB and PDF output for side-by-side comparison
python3 convert_to_markdown.py book.epub book.pdf --all-formats
```

## Where the output goes

The destination depends on what you pass, and `-o` always wins:

| Input | Default destination |
| --- | --- |
| A directory | `<that directory>/md/` |
| Several directories | `<each directory>/md/` |
| A single file | Next to the source file |
| Anything, with `-o DIR` | `DIR` |

With `-r`, the directory structure below the input root is mirrored inside the output folder:

```
library/                  library/md/
├── report.pdf      →     ├── report.md
└── courses/              └── courses/
    └── notes.docx            └── notes.md
```

Generated files therefore stay inside the output folder, and because `.md` is not an input format, repeated runs never read their own output.

## Output naming

Given a single source, the Markdown file is named after it:

```
notes.docx  ->  notes.md
```

If several sources with the same base name are converted into the same folder, the extension moves into the name so nothing is overwritten:

```
book.epub + book.pdf  ->  book.epub.md + book.pdf.md
```

When flat output from different folders would still collide, enough directory levels are prepended to make the name unique — always deterministically, never based on processing order:

```
…/A/Tests/report.docx  ->  report.Tests-A.docx.md
…/B/Tests/report.docx  ->  report.Tests-B.docx.md
```

## Conversion pipeline

```
source file
   │
   ├─ Docling (epub, pdf, docx, odt)  ──success──▶  Markdown
   │        │
   │        └─failure or unknown format
   │                 │
   ├─ Pandoc with an explicit reader   ──success──▶  Markdown   (marked "via Pandoc")
   │        │
   │        └─failure
   │                 │
   └─ reported as failed, exit code 1
```

### Supported formats

- **Docling:** `.epub`, `.pdf`, `.docx`, `.odt`
- **Pandoc fallback:** `.docx`, `.odt`, `.epub`, `.rtf`, `.html`, `.htm`, `.rst`, `.org`, `.tex`, `.latex`, `.ipynb`, `.opml`, `.fb2`, `.textile`
- **Recognised but not convertible:** `.doc`, `.ppt`, `.pptx`, `.xls`, `.xlsx`, `.ods`, `.odp`, `.mobi`, `.pages`, `.key`, `.numbers`

Those last formats are collected and reported as a group at the end of the run instead of disappearing silently. To convert them, save them as `docx` or `odt` first (LibreOffice does this well) and run again.

`.md` files are deliberately not treated as input, so a run with `-o md/` never re-reads its own output.

## Exit codes

- `0` — everything converted or skipped as up to date.
- `1` — at least one file failed, or unconvertible files were found in the input.

A failed file produces no Markdown output, so a failure never leaves a plausible-looking file behind.

## Notes on correctness

A few behaviours are deliberate rather than incidental:

**Pandoc always gets an explicit reader (`-f`).** Without it, Pandoc guesses from the extension, and for formats it does not know it silently reads the bytes as Markdown — producing a large, "successful", completely useless output. The script therefore maps every supported suffix to a fixed reader, and additionally checks that the result is actually text before accepting it. Binary garbage is deleted and reported as a failure.

**Relative image links.** Docling writes image paths exactly as the output directory was passed, often absolute. Those break as soon as the folder moves, so the script rewrites them relative to the Markdown file.

**Partly converted documents are not retried.** If Docling reports `partial_success`, the partially recognised pages are kept rather than replaced by a second, differently-flawed run. A run that lost pages is reported as `~` with the underlying errors shown.

**Same base name does not mean same document.** Two files named alike can be different revisions. They keep the same ranking position, so both are converted — and the duplicate-conversion guard never silently drops the second one on the assumption that they are identical.

## Limitations

- No fallback for `.doc`, `.xls`, `.ppt`, `.pptx`, `.xlsx`, `.ods`, `.mobi`: Pandoc does not read them. Convert to `docx`/`odt` first.
- EPUB images are not exported; Docling keeps `<!-- image -->` placeholders there.
- PDF conversion is CPU-heavy and slow: a 16 MB technical book took about 12 minutes on a laptop CPU. Run large batches in the background.
- Text layer quality is inherited from the source PDF; OCR errors in the original stay in the output.
- Scanned PDFs without a text layer depend on OCR models being available.

## See also

Markdown Knowledge Indexer & Search Engine 
[md_indexer-searcher](https://github.com/Zheng-Bote/md_indexer-searcher)


