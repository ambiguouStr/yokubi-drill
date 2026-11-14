#!/usr/bin/env python3
"""Read a Yokubi lesson, then drill it on real sentences from a corpus you supply.

Yokubi (https://yoku.bi, by Morgawr, CC-BY-4.0) teaches the closed grammatical
system of Japanese — particles, conjugation, clauses, the four conditionals — in
64 lessons of exposition with no exercises. That is a deliberate choice on the
author's part: grammar rules are learned by use, not by flashcard. What it leaves
open is where to do the using.

So bring sentences. The corpus is a plain JSONL file — one object per line, a
leading _meta — which this reads and never writes. Build one from an Anki
collection with --import-anki, or generate it from anything else: subtitles,
Tatoeba, a textbook you typed up.

Nothing is generated here. Every sentence is one a human wrote, which matters:
invented grammar examples are exactly the kind a learner cannot check.

Usage
  yokubi                    list lessons and how many sentences each has
  yokubi 10                 read lesson 10, then drill it
  yokubi 10 --drill         skip the text
  yokubi 10 --cards         flip through the sentences instead of the quiz
  yokubi 10 --read          text only
  yokubi --random           a drillable lesson you have not opened
  yokubi --fetch            clone or update the Yokubi guide
  yokubi --import-anki      build a corpus from an Anki collection (one importer
                            among others; see importers/)
  yokubi --corpus FILE      use a particular corpus for this run
  yokubi --export FILE      write progress to a portable file ("-" for stdout)
  yokubi --import FILE      merge progress from such a file
  yokubi --where            show the paths in use

For an agent tutoring you (nothing here needs a terminal)
  yokubi 10 --quiz-json 5   five questions with their answers, as JSON
  yokubi 10 --quiz-result 4/5
  yokubi 10 --mark-read
  yokubi --find 'てくれ' --source kaishi --limit 5
  yokubi 9 --mistake ichidan-godan --note '慣れった → 慣れた'
  yokubi --mistakes         what kinds of mistake recur

Environment
  YOKUBI_DIR      data directory (default ~/.local/share/yokubi-drill)
  YOKUBI_CORPUS   corpus file (default $YOKUBI_DIR/corpus.jsonl)

The drill is JLPT 文の組み立て: four pieces of a real sentence are shuffled and
you pick the one that belongs in ★. As on the paper, one piece is cut finer
than a phrase — a particle on its own, an auxiliary parted from its て form.
Answer with 1-4; s skips; after a miss, a counts it as right when your order
was also grammatical (word order is loose, and the cutting is a heuristic).

Keys with --cards
  space  reveal reading and translation, then advance
  r      replay audio
  g      which other lessons this sentence exercises
  p      previous
  q      quit
"""
import argparse
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import termios
import textwrap
import tty

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from patterns import COMPILED, PATTERNS, label_for  # noqa: E402

GUIDE_REPO = "https://github.com/Morgawr/yokubi.git"
DATA = os.path.expanduser(os.environ.get("YOKUBI_DIR") or "~/.local/share/yokubi-drill")
GUIDE = os.path.join(DATA, "yokubi")
PROGRESS = os.path.join(DATA, "progress.json")
MISTAKES = os.path.join(DATA, "mistakes.jsonl")
CORPUS = os.path.expanduser(os.environ.get("YOKUBI_CORPUS") or os.path.join(DATA, "corpus.jsonl"))

PLAYERS = (("afplay", ()), ("mpv", ("--no-video", "--really-quiet")),
           ("ffplay", ("-nodisp", "-autoexit", "-loglevel", "quiet")))
DIM, BOLD, RESET, ACCENT = "\x1b[2m", "\x1b[1m", "\x1b[0m", "\x1b[33m"

PROGRESS_VERSION = 1


# ------------------------------------------------------------------ progress

