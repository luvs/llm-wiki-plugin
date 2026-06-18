#!/usr/bin/env python3
"""
wiki_ingest_queue.py — List raw sources that still need to be ingested.

The ingest queue is the set of raw sources that have not yet been compiled
into the wiki. Two detection rules, by file kind:

  - Markdown sources (.md / .markdown / .mdx) carry a `processed:` flag in
    their YAML frontmatter. `processed: true` means already ingested and is
    skipped. `processed: false` — OR a markdown file with no `processed` key
    at all — is pending. (A missing flag counts as unprocessed by design, so
    nothing slips through unnoticed; the ingest workflow sets the flag after.)

  - Everything else (PDFs, .txt transcripts, .html, images, ...) cannot carry
    a frontmatter flag reliably, so "processed" is inferred from the wiki: a
    source is considered ingested once some page under `wiki/sources/`
    references it via its `raw:` frontmatter field. Not referenced → pending.
    (If such a file does happen to carry a frontmatter `processed:` flag, the
    explicit flag wins.)

The script never reads binary file contents — for non-text files it only needs
the path and the reference check. It has no third-party dependencies (the
frontmatter parser is the same minimal one used by wiki_lint.py).

Usage:
    python wiki_ingest_queue.py <project-root> [--wiki-dir wiki] [--raw-dir raw]
                                [--json] [--include-processed]

Examples:
    python wiki_ingest_queue.py .
    python wiki_ingest_queue.py . --json
    python wiki_ingest_queue.py ~/research --wiki-dir kb --raw-dir sources

Exit status is 0 whether or not the queue is empty; a non-existent raw
directory is reported and exits 1.
"""

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Extensions whose "processed" state is governed by the frontmatter flag.
# A markdown file with no flag still counts as pending (see module docstring).
MARKDOWN_EXTS = {".md", ".markdown", ".mdx"}
# Extensions we will read to look for an (optional) frontmatter `processed:`
# flag. Anything outside this set is treated as binary and never read.
TEXT_EXTS = MARKDOWN_EXTS | {".txt", ".text", ".rst", ".org", ".html", ".htm"}

# Values that mean "already processed" for the `processed:` flag.
TRUE_VALUES = {"true", "yes", "1", "on", "done"}

