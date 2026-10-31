#!/usr/bin/env python3
"""Konvertiert Dokumente mit Docling nach Markdown, mit Pandoc als Fallback.

Primär übernimmt Docling (*.epub, *.pdf, *.docx, *.odt). Schlägt die
Konvertierung fehl oder kann Docling den Dateityp gar nicht lesen, springt
Pandoc ein (--no-pandoc-fallback schaltet das ab).

Beispiele:
    python3 convert_to_markdown.py                  # aktuelles Verzeichnis -> ./md/
    python3 convert_to_markdown.py buch.epub         # einzelne Datei -> daneben
    python3 convert_to_markdown.py buecher -r        # rekursiv -> buecher/md/

Liegt neben einer PDF-Datei eine EPUB-Datei mit gleichem Basisnamen, wird nur
die EPUB konvertiert: EPUB liefert die bessere Markdown-Qualität (echte
Überschriftenhierarchie, mehrzeilige Code-Blöcke, interne Links). Mit
--no-prefer-epub lässt sich das abschalten.

Zielverzeichnis: Bei einem Verzeichnis als Eingabe landen die Markdown-Dateien
in <Verzeichnis>/md/ (Unterordner bleiben erhalten), bei einer einzelnen Datei
neben der Quelle. -o überschreibt beides. Bereits vorhandene, aktuellere
Markdown-Dateien werden übersprungen (--force erzwingt eine Neukonvertierung).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import ConversionStatus
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc.base import ImageRefMode

DOCLING_SUFFIXES = (".epub", ".pdf", ".docx", ".odt")

# Pandoc-Reader je Endung. Der Reader wird IMMER explizit mit -f übergeben:
# ohne -f rät Pandoc und liest unbekannte Binärdateien stillschweigend als
# Markdown – das erzeugt "erfolgreiche" Ausgaben voller Binary-Müll.
PANDOC_READERS = {
    ".docx": "docx",
    ".odt": "odt",
    ".epub": "epub",
    ".rtf": "rtf",
    ".html": "html",
    ".htm": "html",
    ".rst": "rst",
    ".org": "org",
    ".tex": "latex",
    ".latex": "latex",
    ".ipynb": "ipynb",
    ".opml": "opml",
    ".fb2": "fb2",
    ".textile": "textile",
}

# Dokumenttypen, die weder Docling noch Pandoc lesen. Sie werden erkannt und
# gesammelt gemeldet, statt stillschweigend zu verschwinden.
KNOWN_UNSUPPORTED = (
    ".doc",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".ods",
    ".odp",
    ".mobi",
    ".pages",
    ".key",
    ".numbers",
)

# Was ein Verzeichnisscan einsammelt. Bewusst ohne .md/.markdown, sonst würde
# ein Lauf mit -o md/ seine eigenen Ergebnisse wieder einlesen.
SCAN_SUFFIXES = tuple(DOCLING_SUFFIXES) + tuple(
    s for s in PANDOC_READERS if s not in DOCLING_SUFFIXES
)

# Rangfolge der Qualität; gleiche Zahl = gleichwertig. Stehen mehrere Formate
# desselben Dokuments nebeneinander, gewinnt das bessere, die schlechteren
# werden übersprungen. EPUB ist XML-basiert und liefert die beste
# Markdown-Struktur; DOCX und ODT sind ebenfalls strukturiert, aber
# untereinander nicht messbar unterschiedlich – sie verdrängen sich daher
# nicht gegenseitig. PDF ist die schlechteste Quelle (flache Überschriften,
# eingerissene Code-Blöcke).
FORMAT_RANK = {".epub": 0, ".docx": 1, ".odt": 1, ".pdf": 2}




@dataclass
class Job:
    source: Path
    target: Path


def collect_jobs(
    inputs: list[str],
    outdir: Path | None,
    recursive: bool,
    prefer_epub: bool = True,
    unsupported: dict[str, list[Path]] | None = None,
) -> list[Job]:
    """Sammelt Quelldateien und berechnet je Datei das Markdown-Ziel.

    Zielverzeichnis je Eingabe:
      * ``outdir`` gesetzt -> dieses Verzeichnis für alle Quellen,
      * Verzeichnis als Eingabe -> ``<Verzeichnis>/md/``,
      * einzelne Datei -> neben der Quelldatei.

    ``unsupported`` sammelt gefundene Dateien ein, die weder Docling noch
    Pandoc lesen, damit sie am Ende gesammelt gemeldet werden können.
    """
    if unsupported is None:
        unsupported = {}
    pairs: list[tuple[Path, Path, Path]] = []  # (Quelle, Wurzel, Ausgabeordner)
    seen: set[Path] = set()

    for raw in inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            # Standardziel für einen Ordner ist <Ordner>/md/.
            target_root = outdir if outdir is not None else path / "md"
            pattern = "**/*" if recursive else "*"
            candidates = [
                p
                for p in sorted(path.glob(pattern))
                if p.is_file()
                and (
                    p.suffix.lower() in SCAN_SUFFIXES
                    or p.suffix.lower() in KNOWN_UNSUPPORTED
                )
            ]
            if not candidates:
                print(f"  ! keine passenden Dateien in {path}", file=sys.stderr)
            elif outdir is None:
                print(f"  · Ausgabe nach {target_root}/", file=sys.stderr)
            for file in candidates:
                resolved = file.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                if file.suffix.lower() in KNOWN_UNSUPPORTED:
                    unsupported.setdefault(file.suffix.lower(), []).append(file)
                    continue
                pairs.append((file, path, target_root))
        elif path.is_file():
            suffix = path.suffix.lower()
            if suffix in KNOWN_UNSUPPORTED:
                unsupported.setdefault(suffix, []).append(path)
                continue
            if suffix not in SCAN_SUFFIXES:
                print(
                    f"  ! übersprungen (nicht unterstützt): {path}", file=sys.stderr
                )
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            # Einzelne Datei: neben der Quelle bleiben, außer -o sagt etwas anderes.
            pairs.append((path, path.parent, outdir if outdir is not None else path.parent))
        else:
            print(f"  ! Pfad nicht gefunden: {path}", file=sys.stderr)

    if prefer_epub:
        pairs = drop_shadowed_copies(pairs)

    # Namensvergabe: nur die tatsächlich konvertierten Quellen zählen.
    stem_counts: dict[tuple[Path, str], int] = {}
    for file, _root, _out in pairs:
        key = (file.parent.resolve(), file.stem)
        stem_counts[key] = stem_counts.get(key, 0) + 1

    jobs: list[Job] = []
    for file, root, target_root in pairs:
        ambiguous = stem_counts[(file.parent.resolve(), file.stem)] > 1
        name = target_name(file, ambiguous)
        try:
            relative = file.relative_to(root)
        except ValueError:
            relative = Path(file.name)
        jobs.append(Job(file, target_root / relative.with_name(name)))

    return disambiguate(jobs)


def drop_shadowed_copies(
    pairs: list[tuple[Path, Path, Path]],
) -> list[tuple[Path, Path, Path]]:
    """Entfernt Dateien, zu denen im selben Verzeichnis ein besseres Format liegt.

    EPUB liefert die deutlich bessere Markdown-Qualität als PDF, DOCX und ODT
    sind meist ebenfalls sauberer als PDF. Liegen z. B. buch.epub und buch.pdf
    nebeneinander, wird nur buch.epub konvertiert – die PDF wäre eine teure,
    schlechtere Zweitausgabe. Der Nachbar wird auf der Platte gesucht, auch
    wenn er selbst nicht zur Eingabemenge gehört.
    """
    kept: list[tuple[Path, Path, Path]] = []
    for triple in pairs:
        file = triple[0]
        better = find_better_twin(file)
        if better is not None:
            print(
                f"  · {file.name}: bessere Fassung vorhanden, "
                f"wird übersprungen ({better.name})",
                file=sys.stderr,
            )
            continue
        kept.append(triple)
    return kept


def find_better_twin(source: Path) -> Path | None:
    """Sucht im selben Verzeichnis eine Quelle desselben Dokuments in besserem Format."""
    rank = FORMAT_RANK.get(source.suffix.lower())
    if rank is None or rank == 0:
        return None

    for candidate in source.parent.glob(f"{glob_escape(source.stem)}.*"):
        if candidate == source or not candidate.is_file():
            continue
        candidate_rank = FORMAT_RANK.get(candidate.suffix.lower())
        if candidate_rank is not None and candidate_rank < rank:
            return candidate
    return None


def target_name(source: Path, ambiguous: bool) -> str:
    """Markdown-Dateiname für eine Quelle.

    Werden im selben Verzeichnis mehrere Quellen mit gleichem Basisnamen
    konvertiert (z. B. buch.docx und buch.odt), wandert die Endung mit in den
    Namen: buch.docx.md / buch.odt.md. Sonst hieße die Ausgabe für beide
    buch.md und die zweite Datei würde die erste überschreiben.
    """
    if ambiguous:
        return f"{source.stem}.{source.suffix.lower().lstrip('.')}.md"
    return f"{source.stem}.md"


def glob_escape(text: str) -> str:
    """Macht Sonderzeichen eines Dateinamens für ein Glob-Muster unschädlich."""
    return "".join(f"[{ch}]" if ch in "*?[" else ch for ch in text)


def disambiguate(jobs: list[Job]) -> list[Job]:
    """Sicherheitsnetz: keine zwei Quellen auf derselben Markdown-Datei.

    Nötig z. B. bei flachem -o, wenn gleichnamige Quellen aus verschiedenen
    Verzeichnissen zusammenlaufen.
    """
    by_target: dict[Path, list[Job]] = {}
    for job in jobs:
        by_target.setdefault(job.target.resolve(), []).append(job)

    for group in by_target.values():
        if len(group) < 2:
            continue
        for job, name in zip(group, _unique_names(group)):
            job.target = job.target.with_name(name)
    return jobs


def _unique_names(group: list[Job]) -> list[str]:
    """Eindeutige Dateinamen für Quellen, die auf dasselbe Ziel zeigen.

    Zuerst wird die Endung angehängt (buch.docx.md / buch.odt.md), dann so
    viele Verzeichnisebenen vorangestellt, bis die Namen eindeutig sind. Das
    Ergebnis hängt nicht von der Reihenfolge der Eingabe ab – ein Zähler wie
    "-2" wäre bei jedem Lauf anders verteilt.
    """
    for depth in range(0, 32):
        names = [_name_with_ancestry(job, depth) for job in group]
        if len(set(names)) == len(names):
            return names

    # Letzter Ausweg: identische Pfade doppelt angegeben -> durchzählen.
    unique: list[str] = []
    for job in group:
        name = _name_with_ancestry(job, 0)
        candidate, counter = name, 2
        while candidate in unique:
            stem = candidate[: -len(".md")]
            candidate = f"{stem}-{counter}.md"
            counter += 1
        unique.append(candidate)
    return unique


def _name_with_ancestry(job: Job, depth: int) -> str:
    """Dateiname mit ``depth`` Verzeichnisebenen: buch.docx.md -> buch.Dir.docx.md."""
    ext = job.source.suffix.lower().lstrip(".")
    if depth == 0:
        return f"{job.source.stem}.{ext}.md"
    parts = [p for p in job.source.resolve().parent.parts if p not in ("/", "")]
    dirs = "-".join(reversed(parts[-depth:]))
    return f"{job.source.stem}.{dirs}.{ext}.md" if dirs else f"{job.source.stem}.{ext}.md"


def check_optional_deps(jobs: list[Job], pandoc_fallback: bool = True) -> None:
    """Warnt einmal vorab, wenn für ein Format ein Extra-Paket fehlt."""
    if not any(job.source.suffix.lower() == ".odt" for job in jobs):
        return
    try:
        import odfdo  # noqa: F401
    except ImportError:
        hint = (
            "Pandoc übernimmt diese Dateien."
            if pandoc_fallback
            else "Ohne --pandoc-fallback schlagen sie fehl."
        )
        print(
            "  ! ODT-Dateien gefunden, aber das Paket 'odfdo' fehlt – Docling kann "
            f"sie nicht lesen. {hint}\n"
            "    Installation: pip install 'docling-slim[format-opendocument]'",
            file=sys.stderr,
        )


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def build_converter(with_images: bool) -> DocumentConverter:
    """Ein Konverter für alle Dateien – die Modelle werden nur einmal geladen."""
    if not with_images:
        return DocumentConverter()
    # Ohne diese Option enthalten PDF-Pictures keine Bilddaten, es gäbe also
    # nichts zu exportieren.
    pdf_options = PdfPipelineOptions()
    pdf_options.generate_picture_images = True
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)
        }
    )


def convert(
    job: Job,
    converter: DocumentConverter,
    force: bool,
    with_images: bool,
    pandoc_fallback: bool = True,
    pandoc_timeout: int = 300,
) -> str:
    """Konvertiert eine Datei.

    Rückgabe: 'ok' | 'ok-pandoc' | 'partial' | 'skipped' | 'failed'.
    Docling läuft zuerst; scheitert es, übernimmt Pandoc.
    """
    if (
        not force
        and job.target.exists()
        and job.target.stat().st_mtime >= job.source.stat().st_mtime
    ):
        return "skipped"

    suffix = job.source.suffix.lower()
    job.target.parent.mkdir(parents=True, exist_ok=True)

    docling_error: str | None = None
    if suffix in DOCLING_SUFFIXES:
        outcome, docling_error = _convert_with_docling(job, converter, with_images)
        if outcome is not None:
            return outcome
        print(
            f"  ! {job.source.name}: Docling gescheitert ({_short(docling_error)})",
            file=sys.stderr,
        )
    else:
        docling_error = f"Format {suffix} wird von Docling nicht unterstützt"

    if not pandoc_fallback:
        print(f"  ✗ {job.source.name}: {_short(docling_error)}", file=sys.stderr)
        return "failed"

    if not pandoc_available():
        print(
            f"  ✗ {job.source.name}: {_short(docling_error)}; Pandoc ist nicht installiert",
            file=sys.stderr,
        )
        return "failed"

    pandoc_error = _convert_with_pandoc(job, with_images, pandoc_timeout)
    if pandoc_error is None:
        return "ok-pandoc"

    print(
        f"  ✗ {job.source.name}: Docling: {_short(docling_error)} "
        f"| Pandoc: {_short(pandoc_error)}",
        file=sys.stderr,
    )
    return "failed"


def _short(text: str, limit: int = 160) -> str:
    """Kürzt Fehlertexte auf eine lesbare Zeile."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def pandoc_available() -> bool:
    return shutil.which("pandoc") is not None


