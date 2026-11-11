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
  yokubi 10 --read          text only
  yokubi --random           a drillable lesson you have not opened
  yokubi --fetch            clone or update the Yokubi guide
  yokubi --import-anki      build a corpus from an Anki collection (one importer
                            among others; see importers/)
  yokubi --corpus FILE      use a particular corpus for this run
  yokubi --export FILE      write progress to a portable file ("-" for stdout)
  yokubi --import FILE      merge progress from such a file
  yokubi --where            show the paths in use

Environment
  YOKUBI_DIR      data directory (default ~/.local/share/yokubi-drill)
  YOKUBI_CORPUS   corpus file (default $YOKUBI_DIR/corpus.jsonl)

Keys while drilling
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


def record(n, *, read=False, drilled=0):
    doc = load_progress()
    e = doc["lessons"].setdefault(str(n), {"read_at": None, "drills": 0, "sentences": 0})
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if read and not e.get("read_at"):
        e["read_at"] = now
    if drilled:
        e["drills"] = e.get("drills", 0) + 1
        e["sentences"] = e.get("sentences", 0) + drilled
        e["last_drill_at"] = now
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


def show_list(files, by_lesson):
    doc = load_progress()["lessons"]
    done = sum(1 for v in doc.values() if v.get("read_at"))
    print(f"\n{BOLD}Yokubi{RESET} — {len(files)} lessons, {len(PATTERNS)} drillable here"
          f"{f', {done} opened' if done else ''}\n")
    for n in sorted(files):
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


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Read a Yokubi lesson and drill it on real sentences.",
                                 add_help=True)
    ap.add_argument("lesson", nargs="?", type=int)
    ap.add_argument("--drill", action="store_true", help="skip the text")
    ap.add_argument("--read", action="store_true", help="text only")
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
    player, drilled = Player(), 0
    try:
        with Keys() as keys:
            if not args.drill:
                if read_lesson(files[n], keys) == "quit" or args.read:
                    return
            if not sentences:
                print(f"\n  Lesson {n} has no drillable sentences in your collection — its "
                      f"grammar leaves no reliable surface pattern.\n")
                return
            drilled = drill(n, sentences, keys, player) or 0
    finally:
        player.stop()
        record(n, read=not args.drill, drilled=drilled)


if __name__ == "__main__":
    main()
