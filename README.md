# yokubi-drill

Read a [Yokubi](https://yoku.bi) lesson in your terminal, then drill it on real
Japanese sentences — from any corpus you supply.

```
Lesson 10 · て form · 文の組み立て · 2/58 · 1/1

  次の文の ★ に入る最もよいものを、1・2・3・4から一つえらびなさい。

  庭の手入れをしようと ＿★＿＿ ＿＿＿＿ ＿＿＿＿ ＿＿＿＿ 雨が降る。

    1 限って　　2 いる　　3 日に　　4 思って

  1-4 answer · s skip · q quit
```

The drill is JLPT's 文の組み立て: four pieces of a real sentence, shuffled, and
you pick the one that belongs in ★. As on the paper, one piece is cut finer
than a phrase (a particle on its own, an auxiliary parted from its て form) and
the rest of the sentence stays around the blanks. The key is just the order the
sentence's author wrote.

## Why

Yokubi teaches the **closed grammatical system** — particles, conjugation,
clauses, the four conditionals — across 64 lessons with no exercises. That is a
deliberate choice by its author: grammar rules are learned by use, not by
flashcard. What it leaves open is where to do the using.

So bring your own sentences. Read a lesson, then work through ones that actually
contain its grammar — morpheme highlighted, audio playing, reading and gloss a
keypress away.

**Nothing is generated.** Every sentence comes from your corpus, written by a
human for a published deck or text. That constraint matters: invented grammar
examples are exactly the kind a learner cannot check.

## Install

Needs Python 3.9+ and git. No third-party packages at all — `--import-anki`
reads the collection with the standard library's `sqlite3`.

```bash
git clone https://github.com/ambiguouStr/yokubi-drill.git
cd yokubi-drill
./yokubi.py --fetch                   # clone the guide
./yokubi.py --import-anki             # build a corpus, if you use Anki
ln -s "$PWD/yokubi.py" ~/.local/bin/yokubi
```

Optional audio: `afplay` (built into macOS), `mpv`, or `ffplay`.

## The corpus

Sentences live in a JSONL file, not in Anki. One object per line, plus a leading
`_meta`:

```json
{"_meta": {"version": 1, "media": "/path/to/audio", "count": 1200}}
{"text": "この整理券は、商品受け取りの際、必要です。",
 "reading": "この 整理券[せいりけん]は、…",
 "gloss": "This ticket is required when collecting your purchase.",
 "audio": "lesson_01_line_001.mp3",
 "source": "my-sentences"}
```

Only `text` is required. `audio` is a bare filename resolved against `_meta.media`,
so moving the corpus between machines means editing one line.

`--import-anki` reads a collection **once**, read-only, and writes this file.
All the Anki-aware code lives in `importers/anki.py` and nowhere else — point
`ANKI_CLI_DIR` at nothing and the drill still runs. Write another importer the
same way: read whatever you have, emit this format. Tatoeba, subtitle files, a
textbook you typed up.

## Use

```bash
yokubi                 # list lessons, with how many sentences each has
yokubi 10              # read lesson 10, then drill it
yokubi 27 --drill      # straight to the sentences
yokubi 27 --cards      # flip through them instead of the quiz
yokubi 10 --read       # text only
yokubi --random        # a drillable lesson you have not opened
yokubi --where         # which paths are in use
```

Quiz keys: `1`–`4` answer · `s` skip · `r` replay · `q` quit. After a miss,
`a` counts it as right: Japanese word order is loose and the chunking is a
heuristic (cut where hiragana meets kanji, with no morphological analyser), so
sometimes your order is also grammatical, or a word gets split in the wrong
place. ★ is never put on a piece whose phrase could swap places with another,
but not every case can be caught.

Card keys (`--cards`): `space` reveal then advance · `r` replay · `g` which
other lessons this sentence exercises · `p` back · `q` quit.

Do not read it front to back. If you have been consuming Japanese for a while
you already use most of this grammar without names for it; skim the list and
open only the lessons whose title you cannot immediately explain.

## With an agent as tutor

The terminal quiz tells you the right order and nothing else. An agent such as
Claude Code can run the same questions in conversation and explain each miss,
so everything the quiz needs is also available without a terminal:

```bash
yokubi 10 --quiz-json 5              # five questions with their answers, as JSON
yokubi 10 --quiz-json 5 --source kaishi
yokubi 10 --quiz-result 4/5          # record a quiz taken in conversation
yokubi 10 --mark-read
yokubi --find 'てくれ' --source kaishi --limit 5   # real sentences for an explanation
yokubi 9 --mistake ichidan-godan --note '慣れった → 慣れた'
yokubi --mistakes                    # which kinds of mistake recur
```

Mistakes are appended to `mistakes.jsonl` in the data directory, one line each,
tagged by kind. The tags are whatever the tutor chooses; the point is that
"慣れった" and "震える is godan" end up under the same name, so the pattern
shows before the count of wrong answers would.

## Progress

Progress lives in a single portable JSON document — versioned, no absolute paths,
safe to commit to a dotfiles repo or carry between machines.

```bash
yokubi --export progress.json
yokubi --export -              # to stdout
yokubi --import progress.json
```

Merging takes the **earliest** read date and the **maximum** of each counter, so
importing the same file twice changes nothing. Unknown lessons are added; nothing
is ever dropped.

## Coverage

```bash
yokubi --coverage
```

**42 of the 64 lessons have a pattern.** How many of those are actually
*drillable* is a property of your corpus, not of this tool, so the question is
answered at runtime rather than decided in advance: `--coverage` lists every
lesson with its sentence count and marks the thin ones.

Matching is by surface pattern. No morphological analyser is required, because
the grammar worth drilling leaves a written mark: て form, past た, the
conditionals, ている, passive, なければならない. The 22 lessons with no pattern
are structural omissions — particle case, demonstratives, adverbs, counters. A
regex for those matches most of the language and tells you nothing, and drilling
noise is worse than not drilling.

Nothing is dropped for being thin. A pattern that finds four sentences in one
corpus finds four hundred in another; that is the corpus talking, not the regex.

**Known residue.** `さよなら` registers as a conditional because `なら` really is
inside it. Quotative `〜と思う` had to be excluded explicitly. Potential and
passive `〜られる` share hits, which is correct — those sentences are genuinely
ambiguous in writing. Press `g` on any sentence to see everything it matched and
judge for yourself.

`patterns.py` is a plain list of `(lesson, label, regex)`; adding or tightening
one is a one-line change.

## Which Anki decks `--import-anki` understands

Any note type with a sentence field. These names are tried, in order:

| role | field names tried |
| --- | --- |
| sentence | `Sentence`, `BackSentence`, `Expression`, `SentKanji` |
| reading | `Sentence Furigana`, `ReadingFurigana`, `Reading`, `SentFurigana` |
| gloss | `Sentence Meaning`, `Translation`, `Meaning`, `SentEng` |
| audio | `Sentence Audio`, `AudioFile`, `Audio`, `SentAudio` |

Add yours to the tuples in `yokubi.py` if your deck names them differently. Note
types with no recognised sentence field are reported and skipped, not silently
dropped.

## Paths

| what | where | override |
| --- | --- | --- |
| guide | `~/.local/share/yokubi-drill/yokubi` | `YOKUBI_DIR` |
| corpus | `~/.local/share/yokubi-drill/corpus.jsonl` | `YOKUBI_CORPUS`, `--corpus` |
| progress | `~/.local/share/yokubi-drill/progress.json` | `YOKUBI_DIR` |
| collection | `~/.local/share/anki-cli/collection.anki2` | `ANKI_CLI_DIR` |

The last row is touched **only** by `importers/anki.py`, read-only, and only
when you ask for it. It defaults to the same place as
[anki-review](https://github.com/ambiguouStr/anki-review) so the two agree out
of the box, but nothing here needs Anki installed at all.

## Credits

[**Yokubi — The Common Grammar Guide**](https://yoku.bi) is written by
[**Morgawr**](https://github.com/Morgawr) and released under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Source:
[github.com/Morgawr/yokubi](https://github.com/Morgawr/yokubi).

This repository contains none of that work. The guide is cloned at runtime by
`--fetch`, so you always read the author's current text, from the author's
repository, with the author's history attached. All the teaching here is theirs;
this is only a reader and a drill harness bolted to the side.

Example sentences come from whatever corpus you supply and belong to their
respective authors. None are included here.

## Licence

MIT, for the code in this repository. The guide keeps CC BY 4.0; your deck
content keeps whatever licence it came with.