def load_progress():
    """Progress is a plain document, versioned, with no machine-specific paths
    in it, so it can be copied between machines or kept in a dotfiles repo."""
    try:
        with open(PROGRESS, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {"version": PROGRESS_VERSION, "lessons": {}}
    if "lessons" not in doc:                       # pre-versioned layout
        doc = {"version": PROGRESS_VERSION,
               "lessons": {k: {"read_at": None, "drills": 0, "sentences": 0}
                           for k in doc if k.isdigit()}}
    doc.setdefault("version", PROGRESS_VERSION)
    doc.setdefault("lessons", {})
    return doc


def save_progress(doc):
    os.makedirs(DATA, exist_ok=True)
    doc["saved_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    tmp = PROGRESS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, PROGRESS)


def record(n, *, read=False, drilled=0, right=None):
    doc = load_progress()
    e = doc["lessons"].setdefault(str(n), {"read_at": None, "drills": 0, "sentences": 0})
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if read and not e.get("read_at"):
        e["read_at"] = now
    if drilled:
        e["drills"] = e.get("drills", 0) + 1
        e["sentences"] = e.get("sentences", 0) + drilled
        e["last_drill_at"] = now
        if right is not None:
            e["quiz_answered"] = e.get("quiz_answered", 0) + drilled
            e["quiz_right"] = e.get("quiz_right", 0) + right
    save_progress(doc)


def merge_progress(incoming):
    """Union of two progress documents: earliest read wins, counts add up.
    Designed so importing the same file twice is not idempotent for counts but
    never loses a lesson — see the warning printed by --import."""
    doc = load_progress()
    added = changed = 0
    for k, inc in (incoming.get("lessons") or {}).items():
        if not k.isdigit():
            continue
        cur = doc["lessons"].get(k)
        if cur is None:
            doc["lessons"][k] = dict(inc)
            added += 1
            continue
        before = dict(cur)
        if inc.get("read_at") and (not cur.get("read_at") or inc["read_at"] < cur["read_at"]):
            cur["read_at"] = inc["read_at"]
        cur["drills"] = max(cur.get("drills", 0), inc.get("drills", 0))
        cur["sentences"] = max(cur.get("sentences", 0), inc.get("sentences", 0))
        if inc.get("last_drill_at", "") > cur.get("last_drill_at", ""):
            cur["last_drill_at"] = inc["last_drill_at"]
        if cur != before:
            changed += 1
    save_progress(doc)
    return added, changed


# ------------------------------------------------------------------- corpus

def load_corpus(path=None):
    """The corpus is JSONL: one sentence per line, plus a leading _meta line.
    Plain enough to write by hand, grep, diff or generate from anything."""
    path = path or CORPUS
    if not os.path.exists(path):
        sys.exit(f"no corpus at {path}\n"
                 f"build one with:  yokubi --import-anki\n"
                 f"or point at another with --corpus / YOKUBI_CORPUS")
    rows, meta = [], {}
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                sys.exit(f"{path}:{lineno}: not valid JSON")
            if "_meta" in row:
                meta = row["_meta"]
                continue
            if row.get("text"):
                rows.append(row)
    if not rows:
        sys.exit(f"{path} has no sentences")
    media = meta.get("media") or ""
    for r in rows:
        a = r.get("audio") or ""
        r["audio"] = os.path.join(media, a) if a and media and not os.path.isabs(a) else a
        if r["audio"] and not os.path.exists(r["audio"]):
            r["audio"] = ""
    return rows


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


def index_corpus(corpus):
    by_lesson = {}
    for s in corpus:
        s["lessons"] = [n for n, _, rx in COMPILED if rx.search(s["text"])]
        for n in s["lessons"]:
            by_lesson.setdefault(n, []).append(s)
    return by_lesson


# -------------------------------------------------------------------- guide

def fetch_guide(quiet=False):
    os.makedirs(DATA, exist_ok=True)
    if os.path.isdir(os.path.join(GUIDE, ".git")):
        if not quiet:
            print(f"updating {GUIDE}")
        subprocess.run(["git", "-C", GUIDE, "pull", "--quiet", "--ff-only"], check=False)
    else:
        print(f"cloning Yokubi into {GUIDE}")
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", GUIDE_REPO, GUIDE], check=True)
    n = len(lesson_files())
    print(f"{n} lessons available." if n else "cloned, but no lessons found — has the layout changed?")


def lesson_files():
    out = {}
    for root, _, files in os.walk(os.path.join(GUIDE, "src")):
        for f in files:
            m = re.fullmatch(r"Lesson(\d+)\.md", f)
            if m:
                out[int(m.group(1))] = os.path.join(root, f)
    return out


def lesson_title(path):
    for line in open(path, encoding="utf-8"):
        if line.startswith("# "):
            return line[2:].strip()
    return "?"


def render_markdown(path, width):
    """Yokubi's source is mdBook, so it mixes raw HTML into the markdown: <b> to
    mark the morpheme under discussion, <div class="warning"> for exceptions,
    pipe tables for conjugation paradigms. All three carry meaning."""
    text = open(path, encoding="utf-8").read()
    text = re.sub(r'<div class="warning">', "\n⚠ ", text)
    text = re.sub(r"<div[^>]*>|</div>|</?pre>|</?code>", "", text)
    text = re.sub(r"<b>(.*?)</b>", ACCENT + BOLD + r"\1" + RESET, text, flags=re.S)
    text = re.sub(r"<i>(.*?)</i>", DIM + r"\1" + RESET, text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    out = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.startswith("|"):
            if re.fullmatch(r"[|\-: ]+", line):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if any(cells):
                out.append("    " + "   ".join(c for c in cells if c))
            continue
        if line.startswith("#"):
            depth = len(line) - len(line.lstrip("#"))
            head = line.lstrip("# ").strip()
            out += ["", f"{BOLD}{head}{RESET}"] + ([DIM + "─" * min(width, 60) + RESET]
                                                   if depth <= 1 else [])
            continue
        if line.startswith("```"):
            out.append(DIM + "─" * 20 + RESET)
            continue
        if not line.strip():
            out.append("")
            continue
        line = re.sub(r"\*\*(.+?)\*\*", BOLD + r"\1" + RESET, line)
        line = re.sub(r"`(.+?)`", ACCENT + r"\1" + RESET, line)
        line = re.sub(r"\[(.+?)\]\([^)]+\)", r"\1", line)
        indent = "  " if line.lstrip().startswith(("-", "*", "+")) else ""
        out += textwrap.wrap(line, width=width, subsequent_indent=indent) or [""]
    return "\n".join(out)


# ----------------------------------------------------------------------- tty

class Keys:
    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.tty = os.isatty(self.fd)
        if self.tty:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.tty:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def read(self):
        if not self.tty:
            return "q"
        try:
            data = os.read(self.fd, 32)
        except KeyboardInterrupt:
            return "q"
        return data.decode(errors="ignore")[:1].lower() if data else "q"


class Player:
    def __init__(self):
        self.cmd, self.proc = (), None
        if not sys.stdout.isatty() or os.environ.get("SSH_CONNECTION"):
            return
        for name, flags in PLAYERS:
            if shutil.which(name):
                self.cmd = (name, *flags)
                break

    def play(self, path):
        if not (self.cmd and path):
            return
        self.stop()
        try:
            self.proc = subprocess.Popen([*self.cmd, path], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.proc = None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()
        self.proc = None


# --------------------------------------------------------------------- views

def show_coverage(files, by_lesson, corpus):
    """Which patterns are worth anything against *this* corpus. A pattern that
    matches four sentences is useless and one that matches four hundred is a
    drill; which you get is a property of your sentences, not of the regex, so
    the judgement is made here rather than baked into patterns.py."""
    counts = sorted(((len(by_lesson.get(n, [])), n, label) for n, label, _ in PATTERNS),
                    reverse=True)
    matched = sum(1 for s in corpus if s["lessons"])
    print(f"\n{BOLD}{len(corpus)}{RESET} sentences · "
          f"{BOLD}{matched}{RESET} match at least one lesson "
          f"({100 * matched / len(corpus):.0f}%)\n")
    print(f"  {'#':>3}  {'lesson':<32}{'sentences':>10}")
    thin = []
    for c, n, label in counts:
        bar = "█" * min(30, c // 20) if c else ""
        flag = ""
        if c == 0:
            flag = f"  {DIM}nothing here{RESET}"
        elif c < 25:
            flag = f"  {DIM}thin{RESET}"
            thin.append(n)
        print(f"  {n:>3}  {label:<32}{c:>10}  {DIM}{bar}{RESET}{flag}")
    print(f"\n{DIM}  Lessons marked thin have too few examples to drill usefully — in your")
    print(f"  corpus. Add decks or another source and they may fill in. The {len(files) - len(PATTERNS)}")
    print(f"  lessons with no pattern at all are structural: their grammar leaves no")
    print(f"  reliable surface mark, so any regex would match noise.{RESET}\n")


def lesson_parts():
    """The guide's own grouping, read from its table of contents:
    [(part title, [lesson numbers]), ...]. Empty when the file is not there or
    is laid out differently, in which case the list is simply printed flat."""
    parts = []
    try:
        with open(os.path.join(GUIDE, "src", "SUMMARY.md"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r"-\s*\[(Part \d+:[^\]]*)\]", line)
                if m:
                    parts.append((m.group(1).strip(), []))
                    continue
                m = re.match(r"\s+-\s*\[.*\]\(.*?Lesson(\d+)\.md\)", line)
                if m and parts:
                    parts[-1][1].append(int(m.group(1)))
    except OSError:
        return []
    return [(title, nums) for title, nums in parts if nums]


def show_list(files, by_lesson):
    doc = load_progress()["lessons"]
    done = sum(1 for v in doc.values() if v.get("read_at"))
    print(f"\n{BOLD}Yokubi{RESET} — {len(files)} lessons, {len(PATTERNS)} drillable here"
          f"{f', {done} opened' if done else ''}")
    groups = [(t, [n for n in nums if n in files]) for t, nums in lesson_parts()]
    listed = {n for _, nums in groups for n in nums}
    rest = sorted(n for n in files if n not in listed)
    if rest:                                         # lessons the contents page does not place
        groups.append(("" if not groups else "Other", rest))
    for title, nums in groups:
        if title:
            read = sum(1 for n in nums if doc.get(str(n), {}).get("read_at"))
            print(f"\n {BOLD}{title}{RESET}{DIM}  {read}/{len(nums)}{RESET}")
        else:
            print()
        for n in nums:
            hits = len(by_lesson.get(n, []))
            e = doc.get(str(n), {})
            mark = f"{DIM}·{RESET}" if e.get("read_at") else " "
            drills = f"{DIM} ×{e['drills']}{RESET}" if e.get("drills") else ""
            count = f"{hits:>4} sentences" if hits else f"{DIM}      —     {RESET}"
            print(f" {mark}{n:>3}. {lesson_title(files[n])[:50]:<52}{count}{drills}")
    print(f"\n{DIM}  ·  opened.  No count = not drillable: that lesson's grammar leaves")
    print(f"     no reliable surface pattern, so any match would be noise.{RESET}\n")


def read_lesson(path, keys):
    width = min(shutil.get_terminal_size().columns - 4, 84)
    body = render_markdown(path, width).split("\n")
    offset, page = 0, max(5, shutil.get_terminal_size().lines - 3)
    while True:
        sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(body[offset:offset + page]))
        left = max(0, len(body) - page - offset)
        sys.stdout.write(f"\n{DIM}space/j scroll · k back · d drill · q quit"
                         f"{f'  ({left} more)' if left else '  (end)'}{RESET}")
        sys.stdout.flush()
        k = keys.read()
        if k in (" ", "j", "\n", "\r"):
            if left:
                offset += page
            else:
                return "drill"
        elif k == "k":
            offset = max(0, offset - page)
        elif k == "d":
            return "drill"
        elif k == "q":
            return "quit"


def drill(n, sentences, keys, player):
    rx = next(r for num, _, r in COMPILED if num == n)
    pool = sentences[:]
    random.shuffle(pool)
    i = seen = 0
    while 0 <= i < len(pool):
        s = pool[i]
        hit = rx.search(s["text"])
        shown = (s["text"][:hit.start()] + ACCENT + BOLD + hit.group(0) + RESET
                 + s["text"][hit.end():]) if hit else s["text"]
        sys.stdout.write("\x1b[2J\x1b[H")
        print(f"{DIM}Lesson {n} · {label_for(n)} · {i + 1}/{len(pool)} · {s['source']}{RESET}\n")
        print(f"  {shown}\n")
        player.play(s["audio"])
        print(f"{DIM}  space reveal · r replay · g other lessons · p back · q quit{RESET}")
        advance = None
        while advance is None:
            k = keys.read()
            if k == "r":
                player.play(s["audio"])
            elif k == "g":
                others = [x for x in s["lessons"] if x != n]
                print(f"\n{DIM}  also: " + (", ".join(f"{x} {label_for(x)}" for x in others)
                                            or "only this lesson") + RESET)
            elif k == "p":
                advance = -1
            elif k == "q":
                return seen
            elif k in (" ", "\n", "\r"):
                if s["reading"]:
                    print(f"\n  {DIM}{s['reading']}{RESET}")
                if s["gloss"]:
                    print(f"  {s['gloss']}")
                print(f"\n{DIM}  space next · r replay · p back · q quit{RESET}")
                while advance is None:
                    k2 = keys.read()
                    if k2 == "r":
                        player.play(s["audio"])
                    elif k2 == "p":
                        advance = -1
                    elif k2 == "q":
                        return seen + 1
                    elif k2 in (" ", "\n", "\r"):
                        advance, seen = 1, seen + 1
        i = max(0, i + advance)
    print("\n  done.")
    return seen


# ---------------------------------------------------------------------- quiz
#
# JLPT grammar never asks you to type. Its three formats are all four-choice, and
# the one that can be built from a real sentence without inventing anything is
# 文の組み立て: four consecutive chunks are blanked and shuffled, and you say which
# one goes in the ★ slot. The answer is simply the order the sentence's author
# wrote — nothing here is generated.
#
# Chunks are cut where hiragana gives way to kanji, katakana or a digit, which
# is roughly where a new content word starts. That is a heuristic, and Japanese
# word order is loose: 窓から外を and 外を窓から can both be right. ★ is never put
# on a piece whose chunk could swap places with another, and `a` lets you
# overrule the key when your order was also grammatical.

_BOUND = re.compile(r"(?<=、)|(?<=[ぁ-ゖ])(?=[一-龯々〆ヵヶァ-ヺーA-Za-z0-9０-９Ａ-Ｚａ-ｚ「『（])")
_END = "。！？!?」』…"
# kanji + one kana that ends a verb stem and is not itself a particle. Stems in
# い are listed by verb, because kanji+い is otherwise an adjective (高い|山 stays
# cut, 買い|求めて does not).
_STEM = re.compile(r"(?:[一-龯々][きしちひみりぎじびえけせねめれげべっ]"
                   r"|(?:思|買|言|使|会|笑|歌|洗|払|願|追|習|拾|誘|迷|戦|扱|疑|失|救|奪|襲|通|吸|飼"
                   r"|問|誓|祝|争|合|狙|雇|覆|敬|養|担|負|食|向か|手伝)い)$")
# a chunk ending in a case/topic particle, or an adverb set off by 、, can
# usually trade places with another
_MOVABLE = re.compile(r"(?:(?:が|を|に|で|へ|と|は|も|から|まで|より)、?|、)$")


def chunk(text):
    """Split a single sentence into bunsetsu-ish chunks; None if it is not one
    sentence (an internal 。 means two, and the shuffle would cross them)."""
    body = text.rstrip(_END)
    if re.search(r"[。！？!?]", body):
        return None
    parts = []
    for piece in body.split():
        parts += [p for p in _BOUND.split(piece) if p]
    # the honorific prefix お/ご belongs to the word after it, not the particle
    # before it: お客様にお|菓子を → お客様に|お菓子を
    for i in range(len(parts) - 1):
        m = re.search(r"[おご]$", parts[i])
        prev = parts[i][-2:-1]
        if m and prev and (prev in "はがをにでへとものや、" or not re.match(r"[ぁ-ゖ]", prev)):
            parts[i], parts[i + 1] = parts[i][:-1], parts[i][-1] + parts[i + 1]
    parts = [p for p in parts if p]
    for i in range(len(parts) - 2, -1, -1):
        # a bare お/ご chunk joins the next; so does a verb stem that is the first
        # half of a compound: 思い|出 走り|方 書き|進めて 真っ|直ぐ 食べ|物
        if parts[i] in ("お", "ご") or _STEM.search(parts[i]):
            parts[i:i + 2] = [parts[i] + parts[i + 1]]
    return parts, text[len(body):]


# The real test cuts finer than a chunk: a particle stands alone, an auxiliary
# is parted from its て form, and the grammar under test is usually what got cut.
# So one chunk in each question is split in two. Only one, because every extra
# loose particle is another way for a second order to be right.
_KANJI_END = r"[一-龯々ァ-ヺー0-9０-９]"
_SPLITS = [
    # 閉店して|しまって、  買って|もらいました   (the lesson 10-12, 22, 57 material)
    ("aux", re.compile(r"^(.+[てで])((?:しま|い(?:る|た|ま|な|て)|くれ|もら|あげ|くださ|下さ|お[いきく]"
                       r"|み(?:る|た|ま|て)|いただ|ほし|欲し|く(?:る|れ)|き(?:た|ま)|い(?:く|っ|き)).*)$")),
    # 成長する|にしたがって、  走り方|からして   食べ|ながら
    ("gram", re.compile(r"^(.+?)((?:にしたがって|にとって|について|によって|として|からして|ながら"
                        r"|ので|のに|けれど|けど)、?)$")),
    # 人|です
    ("cop", re.compile(r"^(.*" + _KANJI_END + r")((?:です|でした|だった|だ|ではない|じゃない)(?:よね|[よねか])?)$")),
    # 友人|を
    ("part", re.compile(r"^(.*" + _KANJI_END + r")((?:から|まで|より|が|を|に|で|へ|と|は|も|の)、?)$")),
]
_RANK = {"aux": 4, "gram": 4, "cop": 2, "part": 1}


def split2(c):
    """Every way of cutting chunk c in two, as (kind, left, right)."""
    out = []
    for kind, rx in _SPLITS:
        m = rx.match(c)
        if m and m.group(1) and m.group(2):
            out.append((kind, m.group(1), m.group(2)))
    return out


def build_question(s, rx, min_chunks=5):
    """One 組み立て question from sentence s, or None when it will not make one.

    Three consecutive chunks, one of them cut in two, so there are always four
    pieces and — with min_chunks=5 — always some sentence left around the
    blanks, as on the real paper."""
    cut = chunk(s["text"])
    if not cut:
        return None
    parts, tail = cut
    if len(parts) < max(min_chunks, 4):
        return None
    hit = rx.search(s["text"])
    pos, at = 0, 0                                   # which chunk holds the lesson's grammar
    if hit:
        for i, p in enumerate(parts):
            j = s["text"].find(p, pos)
            if j <= hit.start() < j + len(p):
                at = i
                break
            pos = j + len(p)
    best = None
    for w in range(len(parts) - 2):
        three = parts[w:w + 3]
        movable = [bool(_MOVABLE.search(c)) for c in three]
        for ci, c in enumerate(three):
            for kind, left, right in split2(c):
                pieces = three[:ci] + [left, right] + three[ci + 1:]
                if len(set(pieces)) < 4:
                    continue
                owner = [0, 1, 2]
                owner[ci:ci + 1] = [ci, ci]          # piece index → chunk index
                at_end = w + 3 == len(parts)

                def score(k):
                    sc = 0.0
                    if movable[owner[k]] and sum(movable) > 1:
                        sc -= 10                     # its chunk could sit elsewhere
                    if owner[k] == ci:
                        sc += 4                      # the cut is the point of the item
                    if w + owner[k] == at:
                        sc += 3
                    if at_end and k == 3:
                        sc -= 5                      # last piece of the sentence: a giveaway
                    return sc + random.random()

                star = max(range(4), key=score)
                if movable[owner[star]] and sum(movable) > 1:
                    continue                         # no piece with a single home
                total = (_RANK[kind] + (5 if w + ci == at else 0) + (2 if w <= at <= w + 2 else 0)
                         - (3 if at_end else 0) + random.random())
                if best is None or total > best[0]:
                    best = (total, w, pieces, star)
    if best is None:
        return None
    _, w, pieces, star = best
    order = list(range(4))
    while order == list(range(4)):
        random.shuffle(order)
    return {"before": "".join(parts[:w]), "after": "".join(parts[w + 3:]) + tail,
            "four": pieces, "star": star, "options": [pieces[k] for k in order],
            "hit": hit.group(0) if hit else ""}


def build_pool(sentences, rx):
    """Prefer questions with sentence on both sides of the blanks; only when a
    lesson is too thin for that, accept ones that use the whole sentence."""
    pool = [(s, q) for s in sentences if (q := build_question(s, rx))]
    if len(pool) < 10:
        have = {id(s) for s, _ in pool}
        pool += [(s, q) for s in sentences
                 if id(s) not in have and (q := build_question(s, rx, min_chunks=3))]
    return pool


def quiz(n, sentences, keys, player):
    """Returns (answered, right), or None when no sentence makes a question."""
    rx = next(r for num, _, r in COMPILED if num == n)
    pool = build_pool(sentences, rx)
    if not pool:
        return None
    random.shuffle(pool)
    answered = right = 0
    for i, (s, q) in enumerate(pool):
        slots = ["＿＿＿＿"] * 4
        slots[q["star"]] = "＿★＿＿"
        sys.stdout.write("\x1b[2J\x1b[H")
        tally = f" · {right}/{answered}" if answered else ""
        print(f"{DIM}Lesson {n} · {label_for(n)} · 文の組み立て · {i + 1}/{len(pool)}{tally}{RESET}\n")
        print(f"  次の文の ★ に入る最もよいものを、1・2・3・4から一つえらびなさい。\n")
        print(f"  {q['before']} {' '.join(slots)} {q['after']}\n")
        print("    " + "　　".join(f"{k} {opt}" for k, opt in enumerate(q["options"], 1)))
        print(f"\n{DIM}  1-4 answer · s skip · q quit{RESET}")
        k = ""
        while k not in ("1", "2", "3", "4", "s", "q"):
            k = keys.read()
        if k == "q":
            break
        sys.stdout.write("\x1b[2J\x1b[H")
        print(f"{DIM}Lesson {n} · {label_for(n)} · 文の組み立て · {i + 1}/{len(pool)}{RESET}\n")
        ok = None
        if k != "s":
            answered += 1
            ok = q["options"][int(k) - 1] == q["four"][q["star"]]
            right += ok
            mark = f"{ACCENT}✓{RESET}" if ok else f"✗  you chose {q['options'][int(k) - 1]}"
            print(f"  {mark}\n")
        filled = [f"{ACCENT}{BOLD}{p}{RESET}" if j == q["star"] else p for j, p in enumerate(q["four"])]
        print(f"  {q['before']}{''.join(filled)}{q['after']}")
        if q["hit"]:
            print(f"  {DIM}grammar: {q['hit']}{RESET}")
        if s.get("reading"):
            print(f"\n  {DIM}{s['reading']}{RESET}")
        if s.get("gloss"):
            print(f"  {s['gloss']}")
        player.play(s.get("audio"))
        hint = "a my order was also right · " if ok is False else ""
        print(f"\n{DIM}  space next · r replay · {hint}q quit{RESET}")
        while True:
            k2 = keys.read()
            if k2 == "r":
                player.play(s.get("audio"))
            elif k2 == "a" and ok is False:
                right += 1
                ok = True
                print(f"  {DIM}counted as right{RESET}")
            elif k2 == "q":
                print(f"\n  {right}/{answered} right.\n")
                return answered, right
            elif k2 in (" ", "\n", "\r"):
                break
    else:
        print("\n  done.")
    if answered:
        print(f"\n  {right}/{answered} right.\n")
    return answered, right


# --------------------------------------------------------------------- agent
#
# The same material without a terminal, for a tutor that is a program: an agent
# such as Claude Code asks for questions as JSON, puts them to the learner in
# conversation, explains the misses, and writes down what kind of miss each was.
# Nothing here reads a key or clears the screen.

def quiz_json(n, sentences, k):
    rx = next((r for num, _, r in COMPILED if num == n), None)
    pool = build_pool(sentences, rx) if rx else []
    out = []
    for s, q in random.sample(pool, min(k, len(pool))):
        out.append({
            "lesson": n, "grammar": label_for(n),
            "before": q["before"], "after": q["after"],
            "star_position": q["star"] + 1,
            "options": q["options"],
            "answer": q["options"].index(q["four"][q["star"]]) + 1,
            "correct_order": q["four"],
            "sentence": s["text"], "reading": s.get("reading", ""),
            "gloss": s.get("gloss", ""), "matched": q["hit"], "source": s.get("source", ""),
        })
    print(json.dumps({"lesson": n, "available": len(pool), "questions": out},
                     ensure_ascii=False, indent=1))


def find_sentences(corpus, pattern, source, limit):
    try:
        rx = re.compile(pattern)
    except re.error as e:
        sys.exit(f"bad regex: {e}")
    seen, hits = set(), []
    for s in corpus:
        if s["text"] in seen or (source and source.lower() not in s.get("source", "").lower()):
            continue
        if rx.search(s["text"]):
            seen.add(s["text"])
            hits.append(s)
    hits.sort(key=lambda s: len(s["text"]))
    for s in hits[:limit]:
        print(json.dumps({k: s.get(k, "") for k in ("text", "reading", "gloss", "source")},
                         ensure_ascii=False))
    print(f"# {len(hits)} matched, shortest {min(limit, len(hits))} shown", file=sys.stderr)


def log_mistake(tag, lesson, note):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", tag):
        sys.exit("a tag is short kebab-case, e.g. ichidan-godan")
    os.makedirs(DATA, exist_ok=True)
    row = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "tag": tag, "lesson": lesson, "note": note}
    with open(MISTAKES, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"logged {tag}")


def show_mistakes():
    rows = []
    try:
        with open(MISTAKES, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except OSError:
        pass
    if not rows:
        print("no mistakes logged yet")
        return
    by = {}
    for r in rows:
        by.setdefault(r["tag"], []).append(r)
    print(f"{len(rows)} mistakes, {len(by)} kinds — most frequent first\n")
    for tag, rs in sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lessons = sorted({r["lesson"] for r in rs if r.get("lesson") is not None})
        print(f"{len(rs):>3}  {tag:<22} last {rs[-1]['at'][:10]}  lessons {lessons}")
        for r in rs[-2:]:
            if r.get("note"):
                print(f"       · {r['note']}")


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Read a Yokubi lesson and drill it on real sentences.",
                                 add_help=True)
    ap.add_argument("lesson", nargs="?", type=int)
    ap.add_argument("--drill", action="store_true", help="skip the text")
    ap.add_argument("--cards", action="store_true",
                    help="flip through sentences instead of the 組み立て quiz")
    ap.add_argument("--read", action="store_true", help="text only")
    agent = ap.add_argument_group("for an agent tutoring you (no terminal needed)")
    agent.add_argument("--quiz-json", dest="quiz_json", nargs="?", const=5, type=int, metavar="K",
                       help="K 組み立て questions for the lesson, with answers, as JSON")
    agent.add_argument("--quiz-result", dest="quiz_result", metavar="RIGHT/TOTAL",
                       help="record a quiz taken outside the terminal")
    agent.add_argument("--mark-read", dest="mark_read", action="store_true",
                       help="record the lesson as read")
    agent.add_argument("--find", metavar="REGEX", help="corpus sentences matching REGEX, shortest first")
    agent.add_argument("--source", metavar="NAME", help="with --find or --quiz-json: only sources containing NAME")
    agent.add_argument("--limit", type=int, default=8, help="with --find: how many (default 8)")
    agent.add_argument("--mistake", metavar="TAG", help="log one mistake of kind TAG (kebab-case)")
    agent.add_argument("--note", default="", help="with --mistake: what was written, and what was right")
    agent.add_argument("--mistakes", action="store_true", help="summarise the logged mistakes")
    ap.add_argument("--random", action="store_true", help="a drillable lesson you have not opened")
    ap.add_argument("--fetch", action="store_true", help="clone or update the Yokubi guide")
    ap.add_argument("--import-anki", dest="import_anki", nargs="?", const=CORPUS, metavar="OUT",
                    help="build a corpus from an Anki collection (default: the standard path)")
    ap.add_argument("--corpus", metavar="FILE", help="use this corpus for the run")
    ap.add_argument("--export", metavar="FILE", help="write progress to a portable file ('-' = stdout)")
    ap.add_argument("--import", metavar="FILE", dest="import_", help="merge progress from such a file")
    ap.add_argument("--coverage", action="store_true",
                    help="how many sentences each lesson has in your corpus")
    ap.add_argument("--where", action="store_true", help="show the paths in use")
    args = ap.parse_args()

    if args.where:
        corpus = args.corpus or CORPUS
        have = "present" if os.path.exists(corpus) else "MISSING — run --import-anki"
        print(f"  guide       {GUIDE}")
        print(f"  corpus      {corpus}   ({have})")
        print(f"  progress    {PROGRESS}")
        return
    if args.import_anki:
        from importers.anki import import_anki
        return import_anki(args.import_anki)
    if args.fetch:
        return fetch_guide()
    if args.export:
        doc = load_progress()
        doc["exported_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        text = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True)
        if args.export == "-":
            print(text)
        else:
            with open(args.export, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"wrote {len(doc['lessons'])} lessons to {args.export}")
        return
    if args.import_:
        src = sys.stdin if args.import_ == "-" else open(args.import_, encoding="utf-8")
        try:
            incoming = json.load(src)
        except ValueError as e:
            sys.exit(f"not a progress file: {e}")
        if incoming.get("version", 1) > PROGRESS_VERSION:
            sys.exit(f"progress file is version {incoming['version']}, this build reads "
                     f"{PROGRESS_VERSION} — upgrade yokubi-drill")
        added, changed = merge_progress(incoming)
        print(f"merged: {added} new lessons, {changed} updated. Counts are maxed, not summed, "
              f"so re-importing the same file is safe.")
        return

    if args.mistake:
        return log_mistake(args.mistake, args.lesson, args.note)
    if args.mistakes:
        return show_mistakes()
    if args.mark_read or args.quiz_result:
        if args.lesson is None:
            sys.exit("which lesson? e.g. yokubi 12 --mark-read")
        if args.mark_read:
            record(args.lesson, read=True)
            print(f"lesson {args.lesson} marked read")
        if args.quiz_result:
            m = re.fullmatch(r"(\d+)/(\d+)", args.quiz_result)
            if not m or int(m.group(1)) > int(m.group(2)) or not int(m.group(2)):
                sys.exit("--quiz-result takes RIGHT/TOTAL, e.g. 7/10")
            record(args.lesson, drilled=int(m.group(2)), right=int(m.group(1)))
            print(f"lesson {args.lesson}: recorded {args.quiz_result}")
        return

    files = lesson_files()
    if not files:
        print(f"the Yokubi guide is not here yet ({GUIDE}).")
        fetch_guide()
        files = lesson_files()
        if not files:
            sys.exit("still no lessons found")

    corpus = load_corpus(args.corpus)
    by_lesson = index_corpus(corpus)
    if args.coverage:
        return show_coverage(files, by_lesson, corpus)
    if args.find:
        return find_sentences(corpus, args.find, args.source, args.limit)
    if args.quiz_json is not None:
        if args.lesson is None or args.lesson not in files:
            sys.exit("which lesson? e.g. yokubi 10 --quiz-json 5")
        pool = [s for s in by_lesson.get(args.lesson, [])
                if not args.source or args.source.lower() in s.get("source", "").lower()]
        return quiz_json(args.lesson, pool, args.quiz_json)
    n = args.lesson
    if args.random:
        doc = load_progress()["lessons"]
        fresh = [k for k in by_lesson if not doc.get(str(k), {}).get("read_at")]
        n = random.choice(fresh or list(by_lesson))
    if n is None:
        return show_list(files, by_lesson)
    if n not in files:
        sys.exit(f"no lesson {n}; they run 0-{max(files)}")

    sentences = by_lesson.get(n, [])
    player, drilled, right = Player(), 0, None
    try:
        with Keys() as keys:
            if not args.drill:
                if read_lesson(files[n], keys) == "quit" or args.read:
                    return
            if not sentences:
                print(f"\n  Lesson {n} has no drillable sentences in your collection — its "
                      f"grammar leaves no reliable surface pattern.\n")
                return
            result = None if args.cards else quiz(n, sentences, keys, player)
            if result is None:
                if not args.cards:
                    print(f"\n  None of lesson {n}'s sentences split into four chunks; "
                          f"showing them as cards.\n")
                drilled = drill(n, sentences, keys, player) or 0
            else:
                drilled, right = result
    finally:
        player.stop()
        record(n, read=not args.drill, drilled=drilled, right=right if drilled else None)


if __name__ == "__main__":
    main()
