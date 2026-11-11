#!/usr/bin/env python3
"""
fill_survey.py — smoke-test a Google Form copy with a randomly-filled response.

Paste your form link into FORM_LOCATION below. The form it submitted is written
to the CSV on every run.

  --dry-run         print the payload, send nothing
  --audit           validate payload against the live schema, send nothing
  --print-schema    dump discovered fields and exit
  --against-har F   diff payload against a real browser submission in F

Requires Python 3.10+.
"""
from __future__ import annotations

import argparse, csv, datetime as dt, http.cookiejar, json, os, random, re, sys, time
import urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass, field
from html import unescape

# ===========================================================================
# PASTE YOUR FORM LINK HERE (viewform, formResponse, /edit, or a bare form ID)
FORM_LOCATION = "https://docs.google.com/forms/d/e/1FAIpQLSfbW4oYkwo3a-iZdCq20DUOZLI1cDYNQdd6-cKfFzSIK68_Kw/viewform?embedded=true"
# ===========================================================================

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:159.0) Gecko/20100101 Firefox/159.0")

# UNVERIFIED — run --audit; it reports STALE ID for any that aren't on this form.
IDENTITY = {
    "entry.204616118": "first_name", "entry.1506567978": "surname",
    "entry.1963280990": "email",     "entry.1201892108": "phone",
}
FORCED = {"entry.1832631779": "No", "entry.939666246": "No"}
OVERRIDES: dict[str, tuple[int, int]] = {}   # entry id -> (min, max) for checkboxes

FIRST_NAMES = ["herb", "river", "ash", "jun", "kit", "sol", "wren", "nico"]
SURNAMES = ["adventure", "lantern", "meadow", "cobalt", "harbour", "quill"]
FREE_TEXT = ["Automated test response - please ignore.",
             "Test submission from a local smoke-test script."]
HIDDEN = {"fvv", "partialResponse", "pageHistory", "fbzx", "submissionTimestamp"}
CSV_FIELDS = ["timestamp", "form", "email", "phone", "status", "detail", "http_status", "dump"]

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_opener.addheaders = [("User-Agent", UA), ("Accept-Language", "en-US,en;q=0.9")]


