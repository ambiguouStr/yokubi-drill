"""Which Yokubi lesson does a Japanese sentence exercise?

Surface-pattern matching, deliberately. No morphological analyser is required
for grammar that carries a distinctive written form — て form, the conditionals,
causative, passive and so on all leave a mark a regex can find.

Lessons whose content is syntactic rather than morphological are absent by
design: particle case, demonstratives, adverbs, counters. A regex for those
matches most of the language and tells you nothing, and a drill built on noise
is worse than no drill. That is the ceiling of this approach, not a gap to be
filled by writing looser patterns.

Whether a given pattern is *useful* depends on the corpus you point at, not on
this file — a pattern matching four sentences is useless and a pattern matching
four hundred is a drill, and which one you get is a property of your sentences.
So nothing is dropped here for being thin. Run `yokubi --coverage` to see the
counts for your own corpus and decide.

Each entry is (lesson number, label, regex). A sentence may match many.
"""
import re

# Written-form caveats worth knowing when reading these:
#   ・ている contracts to てる constantly, so both are matched
#   ・な-adjective で and the て of a て form are homographic; で is left out of
#     the て-form pattern rather than let it drag in every な-adjective clause
#   ・passive られる and potential られる are identical for ichidan verbs. The
#     two lessons therefore share hits, which is correct — the sentence really
#     is ambiguous without context.

PATTERNS = [
    (7,  "negated verbs ない",        r"(?:ない|なかっ|ません|なく(?![なて]))"),
    (9,  "past た/だ",                r"(?:[いきぎしちにびみり]た|[んだ]だ|った|かった)(?![ぁ-ん])"),
    (10, "て form",                   r"(?:[いきぎしちにびみり]て|って|んで|くて)(?![ぁ-ん])"),
    (11, "てくれる/てもらう/てあげる",  r"て(?:くれ|もら|あげ|くださ|いただ)"),
    (12, "てください",                r"て(?:ください|下さい)"),
    (14, "relative clause",           r"[るたい]\s*[一-龯ぁ-んァ-ヶ]{1,6}(?:が|を|は|に|で)"),
    (17, "ます form",                 r"ま(?:す|した|せん|しょう)(?![ぁ-ん])"),
    (18, "いる/ある/である",           r"(?:い(?:る|ます|た)|あ(?:る|ります|った)|である)(?![ぁ-ん])"),
    (19, "question か",               r"(?:か|ですか|ますか)[。？\?]"),
    (20, "explanatory のだ/んです",    r"(?:のだ|のです|んだ|んです|の[。？\?])"),
    (22, "ている/てある",              r"(?:て(?:い|)(?:る|ます|た|まし)|てあ(?:る|ります))"),
    (24, "passive れる/られる",        r"(?:[あかがさたなはまやらわ]れ|られ)(?:る|た|ます|て|ない)"),
    (25, "potential できる/〜える",     r"(?:でき(?:る|ます|た|ない)|[えけげせてねへめれ]る(?![ぁ-ん]))"),
    (26, "desire たい/ほしい",         r"(?:たい|たく|ほしい|欲しい)(?![ぁ-ん])"),
    # と after a quotative 思う/言う is not a conditional; neither is the なら inside
    # さよなら. Both are excluded rather than tolerated — a drill that shows you
    # the wrong morpheme teaches the wrong thing.
    (27, "conditionals",              r"(?:[^思言]うと[、，]|[くぐすつぬぶむる]と[、，]|たら(?!し)|だら|[えけげせてねべめれ]ば|なら(?![なびず]))"),
    (29, "sentence-final ね/よ/な",    r"(?:ね|よ|わ|ぞ|ぜ)[。！？\?!]"),
    (30, "quotation と/という",        r"(?:という|と(?:言|い)っ|と思|って)"),
    (35, "nominalisation こと/もの/の", r"(?:こと|事|もの|物|ところ|所)(?:が|を|は|に|で|だ|です)"),
    (36, "listing や/とか/など",       r"(?:や|とか|など|なんか)(?=[^。]*[。、])"),
    (37, "adversative が/けど/ても",   r"(?:けれど|けど|のに|ても|でも|しかし|が[、，])"),
    (38, "concurrence ながら/うちに",   r"(?:ながら|うちに|あいだ|間に|つつ)"),
    (39, "reasoning から/ので",        r"(?:から|ので|ため)(?:[、，]|だ|です)"),
    (40, "comparison より/の方が",     r"(?:より|の方|ほうが|一番|いちばん|最も)"),
    (42, "のに/ように/ために/おかげで",  r"(?:ように|ために|せいで|おかげで|のに(?![ぁ-ん]))"),
    (43, "likeness みたい/らしい/そう", r"(?:みたい|らしい|っぽい|そうだ|そうです|ようだ)"),
    (32, "prohibition な / なさい",    r"(?:なさい|ないで(?:ください|下さい)|[るうくぐすつぬぶむ]な[。！\s])"),
    (44, "must なければ/だめ/いけない",  r"(?:なければ|なきゃ|ないと|だめ|駄目|いけな|ならな)"),
    (45, "〜たり〜たり / ては",        r"(?:たり|だり|ては(?![ぁ-ん]))"),
    (46, "だけ/のみ/ばかり/しか",       r"(?:だけ|のみ|ばかり|ばっかり|しか)"),
    (47, "もう/まだ/また",             r"(?:もう|まだ|また)(?![ぁ-ん])"),
    (49, "とする",                    r"と(?:する|します|した|して)"),
    (50, "てみる",                    r"て(?:み(?:る|ます|た|て|よう|れば))"),
    (51, "と思う/と考える",            r"と(?:思|考え)"),
    (53, "causative せる/させる",      r"(?:[あかがさたなはまやらわ]せ|させ)(?:る|た|ます|て|ない|られ|よう)(?![ぁ-ん])"),
    (54, "なる/する",                 r"(?:に|く)(?:な(?:る|ります|った|り)|し(?:ます|た|て))"),
    (55, "自分",                      r"自分"),
    (56, "なくて/ないで/ず/ずに",       r"(?:なくて|ないで|ずに|ず[、，。])"),
    (57, "てしまう/ておく",            r"(?:て(?:しま|お(?:く|き|い))|ちゃ(?:う|っ)|じゃ(?:う|っ))"),
    (58, "ていく/てくる",              r"て(?:い(?:く|き|こう)|き(?:ます|た|て)|くる|来る)"),
    (60, "わけ/はず/べき/かもしれない",  r"(?:わけ|訳|はず|筈|べき|かもしれ|かも知れ)"),
    (61, "ころ/くらい/まで/ほど/すぎる", r"(?:ごろ|頃|くらい|ぐらい|ほど|程|すぎ|過ぎ)"),
    (62, "後/前/先/時",                r"(?:あと|後|まえ|前|さき|先|とき|時)(?:に|は|で|の|、)"),
]

COMPILED = [(n, label, re.compile(rx)) for n, label, rx in PATTERNS]


def lessons_in(sentence: str) -> list:
    """Yokubi lesson numbers whose grammar this sentence contains."""
    return [n for n, _, rx in COMPILED if rx.search(sentence)]


def label_for(n: int) -> str:
    for num, label, _ in PATTERNS:
        if num == n:
            return label
    return "?"


COVERED = sorted({n for n, _, _ in PATTERNS})
