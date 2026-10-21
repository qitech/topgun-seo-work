#!/usr/bin/env python3
"""Check a product page text against the TOPGUN SEO text rules (the «шпаргалка»).

Counts what Advego counts (word frequency over all words, word forms merged by a crude
UA/RU stemmer) and checks every page zone: title, meta description, H1, H2/H3, first and
last paragraph, table head, list header, image alts. The rules and their reasons live in
.claude/skills/writing-seo-product-text/SKILL.md.

Input is either a draft (Markdown with front matter) or a rendered page (built HTML file or
live URL). The semantic core (main query, synonyms, keys) comes from a core.json:

  {"lang": "uk", "gz": "Magtech N2 Extreme 1300", "gz_short": "N2 Extreme",
   "gz_words": ["n2", "extreme", "1300"],
   "synonyms": [{"text": "пневматична гвинтівка", "main": true}, {"text": "воздушка"}],
   "keys": ["ціна", "купити", "характеристики", "газова пружина"],
   "lsi": ["нарізний ствол", "дульна енергія"],
   "part_synonyms": ["воздушка Magtech"]}

`gz_words` are the model word(s) of the main query held to 2.5–3.5% (default: the last
word of `gz`); the other words of `gz`/`gz_short` (brand, series) are only capped at 3.5%. `variant_words` (colour, size of this variant: «nikel»)
may be as frequent as a synonym. Synonyms with `"main": true` may reach 2%.

Examples:
  ./venv13/bin/python3 scripts/seo_text_check.py --draft scratch/n2.md --core scratch/n2.core.json
  ./venv13/bin/python3 scripts/seo_text_check.py --url https://topgun.in.ua/products/... --core core.json
  ./venv13/bin/python3 scripts/seo_text_check.py --draft scratch/n2.md --core core.json --json

Exit code: 0 = no ERROR, 1 = at least one ERROR, 2 = bad input.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

LINK_MD_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")
WORD_RE = re.compile(r"[0-9a-zà-ÿа-яёіїєґ'’ʼ]+(?:[.,][0-9]+)?", re.IGNORECASE)
SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")

# Advego lists these separately and they never count as an over-used word.
STOP_WORDS = set(
    """і й та а але або чи що як це цей ця ці той та те ті в у на з із зі до від для по при про над під
    між через без за його її їх він вона воно вони ми ви ти я є був була було були буде бути не ні так
    також ще вже лише тільки можна може треба які який яка яке де коли щоб якщо тому теж свій своя своє
    свої наш ваш весь вся все всі кожен кожна дуже більш більше менш менше
    и в во на с со к ко от до для по при о об про над под между через без за его ее их он она оно они
    мы вы ты я есть был была было были будет быть не ни так также еще уже лишь только можно может
    нужно какие какой какая какое где когда чтобы если поэтому тоже свой своя свое свои наш ваш весь вся
    все всё каждый каждая очень более менее это этот эта эти тот та то те""".split()
    # Units are counted by Advego but are not words anyone optimizes for.
    + "мм см м кг г гр дж мс м/с шт л мл".split()
)

ENDINGS = sorted(
    """ою ею ами ями ах ях ові еві ому ого ими іми их ій ий ая яя ое ее ые ие ої ів ей ов ам ям ом ем
    ого ему ыми ая ую юю ую ою ой ей ий ый ая яя а я о е и і ї у ю ь й ы""".split(),
    key=len,
    reverse=True,
)

# Default targets. Percent of all words in the body text (Advego «частота»).
GZ_MIN, GZ_MAX, GZ_HARD_MAX = 2.5, 3.0, 3.5
MAIN_SYN_MAX = 2.0
KEY_MIN, KEY_MAX = 0.7, 1.0
ANY_WORD_MAX = 1.5      # no other word may exceed this
PLAIN_WORD_MAX = 0.8    # a plain (non-core) word above this is a warning
TITLE_MIN, TITLE_MAX = 70, 110
DESC_MIN, DESC_MAX = 140, 220
FIRST_PARA_MIN, FIRST_PARA_MAX = 50, 60
PARA_MAX_WORDS = 80     # ≈ 6 lines in the product column
SECTION_MAX_WORDS = 160  # ≈ 12 lines
SENTENCE_TARGET = 10
SENTENCE_WARN = 16
HEADINGS_WITH_GZ = 0.6


def words(text: str) -> list[str]:
    return [w.lower().replace("’", "'").replace("ʼ", "'") for w in WORD_RE.findall(text or "")]


def stem(word: str) -> str:
    w = word.lower()
    if not re.search(r"[а-яёіїєґ]", w) or len(w) < 5:
        return w
    for end in ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 4:
            return w[: -len(end)]
    return w


def stems(text: str) -> list[str]:
    return [stem(w) for w in words(text)]


def phrase_count(text: str, phrase: str) -> int:
    """Occurrences of a phrase, word forms merged, words adjacent (one word may sit between)."""
    p = stems(phrase)
    t = stems(text)
    if not p:
        return 0
    count, i = 0, 0
    while i < len(t):
        if t[i] != p[0]:
            i += 1
            continue
        j, k, gaps = i + 1, 1, 0
        while k < len(p) and j < len(t):
            if t[j] == p[k]:
                k += 1
            elif gaps == 0:
                gaps += 1
            else:
                break
            j += 1
        if k == len(p):
            count += 1
            i = j
        else:
            i += 1
    return count


def contains(text: str, phrase: str) -> bool:
    return bool(phrase) and phrase_count(text, phrase) > 0


def contains_all_words(text: str, phrase: str) -> bool:
    have = set(stems(text))
    return bool(phrase) and all(s in have for s in stems(phrase))


# --------------------------------------------------------------------------- input


def parse_draft(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    meta: dict = {"alts": []}
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            head, body = raw[3:end], raw[end + 4 :]
            key = None
            for line in head.splitlines():
                if not line.strip():
                    continue
                m = re.match(r"^(\w+):\s*(.*)$", line)
                if m:
                    key, val = m.group(1).lower(), m.group(2).strip()
                    if val:
                        meta[key] = val.strip("\"'")
                    elif key == "alts":
                        meta["alts"] = []
                elif key == "alts" and line.strip().startswith("-"):
                    meta["alts"].append(line.strip()[1:].strip().strip("\"'"))
    return markdown_zones(body, meta)


def markdown_zones(body: str, meta: dict) -> dict:
    headings, tables, list_headers, paragraphs, sections = [], [], [], [], []
    lists = 0
    lines = body.splitlines()
    i = 0
    current_section: list[str] = []
    prose_lines: list[str] = []
    prev_block = ""

    def flush_para(buf: list[str]):
        text = " ".join(x.strip() for x in buf).strip()
        if text:
            paragraphs.append(text)
        return text

    para: list[str] = []
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if re.match(r"^#{2,3}\s", s):
            if para:
                prev_block = flush_para(para)
                para = []
            if current_section:
                sections.append(" ".join(current_section))
            current_section = []
            headings.append(re.sub(r"^#+\s*", "", s))
            prev_block = ""
        elif re.match(r"^#\s", s) and not meta.get("h1"):
            meta["h1"] = s[2:].strip()
        elif s.startswith("|"):
            if para:
                prev_block = flush_para(para)
                para = []
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            cells = [c.strip() for c in rows[0].strip("|").split("|")]
            tables.append({"head": " ".join(cells), "rows": len(rows)})
            current_section.extend(rows)
            prev_block = ""
            continue
        elif re.match(r"^([-*+]|\d+[.)])\s", s):
            if para:
                prev_block = flush_para(para)
                para = []
            lists += 1
            items = []
            while i < len(lines) and re.match(r"^\s*([-*+]|\d+[.)])\s", lines[i]):
                items.append(re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", lines[i]))
                i += 1
            list_headers.append(prev_block if prev_block.rstrip().endswith(":") else "")
            current_section.extend(items)
            prose_lines.append("\n".join(items))
            prev_block = ""
            continue
        elif not s:
            if para:
                prev_block = flush_para(para)
                para = []
        else:
            para.append(s)
            current_section.append(s)
            prose_lines.append(s)
        i += 1
    if para:
        flush_para(para)
    if current_section:
        sections.append(" ".join(current_section))

    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body)
    clean = re.sub(r"[*_`>#|]", " ", clean)
    # Text under each H2 (H3 belongs to its H2), links unwrapped.
    parts = re.split(r"(?m)^##[ \t]+(.+)$", body)
    h2_sections = [(parts[k].strip(), re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", parts[k + 1]))
                   for k in range(1, len(parts) - 1, 2)]
    first_raw = next((b.strip() for b in body.split("\n\n") if b.strip()
                      and not re.match(r"^(#|\||[-*+>]\s|\d+[.)]\s)", b.strip())), "")
    m_link = LINK_MD_RE.search(first_raw)
    m_end = re.search(r"[.!?…](\s|$)", first_raw)
    first_link_in_first_sentence = bool(m_link and (not m_end or m_link.start() < m_end.start()))
    last_block_is_list = bool(re.search(r"(^|\n)\s*([-*+]|\d+[.)])\s[^\n]*\s*$", body.strip()))
    return {
        "title": meta.get("title", ""),
        "description": meta.get("description", ""),
        "h1": meta.get("h1", ""),
        "headings": headings,
        "alts": meta.get("alts", []),
        "tables": tables,
        "lists": lists,
        "list_headers": list_headers,
        "paragraphs": [re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", p) for p in paragraphs],
        "sections": sections,
        "body": clean,
        "last_is_list": last_block_is_list,
        "h2_sections": h2_sections,
        "first_sentence_link": first_link_in_first_sentence,
        "brand_links": len(re.findall(r"\]\(/(?:ru/)?brands/", body)),
        "mode": "draft",
    }


class PageParser(HTMLParser):
    """Pull the SEO zones out of a rendered TOPGUN product page."""

    def __init__(self, code: str | None):
        super().__init__(convert_charrefs=True)
        self.code = code
        self.zones = {"title": "", "description": "", "h1": "", "headings": [], "alts": [],
                      "tables": [], "lists": 0, "list_headers": [], "paragraphs": [], "body_parts": []}
        self.stack: list[str] = []
        self.capture: str | None = None
        self.buf: list[str] = []
        self.in_body = 0
        self.body_depth: list[int] = []
        self.last_para = ""
        self.in_th = False
        self.th_buf: list[str] = []
        self.table_heads: list[str] = []
        self.h2_sections: list[list[str]] = []
        self.first_link_pos: int | None = None
        self.first_sentence_link = False
        self.brand_links = 0
        self.alt_by_image: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if tag == "meta" and a.get("name") == "description":
            self.zones["description"] = a.get("content", "")
        if tag == "img":
            src = a.get("src", "") or ""
            if self.code is None or f"/products/{self.code}/" in src:
                # The main photo, its thumbnail and the lightbox are one image: one alt.
                key = re.sub(r"-480(?=\.)", "", src.split("?")[0])
                self.alt_by_image.setdefault(key, a.get("alt", "") or "")
        if tag in ("article",) and "markdown-body" in cls or (tag == "div" and "faq-answer" in cls):
            self.in_body += 1
            self.body_depth.append(len(self.stack))
        if tag not in ("img", "meta", "br", "hr", "input", "link", "source"):
            self.stack.append(tag)
        if tag in ("title", "h1", "h2", "h3", "p", "li") and self.capture is None:
            self.capture, self.buf = tag, []
        if tag in ("ul", "ol") and self.in_body:
            self.zones["lists"] += 1
            self.zones["list_headers"].append(self.last_para if self.last_para.rstrip().endswith(":") else "")
        if tag == "th":
            self.in_th, self.th_buf = True, []
        if tag == "a" and self.in_body and re.match(r"^/(?:ru/)?brands/", a.get("href", "") or ""):
            self.brand_links += 1
        if (tag == "a" and self.capture == "p" and self.in_body and not self.zones["paragraphs"]
                and self.first_link_pos is None):
            self.first_link_pos = len("".join(self.buf))
        # The description's own tables and the page's specs block (.chars-table) both count.
        if tag == "table" and (self.in_body or "chars-table" in cls):
            self.table_heads.append("")

    def handle_endtag(self, tag):
        if tag == "th" and self.in_th:
            self.in_th = False
            if self.table_heads:
                self.table_heads[-1] += " " + "".join(self.th_buf)
        if self.capture == tag:
            text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if tag == "title":
                self.zones["title"] = text
            elif tag == "h1":
                self.zones["h1"] = text
            elif tag in ("h2", "h3") and self.in_body:
                self.zones["headings"].append(text)
                if tag == "h2":
                    self.h2_sections.append([text, ""])
            elif tag == "p" and self.in_body:
                if not self.zones["paragraphs"] and self.first_link_pos is not None:
                    m_end = re.search(r"[.!?…](\s|$)", text)
                    self.first_sentence_link = not m_end or self.first_link_pos < m_end.start()
                self.zones["paragraphs"].append(text)
                self.last_para = text
            self.capture = None
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
            if self.body_depth and len(self.stack) == self.body_depth[-1]:
                self.body_depth.pop()
                self.in_body -= 1

    def handle_data(self, data):
        if self.capture:
            self.buf.append(data)
        if self.in_th:
            self.th_buf.append(data)
        if self.in_body:
            self.zones["body_parts"].append(data)
            if self.h2_sections and self.capture != "h2":
                self.h2_sections[-1][1] += data


def parse_html(source: str, code: str | None) -> dict:
    if re.match(r"^https?://", source):
        req = urllib.request.Request(source, headers={"User-Agent": "Mozilla/5.0 topgun-seo-check"})
        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
        if code is None:
            m = re.search(r"-(\d{6,8})/?$", source)
            code = m.group(1) if m else None
    else:
        raw = Path(source).read_text(encoding="utf-8")
    p = PageParser(code)
    p.feed(raw)
    z = p.zones
    z["tables"] = [{"head": h.strip(), "rows": 0} for h in p.table_heads]
    z["body"] = html.unescape(" ".join(z.pop("body_parts")))
    z["sections"] = [z["body"]]
    z["last_is_list"] = False
    z["mode"] = "page"
    z["h2_sections"] = [tuple(x) for x in p.h2_sections]
    z["first_sentence_link"] = p.first_sentence_link
    z["brand_links"] = p.brand_links
    z["alts"] = list(p.alt_by_image.values())
    # The site appends «| TOPGUN»; the rules apply to what is in front of it.
    return z


# --------------------------------------------------------------------------- checks


class Report:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, level: str, zone: str, msg: str):
        self.items.append({"level": level, "zone": zone, "msg": msg})

    def err(self, zone, msg):
        self.add("ERROR", zone, msg)

    def warn(self, zone, msg):
        self.add("WARN", zone, msg)

    def ok(self, zone, msg):
        self.add("OK", zone, msg)


def fix_hint(c: int, total: int, cap_pct: float) -> str:
    """What to do about an over-used word: drop N mentions, or grow the text to M words."""
    allowed = max(1, math.ceil(total * cap_pct / 100))
    need_words = math.ceil(c * 100 / cap_pct)
    return f"прибери {c - allowed} згадк. у реченнях або допиши текст до ≥{need_words} слів фактами"


def count_bounds(total: int, lo: float, hi: float) -> tuple[int, int]:
    """Allowed mention counts: rounding goes in the writer's favour (a 300-word text may
    hold 7–11 mentions at 2.5–3.5%), so short texts are not forced to pad or cut."""
    return max(1, math.floor(total * lo / 100)), max(1, math.ceil(total * hi / 100))


def check(z: dict, core: dict) -> tuple[Report, dict]:
    r = Report()
    gz = core["gz"].strip()
    gz_short = (core.get("gz_short") or "").strip()
    syns = core.get("synonyms") or []
    main_syns = [s["text"] for s in syns if s.get("main")]
    keys = core.get("keys") or []
    lsi = core.get("lsi") or []
    # The band applies to the model word(s); by default the last word of the ГЗ («extreme», «2608»).
    gz_words = [w.lower() for w in (core.get("gz_words") or [w for w in words(gz) if w not in STOP_WORDS][-1:])]
    model_phrase = " ".join(gz_words)

    def has_gz(text: str, allow_short=True, allow_syn=False) -> bool:
        cands = [gz] + ([gz_short] if allow_short and gz_short else []) + (main_syns if allow_syn else [])
        if allow_short and model_phrase:
            cands.append(model_phrase)
        return any(contains(text, c) for c in cands)

    body = z["body"]
    tokens = stems(body)
    total = len(tokens)
    freq = Counter(tokens)
    stats = {"words": total}

    # Title
    title = re.sub(r"\s*[|—-]\s*TOPGUN\s*$", "", z["title"]).strip()
    if not title:
        r.err("title", "немає title")
    else:
        if not contains(title, gz):
            r.err("title", f"немає ГЗ «{gz}»: «{title}»")
        elif stems(title)[: len(stems(gz))] != stems(gz) and not stems(title)[:6].count(stems(gz)[0]):
            r.warn("title", "ГЗ не на початку title")
        n = len(title)
        if not TITLE_MIN <= n <= TITLE_MAX:
            r.warn("title", f"{n} символів (ціль ≈100, {TITLE_MIN}–{TITLE_MAX})")
        if re.search(r"\b\d{6,}\b", title):
            r.warn("title", "у title артикул/код — це не пошуковий запит")
        hit = [k for k in keys + main_syns if contains(title, k)]
        if not hit:
            r.warn("title", "у title немає жодного ключа другого порядку чи головного синоніма")

    # Description
    d = z["description"]
    if not d:
        r.err("description", "немає meta description")
    else:
        if not contains(d, gz):
            r.err("description", f"немає повного ГЗ «{gz}»")
        if not DESC_MIN <= len(d) <= DESC_MAX:
            r.warn("description", f"{len(d)} символів (ціль ≈200)")

    # H1
    h1 = z["h1"]
    if not h1:
        r.err("h1", "немає H1")
    elif not contains(h1, gz):
        r.err("h1", f"H1 «{h1}» не містить ГЗ «{gz}»")
    else:
        extra = [w for w in words(h1) if stem(w) not in set(stems(gz) + sum((stems(s) for s in main_syns + keys), []))]
        # A long number that is the model name itself (Grand Way 220125) is part of the ГЗ, not a code.
        if any(n not in gz for n in re.findall(r"\b\d{6,}\b", h1)):
            r.err("h1", "у H1 артикул/код")
        elif len(extra) > 2:
            r.warn("h1", f"у H1 зайві слова поза ГЗ і синонімами: {' '.join(extra)}")

    # H2/H3
    hs = z["headings"]
    if len(hs) < 3:
        r.err("h2-h3", f"заголовків H2/H3 у тексті {len(hs)} — потрібна структура з ключів (≥3)")
    if hs:
        with_gz = [h for h in hs if has_gz(h, allow_syn=True)]
        share = len(with_gz) / len(hs)
        (r.ok if share >= HEADINGS_WITH_GZ else r.err)(
            "h2-h3", f"ГЗ/синонім у {len(with_gz)}/{len(hs)} заголовків ({share:.0%}, треба ≥{HEADINGS_WITH_GZ:.0%})")
        generic = [h for h in hs if not has_gz(h, allow_syn=True) and not any(contains(h, k) for k in keys)]
        if generic:
            r.warn("h2-h3", "заголовки без ГЗ і без ключа: " + "; ".join(generic))
        # The model without its brand («N2 Extreme») is allowed in one heading at most.
        short_only = [h for h in hs if not contains(h, gz) and has_gz(h)]
        if len(short_only) > 1:
            r.err("h2-h3", f"ГЗ без бренду/версії у {len(short_only)} заголовках (максимум 1): " + "; ".join(short_only))
        key_heads = Counter(k for k in keys for h in hs if contains(h, k))
        twice = [k for k, c in key_heads.items() if c > 1]
        if twice:
            r.warn("h2-h3", "ключ у кількох заголовках (до одного разу): " + ", ".join(twice))

    # Every H2 section carries the full main query in its own text.
    for head_text, sec_text in z.get("h2_sections", []):
        if not contains(sec_text, gz):
            r.err("sections", f"у тексті розділу «{head_text}» немає повного ГЗ «{gz}»")
    # The page's own specs block is the «Характеристики» section; the text must not repeat it.
    for h in [x for x, _ in z.get("h2_sections", [])]:
        if re.search(r"характеристик", h, re.IGNORECASE):
            r.err("h2-h3", f"заголовок «{h}» дублює блок характеристик — назви розділ «Огляд …»")
    if z.get("brand_links"):
        r.err("links", "посилання на бренд у тексті — він уже є в хлібних крихтах і блоці «Купити»")
    if z.get("first_sentence_link"):
        r.err("first-paragraph", "посилання в першому реченні — ставимо його з другого речення")

    # First paragraph
    paras = z["paragraphs"]
    if paras:
        fp = paras[0]
        n = len(words(fp))
        if not FIRST_PARA_MIN - 5 <= n <= FIRST_PARA_MAX + 5:
            r.err("first-paragraph", f"{n} слів (треба {FIRST_PARA_MIN}–{FIRST_PARA_MAX})")
        elif not FIRST_PARA_MIN <= n <= FIRST_PARA_MAX:
            r.warn("first-paragraph", f"{n} слів (треба {FIRST_PARA_MIN}–{FIRST_PARA_MAX})")
        c = phrase_count(fp, gz) + (phrase_count(fp, gz_short) if gz_short and gz_short.lower() not in gz.lower() else 0)
        if c == 0 and not has_gz(fp):
            r.err("first-paragraph", "немає ГЗ")
        elif c > 2:
            r.warn("first-paragraph", f"ГЗ {c} рази (допустимо до 2)")
        if not re.search(r"\d", fp):
            r.warn("first-paragraph", "немає жодної цифри — головні параметри не названі")
        fp_freq = Counter(w for w in stems(fp) if w not in STOP_WORDS and w not in stems(gz))
        rep = [w for w, c in fp_freq.items() if c > 1 and len(w) > 3]
        if rep:
            r.warn("first-paragraph", "слова повторюються (бажано по разу): " + ", ".join(rep))
        # Last paragraph
        lp = paras[-1]
        if z.get("last_is_list"):
            r.err("last-paragraph", "текст закінчується списком, а має закінчуватися висновком-абзацом")
        if len(paras) > 1 and not has_gz(lp):
            r.err("last-paragraph", "в останньому абзаці немає ГЗ")
        for p in paras:
            if len(words(p)) > PARA_MAX_WORDS:
                r.warn("paragraphs", f"абзац {len(words(p))} слів (>{PARA_MAX_WORDS} ≈ 6 рядків): «{p[:60]}…»")
    else:
        r.err("first-paragraph", "немає абзаців тексту")

    for s in z.get("sections", []):
        if len(words(s)) > SECTION_MAX_WORDS and len(z.get("sections", [])) > 1:
            r.warn("sections", f"розділ {len(words(s))} слів (>{SECTION_MAX_WORDS} ≈ 12 рядків): «{s[:50]}…»")

    sentences = [s for s in SENTENCE_RE.split(re.sub(r"\s+", " ", " ".join(paras))) if words(s)]
    if sentences:
        avg = sum(len(words(s)) for s in sentences) / len(sentences)
        stats["avg_sentence"] = round(avg, 1)
        long = [s for s in sentences if len(words(s)) > 22]
        if avg > SENTENCE_WARN:
            r.warn("sentences", f"середнє речення {avg:.1f} слова (ціль 6–{SENTENCE_TARGET})")
        if long:
            r.warn("sentences", f"{len(long)} речень довших за 22 слова")

    # Table: on the page it is the specs block built from characteristics.json, so a draft
    # (the description text) must not carry its own copy of the specs.
    if z.get("mode") == "draft":
        if z["tables"]:
            r.warn("table", "таблиця в описі дублює блок характеристик сторінки — характеристики живуть у characteristics.json")
    elif not z["tables"]:
        r.err("table", "немає таблиці (блок характеристик)")
    else:
        heads = [t["head"] for t in z["tables"]]
        if not any(has_gz(h) for h in heads):
            r.err("table", f"у шапці таблиці немає ГЗ (або скороченого ГЗ «{gz_short or gz}»): {heads}")

    # Lists
    if not z["lists"]:
        r.err("list", "немає маркованого/нумерованого списку")
    else:
        headed = [h for h in z["list_headers"] if h and has_gz(h)]
        if not headed:
            r.err("list", "над жодним списком немає речення-шапки з ГЗ і двокрапкою в кінці")

    # Alts
    alts = [a for a in z["alts"] if a is not None]
    if not alts:
        r.warn("alts", "альтів не передано")
    # Format agreed with the SEO specialist: «<ГЗ> фото N», nothing else.
    alt_re = re.compile(r"^" + re.escape(" ".join(gz.split())) + r" фото \d+$", re.IGNORECASE)
    for a in alts:
        if not alt_re.match(" ".join(a.split())):
            r.err("alts", f"«{a}»: треба рівно «{gz} фото N»")
    if len(set(" ".join(a.lower().split()) for a in alts)) < len(alts):
        r.err("alts", "однакові альти в різних фото — у кожного свій номер")

    # Density
    if total < 150:
        r.warn("density", f"текст {total} слів — відсотки на такому обсязі грубі")
    gz_stats = {}
    for w in gz_words:
        # «TT-X», «M&P40» split into several tokens: count the whole model word as a phrase.
        c = phrase_count(body, w) if len(words(w)) > 1 else freq.get(stem(w), 0)
        pct = 100 * c / total if total else 0
        gz_stats[w] = (c, round(pct, 2))
        lo, hi = count_bounds(total, GZ_MIN, GZ_HARD_MAX)
        need = f"{lo}–{hi} (ціль {GZ_MIN}–{GZ_MAX}%, межа {GZ_HARD_MAX}%)"
        if c < lo:
            r.err("density", f"ГЗ-слово «{w}»: {c} ({pct:.1f}%), треба {need}")
        elif c > hi:
            r.err("density", f"ГЗ-слово «{w}»: {c} ({pct:.1f}%) — переспам, треба {need}: {fix_hint(c, total, GZ_HARD_MAX)}")
        else:
            r.ok("density", f"ГЗ-слово «{w}»: {c} ({pct:.1f}%)")
    stats["gz"] = gz_stats

    core_stems = set(s for w in gz_words for s in stems(w))
    # Other words of the ГЗ (brand, series) have no minimum, only the ГЗ ceiling.
    gz_other = set(s for t in [gz, gz_short] for s in stems(t) if s not in STOP_WORDS) - core_stems
    syn_stems = set(s for t in main_syns + (core.get("variant_words") or []) for s in stems(t)
                    if s not in STOP_WORDS)
    key_stems = set(s for t in keys + [s["text"] for s in syns if not s.get("main")] for s in stems(t)
                    if s not in STOP_WORDS)
    lsi_stems = set(s for t in lsi for s in stems(t) if s not in STOP_WORDS)
    for k in keys:
        if not contains(body, k) and not contains_all_words(body, k):
            r.warn("keys", f"ключ «{k}» не використано в тексті")
    # Synonyms of the query's parts paired with the brand («воздушка Magtech», «пугач SUR»): 1–2 times each.
    for ps in core.get("part_synonyms") or []:
        n = phrase_count(body, ps)
        if n == 0:
            r.err("part-synonyms", f"синонім частини ГЗ «{ps}» не використано (треба 1–2 рази)")
        elif n > 3:
            r.warn("part-synonyms", f"синонім частини ГЗ «{ps}»: {n} разів (треба 1–2)")
    for s, c in freq.most_common():
        if s in STOP_WORDS or s in core_stems or len(s) < 2 or s.isdigit():
            continue
        pct = 100 * c / total if total else 0
        if s in gz_other:
            if c > count_bounds(total, GZ_MIN, GZ_HARD_MAX)[1]:
                r.err("density", f"слово ГЗ «{s}…»: {c} ({pct:.1f}%) > {GZ_HARD_MAX}%: {fix_hint(c, total, GZ_HARD_MAX)}")
        elif s in syn_stems:
            if pct > MAIN_SYN_MAX:
                r.err("density", f"головний синонім «{s}…»: {c} ({pct:.1f}%) > {MAIN_SYN_MAX}%: {fix_hint(c, total, MAIN_SYN_MAX)}")
        elif s in key_stems:
            if pct > ANY_WORD_MAX:
                r.err("density", f"ключ «{s}…»: {c} ({pct:.1f}%) > {ANY_WORD_MAX}%")
            elif pct > KEY_MAX + 0.25:
                r.warn("density", f"ключ «{s}…»: {c} ({pct:.1f}%), норма {KEY_MIN}–{KEY_MAX}%")
        elif pct > ANY_WORD_MAX:
            r.err("density", f"звичайне слово «{s}…»: {c} ({pct:.1f}%) > {ANY_WORD_MAX}%: заміни {c - math.floor(total * ANY_WORD_MAX / 100)} повтор. синонімом або займенником")
        elif pct > PLAIN_WORD_MAX and c > 2 and s not in lsi_stems:
            r.warn("density", f"звичайне слово «{s}…»: {c} ({pct:.1f}%) > {PLAIN_WORD_MAX}%")
    stats["top"] = [(w, c, round(100 * c / total, 2)) for w, c in freq.most_common(15)
                    if w not in STOP_WORDS] if total else []
    return r, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--draft", help="Markdown draft with front matter (title, description, h1, alts)")
    src.add_argument("--html", help="built HTML file of a product page")
    src.add_argument("--url", help="live product page URL")
    ap.add_argument("--core", required=True, help="core.json with gz, synonyms, keys")
    ap.add_argument("--code", help="product code (to pick gallery alts out of HTML)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    try:
        core = json.loads(Path(args.core).read_text(encoding="utf-8"))
        if not core.get("gz"):
            raise ValueError("core.json: поле gz обовʼязкове")
        if args.draft:
            z = parse_draft(Path(args.draft))
        else:
            z = parse_html(args.html or args.url, args.code)
    except Exception as exc:  # noqa: BLE001
        print(f"input error: {exc}", file=sys.stderr)
        return 2

    rep, stats = check(z, core)
    errors = [i for i in rep.items if i["level"] == "ERROR"]
    if args.json:
        print(json.dumps({"items": rep.items, "stats": stats, "ok": not errors}, ensure_ascii=False, indent=2))
    else:
        print(f"ГЗ: {core['gz']}   слів у тексті: {stats['words']}   середнє речення: {stats.get('avg_sentence', '-')}")
        for it in rep.items:
            if it["level"] != "OK":
                print(f"{it['level']:5} [{it['zone']}] {it['msg']}")
        print("top:", ", ".join(f"{w} {c} ({p}%)" for w, c, p in stats["top"][:10]))
        print("RESULT:", "OK" if not errors else f"{len(errors)} ERROR")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
