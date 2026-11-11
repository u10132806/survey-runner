"""Submit a randomised response to the 2026 Hypertext Mobile Survey, and log it.

Every answer is picked at random from the form's own options (taken from
survey-form.html), except:
  * email and phone number, which come from the command line
  * the newsletter and survey-database questions, which are always "No"

Every real submission appends one row to a CSV file:

    firstname,lastname,email,phone,result

result is "ok", or "fail: <reason>".  --dry-run sends nothing and logs nothing.

An email that already has an "ok" row in the CSV is not submitted again; the
attempt is logged as "fail: already entered" and the script exits 1.

Usage:
    python fill_survey.py someone@example.com 0821234567 --dry-run
    python fill_survey.py someone@example.com 0821234567
    python fill_survey.py someone@example.com 0821234567 --csv runs.csv \
        --form-url https://docs.google.com/forms/d/e/TEST_COPY_ID/formResponse

Requires Python 3.10+.
"""

import argparse
import csv
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html import unescape

FORM_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSfbW4oYkwo3a-iZdCq20DUOZLI1cDYNQdd6-cKfFzSIK68_Kw/formResponse"
)

CONFIRMATION = "Your response has been recorded"
CSV_COLUMNS = ["firstname", "lastname", "email", "phone", "result"]

APOS = "’"  # curly apostrophe used in the form's option text

FIRST_NAME = "204616118"
SURNAME = "1506567978"
EMAIL = "1963280990"
PHONE = "1201892108"
TEXT_FIELDS = [FIRST_NAME, SURNAME, EMAIL, PHONE]

FIRST_NAMES = [
    "James", "Thabo", "Sipho", "Lerato", "Naledi", "Ayanda", "Michael", "Sarah", "Jessica",
    "Daniel", "Pieter", "Johan", "Anika", "Zanele", "Kagiso", "Bongani", "Nomvula", "Lindiwe",
    "Tshepo", "Refilwe", "Priya", "Ravi", "Aisha", "Yusuf", "David", "Emma", "Liam", "Olivia",
    "Mpho", "Karabo", "Lwazi", "Busisiwe", "Chloe", "Ethan", "Megan", "Ruan", "Thandeka",
]

SURNAMES = [
    "Nkosi", "Dlamini", "Mokoena", "Khumalo", "Ndlovu", "Botha", "van der Merwe", "Smith",
    "Naidoo", "Pillay", "Mahlangu", "Zulu", "Mthembu", "Pretorius", "Jacobs", "Williams",
    "Molefe", "Sithole", "Venter", "du Plessis", "Govender", "Petersen", "Mabaso", "Coetzee",
    "Nel", "Hendricks", "Radebe", "Maseko", "Ngcobo", "Baloyi", "Steyn", "Fourie", "Adams",
]

# Always answered "No".
STATIC = {
    "1832631779": "No",  # daily newsletter mailing list
    "939666246": "No",   # survey database
}

BRANDS = ["Apple", "HONOR", "Huawei", "OPPO", "Redmi", "Samsung", "TECNO", "vivo", "Xiaomi"]
DURATIONS = ["Less than a year", "1-5 years", "6-10 years", "11-15 years", "15+ years"]

