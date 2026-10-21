"""What the narrator says for a text block: the screen keeps the text as written, the voice hears it
the way a person reads it out loud (".50 cal" -> "fifty cal", "WWII" -> "World War Two",
"1960s" -> "nineteen sixties", "40 km" -> "forty kilometers").

Two layers:
- the script's "say" map (Claude adds it when something on screen would be misread, e.g.
  {"Henry VIII": "Henry the Eighth"}); it wins over the rules, and
- rules for numbers, years, money, units, ordinals and common abbreviations.

words() keeps track of which caption word each spoken word belongs to, so the spoken-word
highlight in the captions still lights up the right word on screen."""
from __future__ import annotations

import re

ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
SCALES = [(10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")]
ORDINAL = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
           "nine": "ninth", "twelve": "twelfth"}

# Said as written words. Keys match the word without its trailing punctuation (case-sensitive).
ABBREVIATIONS = {
    "WWI": "World War One", "WW1": "World War One", "WWII": "World War Two", "WW2": "World War Two",
    "Dr.": "Doctor", "Mr.": "Mister", "Mrs.": "Missus", "Mt.": "Mount", "Ft.": "Fort", "St.": "Saint",
    "Lt.": "Lieutenant", "Gen.": "General", "Col.": "Colonel", "Capt.": "Captain", "Sgt.": "Sergeant",
    "Cpl.": "Corporal", "Adm.": "Admiral", "Pvt.": "Private", "Jr.": "Junior", "Sr.": "Senior",
    "vs": "versus", "vs.": "versus", "approx.": "approximately", "e.g.": "for example", "i.e.": "that is",
    "etc.": "et cetera", "&": "and", "No.": "number", "km/h": "kilometers an hour",
    "mph": "miles an hour", "kph": "kilometers an hour", "ca.": "around", "c.": "around",
}
# After a number: (singular, plural). "in" and "m" alone are too ambiguous unless glued to the number.
UNITS = {
    "km": ("kilometer", "kilometers"), "cm": ("centimeter", "centimeters"), "mm": ("millimeter", "millimeters"),
    "kg": ("kilogram", "kilograms"), "g": ("gram", "grams"), "mg": ("milligram", "milligrams"),
    "lb": ("pound", "pounds"), "lbs": ("pound", "pounds"), "oz": ("ounce", "ounces"),
    "ft": ("foot", "feet"), "mi": ("mile", "miles"), "yd": ("yard", "yards"), "yds": ("yard", "yards"),
    "mph": ("mile an hour", "miles an hour"), "km/h": ("kilometer an hour", "kilometers an hour"),
    "kph": ("kilometer an hour", "kilometers an hour"), "m/s": ("meter a second", "meters a second"),
    "hp": ("horsepower", "horsepower"), "kW": ("kilowatt", "kilowatts"), "MW": ("megawatt", "megawatts"),
    "GW": ("gigawatt", "gigawatts"), "kWh": ("kilowatt hour", "kilowatt hours"),
    "°C": ("degree Celsius", "degrees Celsius"), "°F": ("degree Fahrenheit", "degrees Fahrenheit"),
    "°": ("degree", "degrees"), "K": ("kelvin", "kelvin"), "t": ("ton", "tons"),
    "min": ("minute", "minutes"), "mins": ("minute", "minutes"), "hr": ("hour", "hours"), "hrs": ("hour", "hours"),
    "sec": ("second", "seconds"), "s": ("second", "seconds"), "ms": ("millisecond", "milliseconds"),
    "x": ("times", "times"), "%": ("percent", "percent"),
    "m": ("meter", "meters"), "in": ("inch", "inches"), "MPH": ("mile an hour", "miles an hour"),
}
# A unit as its own word after a number ("40 km"); the short ones only count glued on ("5m", "3in"):
# "3 in 10 people" is not inches.
LOOSE_UNITS = {k for k in UNITS if k not in ("m", "in", "s", "t", "x", "K", "g")}
CURRENCY = {"$": ("dollar", "dollars"), "€": ("euro", "euros"), "£": ("pound", "pounds")}
BIG = {"thousand", "million", "billion", "trillion"}

NUM = r"\d[\d,]*(?:\.\d+)?"
PUNCT_L = "\"'“‘(["
PUNCT_R = "\"'”’)]!?,;:…"


def cardinal(n: int) -> str:
    if n < 0:
        return "minus " + cardinal(-n)
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return ONES[n // 100] + " hundred" + ("" if not rest else " " + cardinal(rest))
    for size, name in SCALES:
        if n >= size:
            rest = n % size
            return cardinal(n // size) + " " + name + ("" if not rest else " " + cardinal(rest))
    return str(n)


def ordinal(n: int) -> str:
    words = cardinal(n).split()
    last = words[-1]
    if last in ORDINAL:
        words[-1] = ORDINAL[last]
    elif last.endswith("y"):
        words[-1] = last[:-1] + "ieth"
    else:
        words[-1] = last + "th"
    return " ".join(words)


def year(n: int) -> str:
    """1969 -> nineteen sixty nine, 1905 -> nineteen oh five, 1900 -> nineteen hundred, 2008 -> two thousand eight."""
    if 2000 <= n < 2010 or n % 1000 == 0:
        return cardinal(n)
    hi, lo = divmod(n, 100)
    return cardinal(hi) + (" hundred" if lo == 0 else " oh " + ONES[lo] if lo < 10 else " " + cardinal(lo))


def plural_decade(words: str) -> str:
    """'nineteen sixty' -> 'nineteen sixties', 'nineteen hundred' -> 'nineteen hundreds'."""
    w = words.split()
    w[-1] = w[-1][:-1] + "ies" if w[-1].endswith("y") else w[-1] + "s"
    return " ".join(w)


def number(s: str, as_year: bool = True) -> str:
    """'40,000' -> forty thousand, '3.5' -> three point five, '1969' -> nineteen sixty nine."""
    s = s.replace(",", "")
    whole, _, frac = s.partition(".")
    if not whole:
        whole = "0"
    n = int(whole)
    if frac:
        return cardinal(n) + " point " + " ".join(ONES[int(d)] for d in frac)
    if as_year and len(whole) == 4 and 1100 <= n <= 2099:
        return year(n)
    return cardinal(n)


def _unit(key: str, value: str | None) -> str | None:
    pair = UNITS.get(key)
    if not pair:
        return None
    return pair[0] if value is not None and value.replace(",", "") in ("1", "1.0") else pair[1]


def _core(word: str) -> tuple[str, str, str]:
    """('(', 'WWII', '),') for '(WWII),': leading punctuation, the word, trailing punctuation.
    A final '.' stays on the word only when it is an abbreviation ('Dr.', 'e.g.')."""
    i = 0
    while i < len(word) and word[i] in PUNCT_L:
        i += 1
    j = len(word)
    while j > i and word[j - 1] in PUNCT_R:
        j -= 1
    core = word[i:j]
    if core.endswith(".") and core not in ABBREVIATIONS and core not in UNITS:
        core, j = core[:-1], j - 1
    return word[:i], core, word[j:]


def _say_word(core: str, prev: str | None, nxt: str | None) -> str | None:
    """The spoken form of one word on screen, or None to say it as written. prev/nxt are the
    neighbouring words' cores (for '5 km', '.50 cal', '$3 million')."""
    if core in ABBREVIATIONS and not (core == "No." and not (nxt and nxt[:1].isdigit())):
        if core in LOOSE_UNITS and prev and re.fullmatch(NUM, prev):
            return _unit(core, prev)
        return ABBREVIATIONS[core]
    prev_num = prev if prev and re.fullmatch(NUM, prev) else None
    if prev_num and core in LOOSE_UNITS:
        return _unit(core, prev_num)
    if m := re.fullmatch(r"\.(\d+)(-?(?:cal|caliber|calibre))?", core):  # .50 cal, .50-cal, .303
        return cardinal(int(m[1])) + (" cal" if m[2] else "")
    if m := re.fullmatch(r"(\d+)(?:-|\s)?(?:cal|caliber|calibre)", core):  # 50-cal
        return cardinal(int(m[1])) + " cal"
    if m := re.fullmatch(r"([$€£])(" + NUM + r")([kKmMbB]n?)?", core):  # $5, $1.5, $3bn
        big = {"k": "thousand", "m": "million", "b": "billion"}.get((m[3] or "")[:1].lower())
        cur = CURRENCY[m[1]]
        if not big and nxt and nxt.lower() in BIG:
            return number(m[2], as_year=False)  # the currency follows the next word: "$3 million"
        return " ".join(filter(None, [number(m[2], as_year=False), big,
                                      cur[0] if m[2] == "1" and not big else cur[1]]))
    if m := re.fullmatch(r"(\d+)(st|nd|rd|th)", core):  # 21st
        return ordinal(int(m[1]))
    if m := re.fullmatch(r"(\d{2,4})['’]?s", core):  # 1960s, 1800s
        n = int(m[1])
        return plural_decade(year(n) if len(m[1]) == 4 else cardinal(n))
    if m := re.fullmatch(r"['’](\d0)s", core):  # '60s
        return plural_decade(cardinal(int(m[1])))
    if m := re.fullmatch(r"([-−]?)(" + NUM + r")-?([A-Za-z°/%]+[A-Za-z]*)", core):  # 40km, 100%, -40°F
        unit = _unit(m[3], m[2])
        if unit:
            return ("minus " if m[1] else "") + number(m[2], as_year=False) + " " + unit
    if m := re.fullmatch(r"(\d{4})[-–](\d{2,4})", core):  # 1939-1945, 1939-45
        a, b = int(m[1]), int(m[2])
        if len(m[2]) == 2:
            b = a // 100 * 100 + b
        return year(a) + " to " + year(b)
    if m := re.fullmatch(r"(\d+)[-–](\d+)", core):  # 3-4
        return cardinal(int(m[1])) + " to " + cardinal(int(m[2]))
    if m := re.fullmatch(r"([A-Z]{1,3})-(\d+)([A-Z]?)", core):  # B-17, P-51D, MiG not matched
        return " ".join(filter(None, [" ".join(m[1]), number(m[2], as_year=False), m[3]]))
    if re.fullmatch(NUM, core):
        unit_next = nxt is not None and (nxt in LOOSE_UNITS or nxt.lower() in BIG)
        return number(core, as_year=not unit_next and "," not in core)
    if m := re.fullmatch(r"(" + NUM + r")-(\w+)", core):  # 40-storey, 3-mile
        rest = _unit(m[2], "1") or m[2]  # "a 3-mile walk": singular
        return number(m[1], as_year=False) + " " + rest
    return None


def _money_tail(prev: str | None, core: str) -> str | None:
    """'$3 million' -> 'three million dollars': the currency comes after the big number."""
    if prev and (m := re.fullmatch(r"([$€£])" + NUM, prev)) and core.lower() in BIG:
        return core.lower() + " " + CURRENCY[m[1]][1]
    return None


def _match_say(tokens: list[str], start: int, say: list[tuple[list[str], str]]) -> tuple[int, str] | None:
    """The longest "say" phrase starting at tokens[start]: (number of caption words, spoken text)."""
    best = None
    for phrase, spoken in say:
        n = len(phrase)
        if start + n <= len(tokens) and [t.lower() for t in tokens[start:start + n]] == phrase:
            if not best or n > best[0]:
                best = (n, spoken)
    return best


def _clean(word: str) -> str:
    return word.replace("*", "")


def _say_key(word: str) -> str:
    return _clean(word).strip(PUNCT_L + PUNCT_R + ".").lower()


def parse_say(say) -> list[tuple[list[str], str]]:
    """The script's {"on screen": "spoken"} map as [(words on screen lowercased, spoken), ...]."""
    out = []
    if isinstance(say, dict):
        for k, v in say.items():
            words = [_say_key(w) for w in str(k).split()]
            # "forty-seven" -> "forty seven": one spoken word per word keeps the highlight in step
            v = " ".join(re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", " ", str(v or "")).split())
            if words and all(words) and v:
                out.append((words, v))
    return out


def words(text: str, say=None) -> list[str]:
    """Per caption word (text.split(), asterisks and all): what the narrator says for it. A
    phrase from the "say" map goes on its first word; the other words of the phrase get ''."""
    tokens = text.split()
    phrases = parse_say(say) if not isinstance(say, list) else say
    keys = [_say_key(t) for t in tokens]
    cores = [_core(_clean(t)) for t in tokens]
    out: list[str] = []
    i = 0
    while i < len(tokens):
        hit = _match_say(keys, i, phrases) if phrases else None
        if hit:
            n, spoken = hit
            lead, _, _ = cores[i]
            trail = cores[i + n - 1][2]
            out += [lead + spoken + trail] + [""] * (n - 1)
            i += n
            continue
        lead, core, trail = cores[i]
        prev = cores[i - 1][1] if i else None
        nxt = cores[i + 1][1] if i + 1 < len(tokens) else None
        said = _money_tail(prev, core) or _say_word(core, prev, nxt)
        out.append(_clean(tokens[i]) if said is None else lead + said + trail)
        i += 1
    return out


def text(text: str, say=None) -> str:
    """The whole block as the narrator says it."""
    return " ".join(w for w in words(text, say) if w)