def resolve_urls(location: str) -> tuple[str, str]:
    """Any form link -> (viewform URL, formResponse URL)."""
    loc = location.strip()
    if not loc.startswith("http"):
        loc = f"https://docs.google.com/forms/d/e/{loc}"
    path = loc.split("?", 1)[0].rstrip("/")
    for suffix in ("/viewform", "/formResponse", "/edit", "/prefill", "/copy"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return path + "/viewform?embedded=true", path + "/formResponse?embedded=true"


def post_url_from_html(html: str, fallback: str) -> str:
    """The <form action=...> in the markup is authoritative."""
    m = re.search(r'<form[^>]+action="([^"]+)"', html)
    return unescape(m.group(1)) if m else fallback


def fetch_form(view_url: str, timeout: float = 30) -> str:
    with _opener.open(view_url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def hidden_fields(html: str) -> dict[str, str]:
    """Collect fvv / partialResponse / pageHistory / fbzx / submissionTimestamp."""
    out = {}
    for tag in re.findall(r"<input[^>]+type=[\"']hidden[\"'][^>]*>", html, re.I):
        n = re.search(r'name=["\']([^"\']+)["\']', tag)
        v = re.search(r'value=["\']([^"\']*)["\']', tag)
        if n and not n.group(1).startswith("entry."):
            out[n.group(1)] = unescape(v.group(1)) if v else ""
    return out


def item_count(html: str) -> int | None:
    """data-first-entry / data-last-entry from the form tag."""
    a = re.search(r'data-first-entry="(\d+)"', html)
    b = re.search(r'data-last-entry="(\d+)"', html)
    if a and b:
        return int(b.group(1)) - int(a.group(1)) + 1
    return None


def extract_load_data(html: str) -> list:
    i = html.find("FB_PUBLIC_LOAD_DATA_")
    if i < 0:
        raise ValueError("FB_PUBLIC_LOAD_DATA_ absent — form may require sign-in")
    j = html.find("[", i)
    depth, in_str, esc = 0, False, False
    for k in range(j, len(html)):
        c = html[k]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
            continue
        if c == '"': in_str = True
        elif c == "[": depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return json.loads(html[j:k + 1])
    raise ValueError("unbalanced FB_PUBLIC_LOAD_DATA_")


def form_title(html: str, data: list) -> str:
    try:
        if isinstance(data[3], str) and data[3]:
            return data[3]
    except (IndexError, TypeError):
        pass
    m = re.search(r'property="og:title"\s+content="([^"]*)"', html)
    return unescape(m.group(1)) if m else "(unknown)"


def _looks_like_q(x) -> bool:
    return (isinstance(x, list) and len(x) >= 5 and isinstance(x[0], int)
            and isinstance(x[1], (str, type(None))) and isinstance(x[3], int))


def find_questions(data: list) -> list:
    try:
        cand = data[1][1]
        if isinstance(cand, list) and any(_looks_like_q(x) for x in cand):
            return cand
    except (IndexError, TypeError):
        pass
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            if sum(_looks_like_q(x) for x in node) >= 2:
                return node
            stack.extend(x for x in node if isinstance(x, list))
    raise ValueError("could not locate question list")


KINDS = {0: "text", 1: "paragraph", 2: "radio", 3: "dropdown", 4: "checkbox",
         5: "scale", 7: "grid", 9: "date", 10: "time", 13: "upload"}
SKIP = {6, 8, 11, 12}


@dataclass
class Field:
    entry: str
    title: str
    kind: str
    options: list[str] = field(default_factory=list)
    has_other: bool = False
    required: bool = False
    min_select: int = 1
    max_select: int = 1
    bounds_source: str = "default"


def select_counts(title: str, n: int) -> tuple[int, int, str]:
    t = title.lower()
    if m := re.search(r"select\s+up\s+to\s+(\d+)", t):
        return 1, min(int(m.group(1)), n), "title"
    if m := re.search(r"select\s+(?:exactly\s+)?(\d+)", t):
        k = min(int(m.group(1)), n); return k, k, "title"
    if "select all that apply" in t:
        return 1, max(1, n), "title"
    return 1, 1, "default"


def parse_fields(data: list) -> list[Field]:
    out = []
    for q in find_questions(data):
        if not _looks_like_q(q): continue
        qtype, title, spec = q[3], (q[1] or "").strip(), q[4]
        if qtype in SKIP or not spec: continue
        kind = KINDS.get(qtype, "unsupported")
        if qtype == 7:
            for row in spec:
                if not row or row[0] is None: continue
                opts = [o[0] for o in (row[1] or []) if o and o[0] is not None]
                lbl = row[3][0] if len(row) > 3 and isinstance(row[3], list) and row[3] else ""
                out.append(Field(f"entry.{row[0]}", f"{title} — {lbl}".strip(" —"),
                                 "radio", opts, False, bool(row[2]), 1, 1, "grid"))
            continue
        s = spec[0]
        if not s or s[0] is None: continue
        raw = s[1] or []
        opts = [o[0] for o in raw if o and o[0] is not None and not (len(o) > 4 and o[4])]
        has_other = any(len(o) > 4 and o[4] for o in raw)
        req = bool(s[2]) if len(s) > 2 else False
        eid = str(s[0])
        if eid in OVERRIDES:
            lo, hi = OVERRIDES[eid]; src = "override"
        else:
            lo, hi, src = select_counts(title, len(opts) or 1)
        out.append(Field(f"entry.{eid}", title, kind, opts, has_other, req, lo, hi, src))
    return out


def random_answers(fields, email, phone, rng):
    ident = {"email": email, "phone": phone,
             "first_name": rng.choice(FIRST_NAMES), "surname": rng.choice(SURNAMES)}
    pairs = []
    for f in fields:
        if f.entry in FORCED:
            pairs.append((f.entry, FORCED[f.entry])); continue
        if f.entry in IDENTITY:
            pairs.append((f.entry, ident[IDENTITY[f.entry]])); continue
        if f.kind == "upload":
            if f.required: raise SystemExit(f"required upload {f.entry}: remove from test copy")
            continue
        if f.kind in ("text", "paragraph"):
            pairs.append((f.entry, rng.choice(FREE_TEXT)))
        elif f.kind in ("radio", "dropdown", "scale"):
            if not f.options:
                if f.required: raise SystemExit(f"required {f.kind} {f.entry} has no options")
                continue
            if f.has_other and rng.random() < 0.1:
                pairs += [(f.entry, "__other_option__"),
                          (f.entry + ".other_option_response", rng.choice(FREE_TEXT))]
            else:
                pairs.append((f.entry, rng.choice(f.options)))
        elif f.kind == "checkbox":
            if not f.options:
                if f.required: raise SystemExit(f"required checkbox {f.entry} has no options")
                continue
            hi = min(f.max_select, len(f.options)); lo = min(f.min_select, hi)
            for c in rng.sample(f.options, rng.randint(lo, hi)):
                pairs.append((f.entry, c))
        elif f.kind == "date":
            d = dt.date.today() - dt.timedelta(days=rng.randint(0, 3650))
            pairs += [(f.entry + "_year", str(d.year)), (f.entry + "_month", str(d.month)),
                      (f.entry + "_day", str(d.day))]
        elif f.kind == "time":
            pairs += [(f.entry + "_hour", f"{rng.randint(0,23):02d}"),
                      (f.entry + "_minute", f"{rng.randint(0,59):02d}")]
        elif f.required:
            raise SystemExit(f"unsupported required question {f.entry} ({f.title!r})")
    return pairs


def audit(fields, pairs) -> list[str]:
    problems = []
    by_entry = {f.entry: f for f in fields}
    grouped: dict[str, list[str]] = {}
    for k, v in pairs:
        grouped.setdefault(k, []).append(v)
    for stale in list(IDENTITY) + list(FORCED):
        if stale not in by_entry:
            problems.append(f"STALE ID   {stale} not in this form's schema")
    for f in fields:
        vals = grouped.get(f.entry, [])
        if f.required and not vals and f.kind not in ("date", "time", "upload"):
            problems.append(f"MISSING    {f.entry} required, no value ({f.title[:50]!r})")
        if f.kind in ("radio", "dropdown", "scale", "checkbox"):
            for v in vals:
                if v == "__other_option__":
                    if not f.has_other:
                        problems.append(f"BAD OTHER  {f.entry} has no 'Other' option")
                    elif not grouped.get(f.entry + ".other_option_response"):
                        problems.append(f"NO TEXT    {f.entry} __other_option__ without response")
                elif v not in f.options:
                    problems.append(f"BAD VALUE  {f.entry} {v!r} not in options")
        if f.kind == "checkbox" and vals:
            n = len(vals)
            if not (f.min_select <= n <= f.max_select):
                tag = "COUNT" if f.bounds_source == "override" else "COUNT?"
                problems.append(f"{tag:10} {f.entry} selected {n}, "
                                f"bounds {f.min_select}-{f.max_select} ({f.bounds_source})")
        if f.kind in ("text", "paragraph") and f.required and any(not v.strip() for v in vals):
            problems.append(f"EMPTY      {f.entry} required text is blank")
    for k in grouped:
        base = k.split(".other_option_response")[0]
        base = re.sub(r"_(year|month|day|hour|minute)$", "", base)
        if base not in by_entry:
            problems.append(f"UNKNOWN    {k} not in schema")
    return problems


def har_submissions(path: str) -> list[tuple[str, dict[str, list[str]]]]:
    with open(path, encoding="utf-8") as fh:
        har = json.load(fh)
    out = []
    for e in har.get("log", {}).get("entries", []):
        req = e.get("request", {})
        if "/formResponse" not in req.get("url", ""): continue
        pd = req.get("postData", {}) or {}
        grouped: dict[str, list[str]] = {}
        if pd.get("params"):
            for p in pd["params"]:
                grouped.setdefault(p["name"], []).append(p.get("value", ""))
        elif pd.get("text"):
            for k, v in urllib.parse.parse_qsl(pd["text"], keep_blank_values=True):
                grouped.setdefault(k, []).append(v)
        if grouped:
            out.append((req["url"], grouped))
    return out


def diff_against_har(fields, pairs, subs) -> list[str]:
    if not subs:
        return ["NO POST    no formResponse entry with a body found in that HAR"]
    notes = []
    mine: dict[str, list[str]] = {}
    for k, v in pairs: mine.setdefault(k, []).append(v)
    by_entry = {f.entry: f for f in fields}
    for url, theirs in subs:
        t = {k: v for k, v in theirs.items() if k.startswith("entry.")}
        notes.append(f"--- vs {url}  ({len(t)} entry keys)")
        for k in sorted(set(t) - set(mine)):
            req = by_entry[k].required if k in by_entry else "?"
            notes.append(f"  ONLY IN HAR  {k} = {t[k]!r}  required={req}")
        for k in sorted(set(mine) - set(t)):
            notes.append(f"  ONLY IN MINE {k} = {mine[k]!r}")
        for k in sorted(set(t) & set(mine)):
            f = by_entry.get(k)
            if f and f.kind in ("radio", "dropdown", "checkbox", "scale"):
                bad = [v for v in mine[k] if v not in f.options and v != "__other_option__"]
                if bad: notes.append(f"  BAD VALUE    {k} {bad!r}")
            if len(t[k]) != len(mine[k]):
                notes.append(f"  ARITY        {k} har={len(t[k])} mine={len(mine[k])}")
    return notes


def submit(pairs, html, post_url, view_url, timeout=30):
    h = hidden_fields(html)
    missing = HIDDEN - set(h)
    if missing:
        return False, f"hidden fields missing from markup: {sorted(missing)}", 0, html
    fbzx = h["fbzx"]
    payload = list(pairs) + [
        ("fvv", h["fvv"]),
        ("partialResponse", h["partialResponse"] or f'[null,null,"{fbzx}"]'),
        ("pageHistory", h["pageHistory"]),
        ("fbzx", fbzx),
        ("submissionTimestamp", h["submissionTimestamp"]),
    ]
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(post_url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": view_url + (f"&fbzx={fbzx}" if fbzx else ""),
        "Origin": "https://docs.google.com",
    })
    try:
        with _opener.open(req, timeout=timeout) as r:
            status, content = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return False, f"network: {e.reason}", 0, ""
    if "Your response has been recorded" in content or \
       "freebirdFormviewerViewResponseConfirmationMessage" in content:
        return True, "recorded", status, content
    errs = re.findall(r'role="alert"[^>]*>([^<]{3,120})<', content)
    detail = "; ".join(dict.fromkeys(e.strip() for e in errs if e.strip()))
    return False, detail or "not recorded (form re-rendered)", status, content


def already_entered(path, form, email):
    if not os.path.exists(path): return False
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("email") or "").lower() == email.lower() \
                    and row.get("status") == "ok" and row.get("form") == form:
                return True
    return False


