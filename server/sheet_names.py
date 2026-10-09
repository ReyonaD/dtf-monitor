"""
Parse the production file-name convention into order code / part / copies / inches.

    "REPRINT - 1PX - PRO9209 (2x) - (1) Don Nguyen LOCAL PICKUP-203INCH.tif"
        -> code PRO9209, copies 2, part 1/1, inch 203", customer "Don Nguyen LOCAL PICKUP"
    "5-1PX - PRO7807 (1-3) Bob-300INCH.png"   -> part 1 of 3

Single source of truth for the queue endpoints. The agent UI has a JS twin
(parseFile in agent/ui_preview/index.html) — keep the two in sync.
"""
import re

_CODE_WITH_COPIES = re.compile(r"([A-Za-z]{1,4}\d+)\s*\(\d+\s*[xX]?\)")
_CODE_PLAIN = re.compile(r"\b([A-Za-z]{1,4}\d{3,})\b")
_COPIES = re.compile(r"\((\d+)\s*[xX]\)")
_PART = re.compile(r"\((\d+)\s*-\s*(\d+)\)|(?<![\w/])(\d+)\s*/\s*(\d+)(?![\w/])")
_INCH = re.compile(r"-(\d+)\s*INCH", re.I)
_URGENT = re.compile(r"^\s*\+\+(?!\+)")  # "++" at the very start of the name = priority order
_RUSH = re.compile(r"^\s*\+\+\+")       # "+++" = RUSH: above urgent, printed before everything else
_REPRINT = re.compile(r"reprint", re.I)  # "REPRINT" anywhere in the name = re-run of a printed sheet


def has_part(s: str) -> bool:
    return bool(_PART.search(s or ""))


def parse_sheet_name(name: str) -> dict:
    m = _CODE_WITH_COPIES.search(name) or _CODE_PLAIN.search(name)
    code = m.group(1).upper() if m else ""
    cm = _COPIES.search(name)
    copies = max(1, int(cm.group(1))) if cm else 1
    part, total = 1, 1
    pm = _PART.search(name)
    if pm:
        a = int(pm.group(1) or pm.group(3))
        b = int(pm.group(2) or pm.group(4))
        # "(a-b)": the smaller number is the part — never part > total
        part, total = (a, b) if a <= b else (b, a)
    im = _INCH.search(name)
    inch = (im.group(1) + '"') if im else ""
    return {"code": code, "part": part, "total": total, "copies": copies, "inch": inch,
            "cust": customer_of(name), "urgent": bool(_URGENT.match(name)),
            "rush": bool(_RUSH.match(name)), "reprint": bool(_REPRINT.search(name)),
            "label": display_name(name)}


def display_name(name: str) -> str:
    """The file name minus the parts shown elsewhere (order code, (a-b) part, (Nx) copies,
    -NNNINCH, +/++/+++ prefix, extension) — so any note someone typed into the name
    ("5-1PX - … Bob", "REPRINT - …", "…LOCAL PICKUP") stays visible in the lists."""
    s = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name or "")
    s = re.sub(r"^\s*\+{1,3}\s*", "", s)
    m = _CODE_WITH_COPIES.search(s)
    if m:
        s = s[:m.start()] + " " + s[m.end():]
    else:
        s = _CODE_PLAIN.sub(" ", s, count=1)
    s = re.sub(r"\(\d+\s*-\s*\d+\)", " ", s)
    s = re.sub(r"\(\d+\)", " ", s)
    s = re.sub(r"-?\s*\d+\s*INCH", " ", s, flags=re.I)
    s = re.sub(r"(\s*[-–]\s*){2,}", " - ", s)      # "a -  - b" → "a - b"
    s = re.sub(r"\s{2,}", " ", s).strip(" -–")
    return s


def customer_of(name: str) -> str:
    after = name
    cut = re.search(r"\(\d+\s*-\s*\d+\)", name)
    if cut:
        after = re.sub(r"^\(\d+\s*-\s*\d+\)\s*", "", name[cut.start():])
    else:
        c2 = re.search(r"\(\d+\s*[xX]?\)", name)
        if c2:
            after = re.sub(r"^\(\d+\s*[xX]?\)\s*[-–]?\s*", "", name[c2.start():])
    after = re.sub(r"^\(\d+\)\s*[-–]?\s*", "", after)  # "(2x) - (1) Name" -> "Name"
    after = re.sub(r"^\s*\+\+\s*", "", after)
    after = re.sub(r"-\d+\s*INCH.*$", "", after, flags=re.I)
    after = re.sub(r"\.[a-z]+$", "", after, flags=re.I)
    return re.sub(r"^[\s-]+", "", after).strip()