# Choice questions in form order: entry id -> (options, min picks, max picks).
# min == max == 1 for single-choice questions.
CHOICES = {
    "142676809": (BRANDS + ["Other"], 1, 1),
    "684694615": ([
        "Outright purchase (new phone)",
        "Outright purchase (used phone)",
        "On contract through a service provider",
        "Via a loan or monthly repayment service",
        "Provided by your work or school",
        "Provided by your parents or other caretakers/Received as a gift",
    ], 1, 1),
    "1914367338": ([
        "Tablet", "Laptop", "Smartwatch",
        "Gaming handheld (Nintendo Switch, Steam Deck, etc.)", "Other",
    ], 1, 3),
    "2004097656": ([
        "Brand", "Price", "Camera",
        "Anything contributing to performance (Processor, RAM, storage, speed etc.)",
        "Incentives (Launch discount, free devices, extra warranty etc.)",
        "Screen size / Overall size", "Storage size", "Durability",
        "Operating system/ecosystem", "Other",
    ], 1, 1),
    "848376734": ([
        "Entertainment", "Mobile gaming", "Internet connectivity (E.g. hotspot)",
        "Browsing the web",
        "Videoconferencing (E.g. Video calls on Microsoft Teams or Zoom)",
        "Reading the news", "Accessing social media", "Listening/Streaming music",
        "Creating documents for work", "Capturing images and video", "Accessing AI features",
    ], 3, 3),  # says "up to 3" but the form validates "exactly 3"
    "819168026": ([
        "Lower pricing", "More durable hinge", "More durable screen",
        "More dust/water resistance",
        "Manufacturer warranty on the hinge/screen/dust/ water",
        "Better availability/more options",
    ], 1, 1),
    "135634943": (BRANDS, 1, 1),
    "1112282583": ([
        "Tech publications/websites", "YouTube tech reviewers", "TikTok creators",
        "Instagram creators", "Online customer reviews",
        "Network store staff (Vodacom, MTN, Cell C, Telkom, etc.)", "Retail store staff",
        "The brand's own website or social media", "Business/lifestyle media",
        "Celebrities/brand ambassadors", "Online forums/communities",
        "In-store demonstrations", "Other",
    ], 3, 3),
    "1954221682": ([f"The phone{APOS}s internal storage", "A memory card", "Cloud storage"], 1, 1),
    "570648959": (["Screen protector", "Case", "Back grip/kickstand", "Card holder/wallet", "Other"], 1, 3),
    "2093632993": (["1 year", "2 years", "3 years", "4 years", "5 years", "6 or more years"], 1, 1),
    "1808224398": ([
        # "Nothing. I would already consider HONOR" and "I would not consider HONOR at a
        # premium price" are left out: they contradict picking two other reasons.
        "Independent reviews comparing to Apple or Samsung", "Camera (photo/video) comparisons",
        "Battery/performance tests", "Durability tests", "Seeing and using the device in-store",
        "Recommendations from tech experts", "Recommendations from friends/family",
        "Seeing more people using HONOR", "Strong warranty/service guarantees",
        "Long-term software update commitments", "Trade-in/resale programme",
        "Attractive network contracts", "Evidence of compatibility with my current devices",
        "Awards from respected tech publications/organisations", "Other",
    ], 3, 3),
    "278757525": (BRANDS, 3, 3),
    "961022566": ([
        "Cell C", "MTN", "Telkom", "Vodacom", "Rain", "Capitec Connect", "FNB Connect",
        "Standard Bank Connect", "Nedbank Connect", "Old Mutual Connect", "Melon Mobile", "Other",
    ], 1, 1),
    "1751510711": ([
        "Overall pricing", "Data provision", "Call and SMS provision",
        "A promotion ( Reduced fees, extra data, an included phone, etc.)",
        "Legacy subscription (Your parents started the contract for you)",
        "Good word of mouth and/or marketing",
    ], 1, 1),
    "1870797998": ([str(n) for n in range(1, 11)], 1, 1),
    "840246573": (DURATIONS, 1, 1),   # how long you've had your number
    "1521783566": (DURATIONS, 1, 1),  # how long with your provider (capped below)
    "1412269022": (["Android", "iOS", "Huawei Mobile Services (HMS)", "Other"], 1, 1),
    "691993297": (["Yes", "No"], 1, 1),
    "244122526": ([
        "Document summaries", "Writing emails/messages", "Generating images or video",
        "Editing photos or videos", "Live language translation",
        "Searching or browsing the internet", "Asking questions",
    ], 1, 3),  # "Never used before" is handled separately below
    "1397880172": (["Samsung", "HONOR", "OPPO", "Apple", "vivo", "Xiaomi", f"I{APOS}m not sure"], 1, 1),
    "984335096": ([
        "The cost of data", "The cost of devices",
        "More free resources (Access to zero-rated sites for education)",
        "The length of contracts", "Difficulty cancelling contracts",
        "A lack of super apps (One app that can aggregate all the tasks, services, and offerings from your network)",
    ], 1, 1),
}


def pick(options: list[str], lo: int, hi: int) -> str | list[str]:
    if lo == hi == 1:
        return random.choice(options)
    chosen = set(random.sample(options, random.randint(lo, hi)))
    return [o for o in options if o in chosen]  # keep the form's order


def random_answers(email: str, phone: str) -> dict[str, str | list[str]]:
    answers: dict[str, str | list[str]] = {
        FIRST_NAME: random.choice(FIRST_NAMES),
        SURNAME: random.choice(SURNAMES),
        EMAIL: email,
        PHONE: phone,
        **STATIC,
    }
    for entry_id, (options, lo, hi) in CHOICES.items():
        answers[entry_id] = pick(options, lo, hi)

    # You can't have been with your provider longer than you've had your number.
    number_idx = DURATIONS.index(answers["840246573"])
    answers["1521783566"] = DURATIONS[random.randint(0, number_idx)]

    # Some people have never used on-device AI; that answer stands alone.
    if random.random() < 0.15:
        answers["244122526"] = ["Never used before"]

    return answers