# Raw-dir entries that are never sources in their own right.
SKIP_RAW_DIRS = {"assets"}


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML-frontmatter parser (mirrors wiki_lint.py). Returns a dict
    of scalar/list values, or {} when there is no frontmatter block."""
    if not text.startswith("---"):
        return {}
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    meta = {}
    current_key = None
    for line in m.group(1).split("\n"):
        if not line.strip():
            continue
        kv = re.match(r"^([a-zA-Z_][\w-]*):\s*(.*)$", line)
        if kv:
            key, value = kv.group(1), kv.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                meta[key] = [x.strip().strip('"').strip("'") for x in value[1:-1].split(",") if x.strip()]
                current_key = None
            elif value:
                meta[key] = value.strip('"').strip("'")
                current_key = None
            else:
                meta[key] = []
                current_key = key
        elif line.startswith("  - ") and current_key:
            meta[current_key].append(line[4:].strip().strip('"').strip("'"))
    return meta


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def collect_referenced_raw(sources_dir: Path) -> tuple[set, set]:
    """Scan source pages for `raw:` frontmatter pointing at raw files.

    Returns (referenced_values, referenced_basenames). `referenced_values`
    holds the paths exactly as written (e.g. "raw/attention-paper.pdf");
    `referenced_basenames` holds just the filenames (e.g. "attention-paper.pdf")
    so a raw file matches regardless of how the path prefix was written.
    """
    values: set[str] = set()
    basenames: set[str] = set()
    if not sources_dir.is_dir():
        return values, basenames
    for md_path in sources_dir.rglob("*.md"):
        if md_path.name.startswith("."):
            continue
        text = read_text(md_path)
        if text is None:
            continue
        meta = parse_frontmatter(text)
        raw_field = meta.get("raw")
        if not raw_field:
            continue
        refs = raw_field if isinstance(raw_field, list) else [raw_field]
        for ref in refs:
            ref = str(ref).strip()
            if not ref:
                continue
            values.add(ref)
            basenames.add(PurePosixPath(ref).name)
    return values, basenames


def classify(raw_file: Path, raw_root: Path, raw_dir_name: str,
             ref_values: set, ref_basenames: set) -> dict:
    """Decide whether a single raw file is pending, and why."""
    rel_to_raw = PurePosixPath(raw_file.relative_to(raw_root).as_posix())
    rel_to_project = f"{raw_dir_name}/{rel_to_raw}"
    ext = raw_file.suffix.lower()

    item = {
        "path": rel_to_project,
        "name": raw_file.name,
        "kind": "markdown" if ext in MARKDOWN_EXTS else "other",
    }

    referenced_by = None
    is_referenced = (
        raw_file.name in ref_basenames
        or rel_to_project in ref_values
        or str(rel_to_raw) in ref_values
    )

    # 1. Explicit frontmatter flag always wins, for any text-readable file.
    if ext in TEXT_EXTS:
        meta = parse_frontmatter(read_text(raw_file) or "")
        if "processed" in meta:
            flag = str(meta["processed"]).strip().lower()
            if flag in TRUE_VALUES:
                return {**item, "pending": False, "reason": f"processed: {flag}"}
            return {**item, "pending": True, "reason": f"processed: {flag or 'false'}"}

    # 2. Markdown without a flag is pending by design. Annotate if a source
    #    page already references it — likely already ingested before the flag
    #    convention, so the right fix is to set processed: true, not re-ingest.
    if ext in MARKDOWN_EXTS:
        item["reason"] = "no processed flag"
        if is_referenced:
            item["already_referenced"] = True
            item["reason"] = "no processed flag (already referenced by a source page)"
        return {**item, "pending": True}

    # 3. Everything else: presence of a referencing source page is the marker.
    if is_referenced:
        return {**item, "pending": False, "reason": "referenced by a source page"}
    return {**item, "pending": True, "reason": "not referenced in wiki/sources/"}


def build_queue(project_root: Path, wiki_dir: str, raw_dir: str) -> dict:
    raw_root = project_root / raw_dir
    sources_dir = project_root / wiki_dir / "sources"

    ref_values, ref_basenames = collect_referenced_raw(sources_dir)

    items = []
    for f in sorted(raw_root.rglob("*"), key=lambda p: p.as_posix()):
        if not f.is_file() or f.name.startswith("."):
            continue
        rel_parts = f.relative_to(raw_root).parts
        if rel_parts and rel_parts[0] in SKIP_RAW_DIRS:
            continue
        items.append(classify(f, raw_root, raw_dir, ref_values, ref_basenames))

    pending = [i for i in items if i["pending"]]
    processed = [i for i in items if not i["pending"]]
    for i in pending:
        i.pop("pending", None)
    for i in processed:
        i.pop("pending", None)

    return {
        "project_root": str(project_root),
        "wiki_dir": wiki_dir,
        "raw_dir": raw_dir,
        "pending": pending,
        "processed": processed,
        "summary": {"pending": len(pending), "processed": len(processed), "total": len(items)},
    }


def render_text(queue: dict, include_processed: bool) -> str:
    lines = []
    pending = queue["pending"]
    lines.append(f"Ingest queue for {queue['project_root']}  ({queue['raw_dir']}/ → {queue['wiki_dir']}/sources/)")
    lines.append("")
    if not pending:
        lines.append("Nothing pending — every raw source is processed. ✔")
    else:
        lines.append(f"Pending ({len(pending)}):")
        width = max(len(i["path"]) for i in pending)
        for n, i in enumerate(pending, 1):
            lines.append(f"  [{n}] {i['path']:<{width}}  {i['reason']}")
    if include_processed and queue["processed"]:
        lines.append("")
        lines.append(f"Already processed ({len(queue['processed'])}):")
        for i in queue["processed"]:
            lines.append(f"  - {i['path']}  ({i['reason']})")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("project_root", type=Path, help="Project root directory.")
    parser.add_argument("--wiki-dir", default="wiki", help="Wiki subdirectory (default: wiki).")
    parser.add_argument("--raw-dir", default="raw", help="Raw sources subdirectory (default: raw).")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument("--include-processed", action="store_true",
                        help="Also list already-processed sources (text mode).")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    raw_root = project_root / args.raw_dir
    if not raw_root.is_dir():
        print(f"Error: raw directory not found: {raw_root}", file=sys.stderr)
        print("Run /wiki:init first, or pass --raw-dir.", file=sys.stderr)
        sys.exit(1)

    queue = build_queue(project_root, args.wiki_dir, args.raw_dir)

    if args.json:
        print(json.dumps(queue, indent=2))
    else:
        print(render_text(queue, args.include_processed))


if __name__ == "__main__":
    main()
