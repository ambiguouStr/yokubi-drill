"""Build a yokubi-drill corpus from an Anki collection.

This is the only file in the project that knows Anki exists. It runs once, reads
the collection read-only with the standard library's sqlite3 — no `anki` package
— and writes a JSONL corpus. After that the drill reads the file and nothing
here is touched again.

Keeping it separate is the point: a corpus can come from anywhere, and the tool
should not grow a dependency on whichever source happened to be first.
"""
import datetime
import json
import os
import re
import sqlite3
import sys

CORPUS_VERSION = 1
ANKI = os.path.expanduser(os.environ.get("ANKI_CLI_DIR") or "~/.local/share/anki-cli")
DIM, RESET = "\x1b[2m", "\x1b[0m"


def strip_html(s):
    s = re.sub(r"\[sound:[^\]]*\]", "", s)
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").strip()


# Field names differ per deck; these are tried in order. Add your own here.
SENTENCE_FIELDS = ("Sentence", "BackSentence", "Expression", "SentKanji")
READING_FIELDS = ("Sentence Furigana", "ReadingFurigana", "Reading", "SentFurigana")
GLOSS_FIELDS = ("Sentence Meaning", "Translation", "Meaning", "SentEng")
AUDIO_FIELDS = ("Sentence Audio", "AudioFile", "Audio", "SentAudio")


def import_anki(out_path):
    """Read an Anki collection once and write a corpus file. This is the only
    code here that knows Anki exists; the drill reads the file, not the deck.

    Opened read-only. Audio is recorded as a bare filename plus a media root in
    _meta, so the corpus stays portable even though the files do not."""
    collection = os.path.join(ANKI, "collection.anki2")
    media = os.path.join(ANKI, "collection.media")
    if not os.path.exists(collection):
        sys.exit(f"no collection at {collection}\nset ANKI_CLI_DIR to point at one")
    con = sqlite3.connect(f"file:{collection}?mode=ro", uri=True)
    # Anki's schema declares this collation; without it even a SELECT fails
    con.create_collation("unicase", lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()))
    try:
        con.execute("select 1 from notes limit 1").fetchone()
    except sqlite3.OperationalError:
        sys.exit("the collection is locked — quit Anki or `ank` and try again")
    q = lambda s: con.execute(s).fetchall()
    rows, skipped = [], []
    for ntid, name in q("select id, name from notetypes"):
        fields = [r[0] for r in q(f"select name from fields where ntid={ntid} order by ord")]
        idx = {f: i for i, f in enumerate(fields)}
        pick = lambda names: next((idx[f] for f in names if f in idx), None)
        sent, furi, gloss, audio = (pick(SENTENCE_FIELDS), pick(READING_FIELDS),
                                    pick(GLOSS_FIELDS), pick(AUDIO_FIELDS))
        n_notes = q(f"select count(*) from notes where mid={ntid}")[0][0]
        if sent is None:
            if n_notes:
                skipped.append((name, n_notes, fields))
            continue
        for (flds,) in q(f"select flds from notes where mid={ntid}"):
            p = flds.split("\x1f")
            text = strip_html(p[sent])
            if not text:
                continue
            sound = ""
            if audio is not None:
                m = re.search(r"\[sound:([^\]]+)\]", p[audio])
                if m:
                    sound = m.group(1)
            rows.append({"text": text, "source": name,
                         "reading": strip_html(p[furi]) if furi is not None else "",
                         "gloss": strip_html(p[gloss]) if gloss is not None else "",
                         "audio": sound})
    con.close()
    write_corpus(rows, out_path, media=media, source=f"anki:{collection}")
    with_audio = sum(1 for r in rows if r["audio"])
    print(f"wrote {len(rows)} sentences to {out_path}  ({with_audio} with audio)")
    by_src = {}
    for r in rows:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    for k, v in sorted(by_src.items(), key=lambda x: -x[1]):
        print(f"   {v:>6}  {k}")
    for name, n, fields in skipped:
        print(f"{DIM}   skipped {n} notes of {name!r}: no sentence field among "
              f"{', '.join(fields[:5])}{RESET}")
    print(f"\nFrom now on the drill reads this file. Re-run --import-anki after "
          f"adding decks.")


def write_corpus(rows, path, media="", source=""):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    meta = {"_meta": {"version": CORPUS_VERSION, "media": media, "source": source,
                      "count": len(rows),
                      "built_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "corpus.jsonl"
    import_anki(out)