def _convert_with_docling(
    job: Job, converter: DocumentConverter, with_images: bool
) -> tuple[str | None, str]:
    """Docling-Versuch. Rückgabe: (Ergebnis oder None bei Fehler, Fehlertext)."""
    started = time.monotonic()
    try:
        result = converter.convert(job.source)
    except Exception as exc:  # noqa: BLE001 - eine Datei darf den Lauf nicht abbrechen
        return None, f"{type(exc).__name__}: {exc}"

    elapsed = time.monotonic() - started
    if (
        result.status in (ConversionStatus.FAILURE, ConversionStatus.SKIPPED)
        or not result.document
    ):
        detail = result.errors[:1] if result.errors else ""
        return None, f"Status {result.status.value} {detail}".strip()

    if with_images:
        image_dir = job.target.parent / f"{job.target.stem}_images"
        image_dir.mkdir(parents=True, exist_ok=True)
        markdown = result.document.export_to_markdown(
            image_mode=ImageRefMode.REFERENCED,
            image_dir=image_dir,
        )
        if not any(image_dir.iterdir()):
            # z. B. EPUB: Bildreferenzen bleiben als <!-- image --> Platzhalter.
            image_dir.rmdir()
            print("    Hinweis: keine exportierbaren Bilddaten, Platzhalter belassen")
        else:
            markdown = _relativize_image_links(
                markdown, image_dir, job.target.parent
            )
        job.target.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    else:
        markdown = result.document.export_to_markdown()
        job.target.write_text(markdown.rstrip() + "\n", encoding="utf-8")

    outcome = "partial" if result.status == ConversionStatus.PARTIAL_SUCCESS else "ok"
    marker = "~" if outcome == "partial" else "✓"
    print(
        f"  {marker} {job.source.name} -> {job.target} "
        f"({human_size(job.target.stat().st_size)}, {elapsed:.1f}s)"
    )
    if outcome == "partial" and result.errors:
        print(f"    Hinweis: Teilfehler: {result.errors[:2]}", file=sys.stderr)
    return outcome, ""


