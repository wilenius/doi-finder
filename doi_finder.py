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
CONTACT_EMAIL = "hw@iki.fi"
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


def find_doi(reference: str, rows: int = 5, timeout: int = 30) -> list[dict]:
    """Query Crossref for a reference and return the candidate works (best first).

    Returns Crossref's own relevance order; callers re-rank with pick_best.
    """
    params = {
        "query.bibliographic": reference,
        "rows": rows,
        "select": "DOI,title,author,issued,container-title,score,type",
        "mailto": CONTACT_EMAIL,
    }
    resp = requests.get(
        CROSSREF_URL,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("message", {}).get("items", [])


def _words(text: str) -> set[str]:
    """Lowercased word tokens, keeping accented letters (e.g. "Lähteenaho") whole."""
    return set(re.findall(r"\w+", text.lower()))


def title_confidence(reference: str, work: dict) -> float:
    """Fraction of significant title words that appear in the input reference.

    A cheap sanity check: Crossref always returns *something*, but a real match
    should share most of its title words with what we searched for. Robust to
    the word-order and punctuation noise typical of PDF paste.
    """
    title_words = {w for w in _words((work.get("title") or [""])[0]) if len(w) > 3}
    if not title_words:
        return 0.0
    ref_words = _words(reference)
    hits = sum(1 for w in title_words if w in ref_words)
    return hits / len(title_words)


def author_overlap(reference: str, work: dict) -> float | None:
    """Fraction of the record's author family names that appear in the reference.

    Discriminates a work from things that merely *mention* it: a book review's
    title embeds the book's title and authors (high title overlap), but the
    review's own author is absent from the cited reference. Returns None when the
    record lists no authors (no signal to offer).
    """
    ref_words = _words(reference)
    families = []
    for a in work.get("author") or []:
        # Family names can be multi-word ("Soto Bermant"); match on any token.
        toks = [t for t in re.findall(r"\w+", (a.get("family") or "").lower()) if len(t) > 2]
        if toks:
            families.append(toks)
    if not families:
        return None
    hits = sum(1 for toks in families if any(t in ref_words for t in toks))
    return hits / len(families)


def year_match(reference: str, work: dict) -> float | None:
    """1.0 if the record's year is cited in the reference, 0.0 if it contradicts it.

    Returns None when either side lacks a year. Catches reviews/reprints whose
    year differs from the edition actually cited.
    """
    ref_years = set(re.findall(r"\b(?:1[5-9]\d\d|20\d\d)\b", reference))
    parts = work.get("issued", {}).get("date-parts", [[None]])
    work_year = parts[0][0] if parts and parts[0] else None
    if not ref_years or work_year is None:
        return None
    return 1.0 if str(work_year) in ref_years else 0.0


def match_score(reference: str, work: dict) -> float:
    """Combined 0..1 confidence weighing title, author and year agreement.

    Missing signals (no authors / no year) are dropped and the rest renormalized,
    so records are judged only on the evidence they actually provide.
    """
    parts = [(title_confidence(reference, work), 2.0)]
    au = author_overlap(reference, work)
    if au is not None:
        parts.append((au, 2.0))
    yr = year_match(reference, work)
    if yr is not None:
        parts.append((yr, 1.0))
    return sum(v * w for v, w in parts) / sum(w for _, w in parts)


def pick_best(reference: str, works: list[dict]) -> dict | None:
    """Choose the candidate that best agrees with the reference.

    Re-ranks Crossref's results by match_score so a candidate whose authors or
    year contradict the reference can't win on title overlap alone (e.g. a book
    review beating the actual book). Ties keep Crossref's original ordering.
    """
    best, best_key = None, None
    for rank, work in enumerate(works):
        key = (match_score(reference, work), -rank)
        if best_key is None or key > best_key:
            best, best_key = work, key
    return best


def format_work(work: dict) -> str:
    """Render a Crossref work item for human inspection (single mode)."""
    title = (work.get("title") or ["(no title)"])[0]
    doi = work.get("DOI", "(no DOI)")
    score = work.get("score", 0)

    authors = work.get("author") or []
    author_str = (
        ", ".join(
            " ".join(filter(None, [a.get("given"), a.get("family")])) for a in authors
        )
        or "(no authors)"
    )

    year = ""
    issued = work.get("issued", {}).get("date-parts", [[None]])
    if issued and issued[0] and issued[0][0]:
        year = str(issued[0][0])

    container = (work.get("container-title") or [""])[0]
    work_type = work.get("type", "")

    lines = [
        f"  Title:     {title}",
        f"  Authors:   {author_str}",
        f"  Year:      {year}",
        f"  Type:      {work_type}" if work_type else None,
        f"  Published: {container}" if container else None,
        f"  Score:     {score:.1f}",
        f"  DOI:       {doi}",
        f"  URL:       https://doi.org/{doi}",
    ]
    return "\n".join(line for line in lines if line)


def lookup(reference: str) -> dict | None:
    """Look up a reference and return the best-agreeing Crossref work, or None."""
    return pick_best(reference, find_doi(reference))


def review_notes(reference: str, work: dict) -> list[str]:
    """Reasons (if any) to manually verify a match, for the confidence warning."""
    notes = []
    t = title_confidence(reference, work)
    if t < CONFIDENCE_THRESHOLD:
        notes.append(f"title overlap {t:.0%}")
    au = author_overlap(reference, work)
    if au is not None and au < 0.5:
        notes.append(f"author overlap {au:.0%}")
    if year_match(reference, work) == 0.0:
        notes.append("year differs from reference")
    return notes


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

    print("Best match:")
    print(format_work(work))
    notes = review_notes(reference, work)
    if notes:
        print(f"\n  ⚠ Low confidence ({'; '.join(notes)}) — verify this match.")
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

        notes = review_notes(reference, work)
        conf = match_score(reference, work)
        score = work.get("score", 0)
        title = (work.get("title") or ["(no title)"])[0]
        doi = work.get("DOI", "(no DOI)")
        status = "CHECK" if notes else "OK   "
        if notes:
            flagged += 1
        doi_url = f"https://doi.org/{doi}" if doi != "(no DOI)" else doi
        print(f"[{i}] {status}  conf {conf:.0%}  score {score:.0f}")
        print(f"    in:  {reference[:90]}")
        print(f"    hit: {title[:90]}")
        if notes:
            print(f"    why: {'; '.join(notes)}")
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
