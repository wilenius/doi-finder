# doi-finder

Look up the DOI for a single bibliography reference using the [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/).

## Crossref token?

No token or registration needed — the Crossref REST API is free and public. The
only courtesy is the "polite pool": including a contact email (`mailto`) routes
you to faster, more reliable infrastructure. The email is baked into
`doi_finder.py` (`CONTACT_EMAIL`); change it to your own.

## Usage

### Single reference

```bash
# Reference as an argument
python3 doi_finder.py "Appel, Hannah C. 2012. 'Walls and White Elephants...'. Ethnography 13 (4): 439-65."

# Or piped on stdin (handles line-wrapped, hyphenated references)
printf "Aspinall, Edward and Ward Berenschot. 2019. Democracy for Sale: Clien-\ntelism..." | python3 doi_finder.py
```

### Batch (a whole bibliography)

```bash
python3 doi_finder.py --batch -f bibliography.txt
cat bibliography.txt | python3 doi_finder.py --batch
```

Each entry is reported as `OK` or `CHECK`, with a confidence percentage, the
Crossref score, the input, the matched title, and the DOI.

Requires Python 3 and the `requests` package.

## How it works

The reference is normalized (de-hyphenated, whitespace collapsed) and sent to
Crossref's `query.bibliographic` endpoint. The top-scoring work is returned with
its title, authors, year, a match `Score`, and the DOI.

In batch mode the input is first **split into individual references**, then each
is looked up.

## Handling poor-quality PDF text

Text cut from a PDF is messy in predictable ways; here is how each is handled and
where it can still fail:

- **No blank lines between entries.** If blank lines *are* present they are used
  as the separator (most reliable). Otherwise a new entry is assumed to start at
  each author-surname line — a capitalized word, comma, capitalized given name
  (`Appel, Hannah`). Continuation lines (`and Infrastructural...`, `telism,...`)
  don't match, so wrapped lines stay attached to their entry.
  *Fails when:* an author list itself wraps onto a line that happens to start
  `Surname, Given`, or an entry doesn't begin with a surname. **If splitting
  looks wrong, put a blank line between entries** — that always wins.
- **Hyphenated line breaks** (`Clien-\ntelism`) are rejoined. *Caveat:* a real
  end-of-line hyphen (`author-\ndate`) is also joined, giving `authordate`.
  Crossref's fuzzy matching usually tolerates this.
- **Smart quotes, en-dashes, ligatures** (`'`, `–`) are passed through; Crossref
  matches on words, so punctuation noise rarely matters.
- **Garbled / OCR'd words.** The `conf` (title-overlap) figure flags these: a low
  percentage means the matched title barely resembles the input — likely wrong or
  no DOI exists. Those rows are marked `CHECK`.

The bottom line: Crossref always returns *a* result, so **treat output as
suggestions to verify, not ground truth** — especially `CHECK` rows. Note that a
high `conf` is necessary but not sufficient: a book and a *review* of that book
share title words, so confident matches can still point at the wrong work type.

## Limitations (MVP)

- Tuned for the Chicago-style author-date format in the source material.
- Splitting is heuristic; blank-line-separated input is the most reliable.