def _convert_with_pandoc(
    job: Job, with_images: bool, timeout: int
) -> str | None:
    """Pandoc-Fallback. Rückgabe: None bei Erfolg, sonst der Fehlertext.

    Pandoc ist der letzte Versuch: schreibt es keine plausible Ausgabe, gilt die
    Datei als fehlgeschlagen. Ein leeres oder binäres Ergebnis wäre schlimmer
    als eine ehrliche Meldung.
    """
    reader = PANDOC_READERS.get(job.source.suffix.lower())
    if reader is None:
        return f"Pandoc unterstützt {job.source.suffix.lower()} nicht"

    # Arbeitsverzeichnis ist das Quellverzeichnis: nur so löst Pandoc relative
    # Bild- und Ressourcenverweise der Eingabe auf. Alle Ausgabepfade werden
    # absolut übergeben und die Bildverweise danach relativ gesetzt.
    workdir = job.source.resolve().parent
    target_abs = job.target.resolve()
    image_dir = job.target.parent.resolve() / f"{job.target.stem}_images"

    cmd = [
        "pandoc",
        "-f",
        reader,          # immer explizit: ohne -f liest Pandoc Binäres als Markdown
        "-t",
        "gfm",
        "--wrap=none",
        "--resource-path",
        str(workdir),
    ]
    if with_images:
        cmd += ["--extract-media", str(image_dir)]
    cmd += [str(job.source.resolve()), "-o", str(target_abs)]

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Zeitüberschreitung nach {timeout}s"
    except OSError as exc:
        return f"Start fehlgeschlagen: {exc}"

    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip().splitlines()
        return f"Exit {proc.returncode}: {message[0] if message else 'ohne Meldung'}"

    if not job.target.exists():
        return "keine Ausgabe erzeugt"

    _normalize_pandoc_output(job.target, with_images)
    if not _looks_like_text(job.target.read_text(encoding="utf-8", errors="replace")):
        job.target.unlink(missing_ok=True)
        return "Ausgabe ist kein Text (Binärmüll)"

    if with_images:
        if _has_images(image_dir):
            markdown = _relativize_image_links(
                job.target.read_text(encoding="utf-8"), image_dir, job.target.parent
            )
            job.target.write_text(markdown, encoding="utf-8")
        elif image_dir.exists():
            shutil.rmtree(image_dir, ignore_errors=True)

    print(
        f"  ✓ {job.source.name} -> {job.target} "
        f"({human_size(job.target.stat().st_size)}, {elapsed:.1f}s, via Pandoc)"
    )
    return None


