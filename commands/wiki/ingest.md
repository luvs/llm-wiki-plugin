---
description: Ingest a source into the LLM Wiki — or, with no argument, pick from the queue of unprocessed raw sources.
argument-hint: "[source-path-or-url-or-description]  (omit to choose from the ingest queue)"
---

Ingest a source into the wiki using the `llm-wiki` skill's ingest workflow.

Source: $ARGUMENTS

First, read `wiki/SCHEMA.md` if you haven't this session — it may override the conventions below. If the wiki doesn't exist yet, suggest running `/wiki:init` first.

## Pick the source to ingest

**If a source was given above** (a path, URL, or description), ingest that one — skip to "Run the ingest workflow."

**If no source was given,** discover the ingest queue and let me choose:

1. Build the queue of unprocessed raw sources:
   ```bash
   python skills/llm-wiki/scripts/wiki_ingest_queue.py . --json
   ```
   (Pass `--wiki-dir`/`--raw-dir` if this wiki uses non-default directory names — check `SCHEMA.md`.) The queue rule is:
   - **Markdown** raw files (`.md`) carry a `processed:` frontmatter flag. `processed: false` — or no `processed` key at all — means pending. `processed: true` is skipped.
   - **PDFs and other non-markdown sources** can't carry the flag reliably, so they count as pending until a page under `wiki/sources/` references them via its `raw:` frontmatter field. Unreferenced → pending.
2. If the `pending` list is empty, tell me there's nothing to ingest and stop.
3. Present the `pending` items as a numbered, interactive choice and ask me which one to process (I may pick one, several, or "all"). Show each item's `reason`. For any item flagged `already_referenced: true` (a markdown file with no flag that a source page already cites), note that it looks already-ingested and offer to just set `processed: true` on it instead of re-ingesting.
4. Ingest each source I pick, one at a time.

## Run the ingest workflow

Follow the full ingest procedure documented in the skill (`references/ingest-workflow.md`):

1. Place the raw source in `raw/` if it isn't already there, using a slugified filename. For a **markdown** source, add `processed: false` to its frontmatter so the queue tracks it.
2. Read the source. Chunk-read if it's large (over ~5000 words / a long PDF) — never load the whole thing into context if it would consume more than ~25% of the context window.
3. Briefly discuss the key takeaways with me before writing anything — what stands out, what connects to existing pages, what's surprising.
4. Survey the wiki to identify which existing pages this source touches; read each candidate to confirm.
5. Write the source-summary page in `wiki/sources/` (its `raw:` frontmatter must point at the raw file — this is what marks PDFs/other sources as processed), surgically update touched entity/concept pages with `str_replace`, create new pages for new entities/concepts (each with at least one inbound link), update the index, append one line to `log.md`.
6. If `wiki/graph/ontology.yaml` exists and the ingest added or could add typed `graph.relationships[]`: add typed edges only when the source explicitly supports them (predicate, source-page slug, evidence quote, confidence, status). Then run `python skills/llm-wiki/scripts/wiki_graph_lint.py wiki/`, triage findings with me, and run `python skills/llm-wiki/scripts/wiki_graph_extract.py wiki/`. Append a `   graph: +N nodes, +M typed edges` sub-line under the ingest entry in `log.md`.
7. **Mark the source processed.** For a **markdown** raw file, set `processed: true` in its frontmatter (add the key if it was missing) — this is the one edit the workflow makes to an otherwise-immutable raw file. For a **PDF/other** source, the `raw:` pointer on the new source page is the marker; no flag to flip.
8. Tell me what you did: pages touched, pages created, contradictions flagged, follow-ups worth investigating. If more sources remain in the queue, offer to continue with the next.