def build_pairs(answers: dict[str, str | list[str]]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for entry_id, value in answers.items():
        for v in value if isinstance(value, list) else [value]:
            pairs.append((f"entry.{entry_id}", v))

    # Choice questions (everything except the text fields) carry a sentinel field.
    for entry_id in answers:
        if entry_id not in TEXT_FIELDS:
            pairs.append((f"entry.{entry_id}_sentinel", ""))

    fbzx = "".join(str(random.randint(0, 9)) for _ in range(19))
    pairs += [
        ("fvv", "1"),
        ("partialResponse", f'[null,null,"{fbzx}"]'),
        ("pageHistory", "0"),
        ("fbzx", fbzx),
        ("submissionTimestamp", str(int(time.time() * 1000))),
    ]
    return pairs


# --------------------------------------------------------------------------- #
# CSV logging
# --------------------------------------------------------------------------- #

def _clean(value, limit: int = 300) -> str:
    """Collapse whitespace so one submission is always one physical CSV line."""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


def already_entered(path, email: str) -> bool:
    """True if the CSV already holds an entered row for this email.

    Columns are read by position (firstname, lastname, email, phone, result),
    not by header name, so an older or mismatched header line can't break it.
    A row counts as entered if its result is "ok", or if it has no result
    column at all (rows from the older 4-column format).  "fail: ..." rows
    don't count, so a network error can still be retried.
    Emails are compared case-insensitively, ignoring surrounding whitespace.
    """
    if not os.path.exists(path):
        return False
    target = email.strip().lower()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if len(row) < 3 or row[2].strip().lower() != target:
                continue
            result = row[4].strip() if len(row) > 4 else ""
            if result in ("", "ok"):
                return True
    return False


def log_submission(path, firstname, lastname, email, phone, result) -> None:
    """Append one row, writing the header only if the file is new or empty."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    row = [_clean(v) for v in (firstname, lastname, email, phone, result)]
    needs_header = (not os.path.exists(path)) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if needs_header:
            writer.writerow(CSV_COLUMNS)
        writer.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())


# --------------------------------------------------------------------------- #
# Submission
# --------------------------------------------------------------------------- #

# Google's rejection-page markup varies; these are best-effort patterns.  Use
# --save-errors to capture a real rejection and add whatever pattern it needs.
_ERROR_PATTERNS = [
    r'class="[^"]*ErrorMessage[^"]*"[^>]*>(.*?)</',
    r'data-error-message="([^"]+)"',
    r'role="alert"[^>]*>(.*?)</div>',
]


def extract_error(body: str) -> str:
    for pattern in _ERROR_PATTERNS:
        m = re.search(pattern, body, re.S | re.I)
        if m:
            text = unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                return text
    return ""


def submit(pairs, form_url: str = FORM_URL, timeout: float = 30) -> tuple[bool, str, str]:
    """POST the payload.  Return (ok, reason, body); never raise on expected failures."""
    body = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote).encode("utf-8")
    req = urllib.request.Request(
        form_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:159.0) "
                          "Gecko/20100101 Firefox/159.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            content = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:   # must precede URLError: HTTPError subclasses it
        detail = e.read().decode("utf-8", errors="replace")
        reason = extract_error(detail) or "no error text in response"
        return False, f"HTTP {e.code} {e.reason}: {reason}", detail
    except urllib.error.URLError as e:
        return False, f"network error: {e.reason}", ""
    except TimeoutError:
        return False, f"timed out after {timeout}s", ""

    if CONFIRMATION in content:
        return True, "", content
    return False, f"HTTP {status} but no confirmation message", content


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Submit a randomised 2026 Hypertext Mobile Survey response.")
    parser.add_argument("email", help="email address for the survey's email field")
    parser.add_argument("phone", help="daytime contact number for the survey's phone field")
    parser.add_argument("--dry-run", action="store_true", help="print the payload, send nothing")
    parser.add_argument("--csv", default="submissions.csv", help="append one row per submission")
    parser.add_argument("--form-url", default=FORM_URL, help="override the target (e.g. a test copy)")
    parser.add_argument("--timeout", type=float, default=30, help="socket timeout in seconds")
    parser.add_argument("--save-errors", metavar="DIR", help="dump failed response HTML here")
    args = parser.parse_args(argv)

    answers = random_answers(args.email, args.phone)
    pairs = build_pairs(answers)

    if already_entered(args.csv, args.email):
        if not args.dry_run:
            log_submission(
                args.csv, answers[FIRST_NAME], answers[SURNAME], args.email, args.phone,
                "fail: already entered",
            )
        print(f"Not submitted - already entered ({args.email} is in {args.csv})")
        return 1

    if args.dry_run:
        for k, v in pairs:
            print(f"{k}={v}")
        return 0

    ok, reason, content = submit(pairs, args.form_url, args.timeout)

    log_submission(
        args.csv, answers[FIRST_NAME], answers[SURNAME], args.email, args.phone,
        "ok" if ok else f"fail: {reason}",
    )

    if not ok and args.save_errors and content:
        os.makedirs(args.save_errors, exist_ok=True)
        name = f"error-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.html"
        dump = os.path.join(args.save_errors, name)
        with open(dump, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Response body saved to {dump}")

    print("Submitted: response recorded." if ok else f"Not submitted - {reason}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())