def _has_images(image_dir: Path) -> bool:
    """Liegen in einem Extraktionsordner überhaupt Dateien?"""
    return image_dir.is_dir() and any(p.is_file() for p in image_dir.rglob("*"))


def _relativize_image_links(markdown: str, image_dir: Path, base: Path) -> str:
    """Macht Bildverweise relativ zur Markdown-Datei.

    Docling wie Pandoc schreiben die Pfade so, wie ``image_dir`` übergeben
    wurde – oft absolut. Absolute Pfade brechen, sobald der Ordner verschoben
    wird.
    """
    names = {p.name for p in image_dir.rglob("*") if p.is_file()}
    if not names:
        return markdown
    prefix = image_dir.resolve().relative_to(base.resolve()).as_posix()

    def repl(match: re.Match[str]) -> str:
        path, name = match.group(1), match.group(2)
        if name in names:
            return f"]({prefix}/{name})"
        return f"]({path})"

    return re.sub(r"\]\(([^)\s]*?/([^/)\s]+))\)", repl, markdown)


def _looks_like_text(text: str) -> bool:
    """Grobe Plausibilitätsprüfung gegen versehentlich durchgereichte Binärdaten."""
    if not text.strip():
        return True  # leere Dokumente sind legitim
    sample = text[:4000]
    printable = sum(
        1 for ch in sample if ch.isprintable() or ch in "\n\r\t"
    )
    return printable / len(sample) > 0.9


