#!/usr/bin/env python3
"""Snapshots of the eknot.me service pages for the seller's knowledge base (stdlib only).

    python3 -I stack/kb/refresh.py           # fetch, compare with stack/kb/pages/, print what changed (exit 1 if any)
    python3 -I stack/kb/refresh.py --write   # same, then overwrite the snapshots

Run it weekly. A changed page is a reason to read the diff (prices, claims), commit the new snapshot and run
`bootstrap.py seller`, which loads stack/group-watcher/services.json and every snapshot here into the base.

Why not Trafilatura: tested 25.11.2026 on these pages, it dropped the price table of online meetings (laid out
with div blocks, not <table>) and the tariff contents of online voting. Here every visible text node of the page
is kept, and what the site repeats on every page (menu, footer, order form) is removed: a line found on most of
the fetched pages is site chrome, not the service.
"""
import datetime, difflib, json, pathlib, re, sys, urllib.request
from html.parser import HTMLParser

HERE = pathlib.Path(__file__).resolve().parent
PAGES = HERE / "pages"
CATALOG = HERE.parent / "group-watcher" / "services.json"
HOST = "eknot.me"
UA = "Mozilla/5.0 (X11; Linux x86_64) sales-bot-kb-refresh"
# <button> stays: the FAQ questions are accordion buttons. Order buttons are dropped as JUNK lines instead.
SKIP = {"script", "style", "noscript", "svg", "template", "head", "nav", "footer", "form", "select"}
JUNK = re.compile(r"^(Заказать|Подключить|Загрузить ещ[её] вопросы|Подать заявку)\b.{0,40}$")
BLOCK = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "br", "section", "article", "ul", "ol",
         "table", "dt", "dd", "span", "a", "label"}
CHROME_SHARE = 0.6  # a line on at least this share of pages is site chrome


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        elif tag in BLOCK:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def lines_of(html):
    p = Text()
    p.feed(html)
    out = []
    for line in "".join(p.out).splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line and not JUNK.match(line) and (not out or out[-1] != line):
            out.append(line)
    return out


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")


def snapshot_body(path):
    """The page text of a stored snapshot, without its header (the date changes on every run)."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    return text.split("\n\n", 1)[1] if "\n\n" in text else ""


def main():
    write = "--write" in sys.argv[1:]
    services = [s for s in json.loads(CATALOG.read_text(encoding="utf-8"))["services"] if HOST in s["url"]]
    pages = {}
    for s in services:
        pages[s["id"]] = lines_of(fetch(s["url"]))
        print(f"  {s['id']:20} {s['url']}: {len(pages[s['id']])} lines")
    seen = {}
    for lines in pages.values():
        for line in set(lines):
            seen[line] = seen.get(line, 0) + 1
    chrome = {line for line, n in seen.items() if n >= max(2, CHROME_SHARE * len(pages))}
    today = datetime.date.today().isoformat()
    changed = 0
    PAGES.mkdir(exist_ok=True)
    for s in services:
        body = "\n".join(line for line in pages[s["id"]] if line not in chrome) + "\n"
        path = PAGES / f"{s['id']}.md"
        old = snapshot_body(path)
        if old == body:
            continue
        changed += 1
        if old is None:
            print(f"NEW  {path.name}: {len(body)} chars")
        else:
            diff = list(difflib.unified_diff(old.splitlines(), body.splitlines(), "stored", "site", n=0, lineterm=""))
            print(f"DIFF {path.name}: {len(diff)} lines")
            print("\n".join(diff[:80]))
        if write:
            path.write_text(f"# {s['name']}\nИсточник: {s['url']}\nПрочитано: {today}\n\n{body}", encoding="utf-8")
    print(f"{changed} page(s) changed, {len(chrome)} chrome line(s) removed"
          + ("; snapshots written: review the diff, commit, then `bootstrap.py seller`" if write and changed else ""))
    sys.exit(1 if changed and not write else 0)


if __name__ == "__main__":
    main()