def log_run(path, **row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new: w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("email"); p.add_argument("phone")
    p.add_argument("--form-location", default=FORM_LOCATION, help="overrides FORM_LOCATION")
    p.add_argument("--csv", default="runs.csv")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--audit", action="store_true")
    p.add_argument("--against-har", metavar="FILE")
    p.add_argument("--print-schema", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--seed", type=int)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--dump-dir", default="dumps")
    args = p.parse_args()

    rng = random.Random(args.seed)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    view_url, post_url = resolve_urls(args.form_location)

    try:
        html = fetch_form(view_url, args.timeout)
        data = extract_load_data(html)
        fields = parse_fields(data)
    except Exception as e:
        log_run(args.csv, timestamp=now, form=view_url, email=args.email,
                phone=args.phone, status="fail", detail=f"schema: {e}")
        print(f"fail: schema: {e}", file=sys.stderr)
        return 1

    post_url = post_url_from_html(html, post_url)

    if args.print_schema:
        print(f"{form_title(html, data)}\n{view_url}\n")
        n = item_count(html)
        if n is not None:
            print(f"markup reports {n} items; parser found {len(fields)} postable fields")
            if len(fields) < n:
                print("(gap may be section headers/images, or missed questions)")
        print()
        for f in fields:
            print(f"{f.entry}  {f.kind}  required={f.required}  "
                  f"bounds={f.min_select}-{f.max_select}({f.bounds_source})  {f.title[:60]!r}")
            for o in f.options: print(f"    {o!r}")
            if f.has_other: print("    [+ Other]")
        return 0

    pairs = random_answers(fields, args.email, args.phone, rng)

    if args.against_har:
        print("\n".join(diff_against_har(fields, pairs, har_submissions(args.against_har))))
        print()

    if args.audit:
        for k, v in pairs: print(f"{k}={v}")
        print()
        problems = audit(fields, pairs)
        if problems:
            print("AUDIT FAILED:"); [print("  " + x) for x in problems]
            return 1
        print("AUDIT OK — every value is in its question's option set")
        return 0

    if args.dry_run:
        for k, v in pairs: print(f"{k}={v}")
        return 0

    if not args.force and already_entered(args.csv, view_url, args.email):
        log_run(args.csv, timestamp=now, form=view_url, email=args.email, phone=args.phone,
                status="fail", detail="already entered")
        print("fail: already entered", file=sys.stderr)
        return 1

    time.sleep(rng.uniform(0.5, 1.5))
    ok, detail, status, content = submit(pairs, html, post_url, view_url, args.timeout)

    dump = ""
    if not ok and content:
        os.makedirs(args.dump_dir, exist_ok=True)
        dump = os.path.join(args.dump_dir, f"fail-{time.strftime('%Y%m%d-%H%M%S')}.html")
        with open(dump, "w", encoding="utf-8") as fh:
            fh.write(content)

    log_run(args.csv, timestamp=now, form=view_url, email=args.email, phone=args.phone,
            status="ok" if ok else "fail", detail=detail, http_status=status, dump=dump)
    print(f"{'ok' if ok else 'fail'}: {detail}")
    if dump: print(f"response saved to {dump}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())