def _normalize_pandoc_output(target: Path, with_images: bool) -> None:
    """Vereinheitlicht die Pandoc-Ausgabe.

    Pandoc schreibt Bilder als rohes HTML mit Stilangaben. Wir machen daraus
    Markdown-Bildverweise, damit die Datei zum Rest der Ausgaben passt.
    """
    text = target.read_text(encoding="utf-8")
    text = re.sub(r'<img\s+src="([^"]+)"[^>]*?/?>', r"![]($1)", text)
    if not with_images:
        # Ohne Bildwünsche bleiben die Platzhalter wie bei Docling.
        text = re.sub(r"!\[\]\([^)]*\)", "<!-- image -->", text)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Konvertiert Dokumente mit Docling nach Markdown; Pandoc springt ein, "
            "wenn Docling scheitert oder ein Typ nicht unterstützt ist."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Dateien und/oder Verzeichnisse (Standard: aktuelles Verzeichnis)",
    )
    parser.add_argument(
        "-o",
        "--outdir",
        type=Path,
        help=(
            "Zielverzeichnis für die Markdown-Dateien "
            "(Standard: <Eingabeordner>/md/ bzw. neben der Quelldatei)"
        ),
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Verzeichnisse rekursiv durchsuchen",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="vorhandene Markdown-Dateien überschreiben",
    )
    parser.add_argument(
        "--with-images",
        action="store_true",
        help="Bilder als Dateien exportieren und im Markdown verlinken",
    )
    parser.add_argument(
        "--no-prefer-epub",
        "--all-formats",
        dest="prefer_epub",
        action="store_false",
        help=(
            "alle Formate konvertieren, auch wenn eine bessere Fassung "
            "(Rangfolge: epub > docx/odt > pdf) daneben liegt"
        ),
    )
    parser.add_argument(
        "--no-pandoc-fallback",
        dest="pandoc_fallback",
        action="store_false",
        help="kein Pandoc-Fallback, wenn Docling scheitert oder den Typ nicht kennt",
    )
    parser.add_argument(
        "--pandoc-timeout",
        type=int,
        default=300,
        metavar="SEK",
        help="Zeitlimit je Pandoc-Aufruf in Sekunden (Standard: 300)",
    )
    return parser.parse_args(argv)


