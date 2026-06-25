#!/usr/bin/env python3
"""Find DOIs for bibliography references via the Crossref API.

Single mode: one reference, passed as an argument or on stdin.
Batch mode (--batch): a whole bibliography, split into individual references.

Each reference is sent to Crossref's bibliographic-match endpoint and the
best-scoring work is returned, with a confidence flag based on how well the
matched title overlaps the input (to catch poor matches from messy PDF text).

Crossref's REST API is free and needs no token. Supplying a contact email puts
you in the faster "polite pool" (https://api.crossref.org/swagger-ui/index.html).
"""

import argparse
import re
import sys
import time

import requests

CROSSREF_URL = "https://api.crossref.org/works"
# Used for the Crossref "polite pool" — identifies us as a well-behaved client.
CONTACT_EMAIL = "your-email@example.com"
USER_AGENT = f"doi-finder/0.1 (mailto:{CONTACT_EMAIL})"

# A reference entry starts with an author surname: a capitalized word, a comma,
# then a capitalized given name (e.g. "Appel, Hannah"). Continuation lines from
# PDF wrapping typically start mid-sentence ("and ...", "telism, ...") and do
# not match.
ENTRY_START = re.compile(r"^[A-Z][^\s,]+,\s+[A-Z]")

# Below this title/reference word overlap, the match is flagged for review.
CONFIDENCE_THRESHOLD = 0.6


def normalize_reference(text: str) -> str:
    """Collapse a possibly line-wrapped reference into a single clean line.

    Handles hyphenated word breaks across lines (e.g. "Clien-\\ntelism" ->
    "Clientelism") and collapses remaining whitespace.
    """
    # Join words split by a hyphen at a line break.
    text = re.sub(r"-\s*\n\s*", "", text)
    # Collapse all remaining whitespace (incl. newlines) to single spaces.
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def split_references(text: str) -> list[str]:
    """Split a bibliography blob into individual (still multi-line) references.

    Prefers blank-line separation when present (most reliable). Otherwise falls
    back to starting a new entry at each author-surname line.
    """
    text = text.strip()
    if not text:
        return []
    # Blank lines between entries are the strongest signal — use them if present.
    if re.search(r"\n[ \t]*\n", text):
        chunks = re.split(r"\n[ \t]*\n", text)
        return [c.strip() for c in chunks if c.strip()]
    # Heuristic: a new entry begins at each author-surname line.
    entries: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if current and ENTRY_START.match(line):
            entries.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        entries.append("\n".join(current))
    return entries


def find_doi(reference: str, rows: int = 5, timeout: int = 30) -> dict | None:
    """Query Crossref for a reference and return the best-matching work, or None."""
    params = {
        "query.bibliographic": reference,
        "rows": rows,
        "select": "DOI,title,author,issued,container-title,score",
        "mailto": CONTACT_EMAIL,
    }
    resp = requests.get(
        CROSSREF_URL,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    items = resp.json().get("message", {}).get("items", [])
    return items[0] if items else None


def title_confidence(reference: str, work: dict) -> float:
    """Fraction of significant title words that appear in the input reference.

    A cheap sanity check: Crossref always returns *something*, but a real match
    should share most of its title words with what we searched for. Robust to
    the word-order and punctuation noise typical of PDF paste.
    """
    title = (work.get("title") or [""])[0].lower()
    title_words = {w for w in re.findall(r"[a-z0-9]+", title) if len(w) > 3}
    if not title_words:
        return 0.0
    ref_words = set(re.findall(r"[a-z0-9]+", reference.lower()))
    hits = sum(1 for w in title_words if w in ref_words)
    return hits / len(title_words)


def format_work(work: dict) -> str:
    """Render a Crossref work item for human inspection (single mode)."""
    title = (work.get("title") or ["(no title)"])[0]
    doi = work.get("DOI", "(no DOI)")
    score = work.get("score", 0)

    authors = work.get("author") or []
    author_str = ", ".join(
        " ".join(filter(None, [a.get("given"), a.get("family")])) for a in authors
    ) or "(no authors)"

    year = ""
    issued = work.get("issued", {}).get("date-parts", [[None]])
    if issued and issued[0] and issued[0][0]:
        year = str(issued[0][0])

    container = (work.get("container-title") or [""])[0]

    lines = [
        f"  Title:     {title}",
        f"  Authors:   {author_str}",
        f"  Year:      {year}",
        f"  Published: {container}" if container else None,
        f"  Score:     {score:.1f}",
        f"  DOI:       {doi}",
        f"  URL:       https://doi.org/{doi}",
    ]
    return "\n".join(line for line in lines if line)


def lookup(reference: str) -> dict | None:
    """Normalize and look up a reference, returning the best Crossref work."""
    return find_doi(reference)


def run_single(raw: str) -> int:
    reference = normalize_reference(raw)
    if not reference:
        print("No reference provided.", file=sys.stderr)
        return 2

    print(f"Looking up:\n  {reference}\n")
    try:
        work = lookup(reference)
    except requests.RequestException as exc:
        print(f"Crossref request failed: {exc}", file=sys.stderr)
        return 1

    if work is None:
        print("No match found.")
        return 1

    conf = title_confidence(reference, work)
    print("Best match:")
    print(format_work(work))
    if conf < CONFIDENCE_THRESHOLD:
        print(f"\n  ⚠ Low confidence (title overlap {conf:.0%}) — verify this match.")
    return 0


def run_batch(raw: str, delay: float = 0.2) -> int:
    references = [normalize_reference(r) for r in split_references(raw)]
    references = [r for r in references if r]
    if not references:
        print("No references provided.", file=sys.stderr)
        return 2

    print(f"Found {len(references)} reference(s).\n")
    flagged = 0
    for i, reference in enumerate(references, 1):
        try:
            work = lookup(reference)
        except requests.RequestException as exc:
            print(f"[{i}] ERROR  {exc}")
            print(f"    {reference[:90]}\n")
            flagged += 1
            continue

        if work is None:
            print(f"[{i}] NONE   no match")
            print(f"    {reference[:90]}\n")
            flagged += 1
            continue

        conf = title_confidence(reference, work)
        score = work.get("score", 0)
        title = (work.get("title") or ["(no title)"])[0]
        doi = work.get("DOI", "(no DOI)")
        status = "OK   " if conf >= CONFIDENCE_THRESHOLD else "CHECK"
        if status.strip() != "OK":
            flagged += 1
        doi_url = f"https://doi.org/{doi}" if doi != "(no DOI)" else doi
        print(f"[{i}] {status}  conf {conf:.0%}  score {score:.0f}")
        print(f"    in:  {reference[:90]}")
        print(f"    hit: {title[:90]}")
        print(f"    doi: {doi_url}\n")

        if delay and i < len(references):
            time.sleep(delay)

    print(f"Done: {len(references)} reference(s), {flagged} need review.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Look up DOIs for bibliography references via Crossref."
    )
    parser.add_argument(
        "reference",
        nargs="*",
        help="Reference text. If omitted, read from stdin.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Treat the input as a whole bibliography (multiple references).",
    )
    parser.add_argument(
        "-f",
        "--file",
        help="Read the bibliography/reference from this file instead of stdin.",
    )
    args = parser.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            raw = fh.read()
    elif args.reference:
        raw = " ".join(args.reference)
    else:
        raw = sys.stdin.read()

    return run_batch(raw) if args.batch else run_single(raw)


if __name__ == "__main__":
    raise SystemExit(main())