def report_unsupported(unsupported: dict[str, list[Path]]) -> None:
    """Meldet gefundene Dateien, die kein Konverter lesen kann – gesammelt."""
    if not unsupported:
        return
    print("\nNicht konvertierbar (weder Docling noch Pandoc):", file=sys.stderr)
    for suffix, files in sorted(unsupported.items()):
        example = files[0] if len(files) == 1 else f"z. B. {files[0].name}"
        print(f"  {len(files):3d} × {suffix}: {example}", file=sys.stderr)
    formats = ", ".join(sorted(s.lstrip(".") for s in unsupported))
    print(
        f"  Hinweis: {formats} vorher nach docx/odt konvertieren "
        "(z. B. mit LibreOffice), dann erneut laufen lassen.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    outdir = args.outdir.expanduser().resolve() if args.outdir else None
    unsupported: dict[str, list[Path]] = {}
    jobs = collect_jobs(
        args.paths or ["."],
        outdir,
        args.recursive,
        prefer_epub=args.prefer_epub,
        unsupported=unsupported,
    )

    if not jobs:
        print("Keine passenden Dateien zum Konvertieren gefunden.")
        if args.prefer_epub:
            print(
                "Hinweis: Liegt ein Dokument in mehreren Formaten vor, wird nur das "
                "beste konvertiert (Rangfolge: epub > docx/odt > pdf); "
                "--all-formats erzwingt alle Formate."
            )
        report_unsupported(unsupported)
        return 0 if not unsupported else 1

    print(f"{len(jobs)} Datei(en) gefunden, starte Konvertierung …")
    check_optional_deps(jobs, args.pandoc_fallback)
    if args.pandoc_fallback and not pandoc_available():
        print(
            "  ! Pandoc ist nicht installiert – für fehlgeschlagene oder nicht "
            "unterstützte Formate gibt es keinen Fallback.",
            file=sys.stderr,
        )
    converter = build_converter(args.with_images)

    counts = {"ok": 0, "ok-pandoc": 0, "partial": 0, "skipped": 0, "failed": 0}
    failures: list[Path] = []
    for index, job in enumerate(jobs, start=1):
        print(f"[{index}/{len(jobs)}] {job.source}")
        outcome = convert(
            job,
            converter,
            args.force,
            args.with_images,
            pandoc_fallback=args.pandoc_fallback,
            pandoc_timeout=args.pandoc_timeout,
        )
        counts[outcome] += 1
        if outcome == "failed":
            failures.append(job.source)
        if outcome == "skipped":
            print(f"  · aktuell, übersprungen: {job.target}")

    summary = (
        f"\nFertig: {counts['ok']} konvertiert"
        f", {counts['ok-pandoc']} via Pandoc"
        f", {counts['partial']} teilweise"
        f", {counts['skipped']} übersprungen"
        f", {counts['failed']} fehlgeschlagen."
    )
    print(summary)
    for path in failures:
        print(f"  fehlgeschlagen: {path}", file=sys.stderr)
    report_unsupported(unsupported)
    return 1 if (failures or unsupported) else 0


if __name__ == "__main__":
    raise SystemExit(main())
