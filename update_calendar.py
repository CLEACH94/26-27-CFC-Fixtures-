from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests


# ============================================================
# CARTERTON FC AUTOMATIC FIXTURE CALENDAR
# Source: FA Full-Time
# ============================================================

TEAM_NAME = "Carterton"
TEAM_ID = "200955811"
LEAGUE_ID = "646734134"

TEAM_URL = (
    f"https://fulltime.thefa.com/displayTeam.html?id={TEAM_ID}"
)

LEAGUE_URL = (
    f"https://fulltime.thefa.com/fixtures.html?league={LEAGUE_ID}"
)

OUTPUT_FILE = Path("fixtures.ics")
STATUS_FILE = Path("status.json")

UK_TIME = ZoneInfo("Europe/London")

REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# ============================================================
# TEXT HELPERS
# ============================================================

def clean(value) -> str:
    if value is None:
        return ""

    text = str(value)

    if text.lower() in {"nan", "none"}:
        return ""

    return " ".join(text.split()).strip()


def normalise(value: str) -> str:
    return clean(value).casefold()


def is_carterton(value: str) -> bool:
    """
    Allow small variations such as:
    Carterton
    Carterton FC
    Carterton First
    """
    text = normalise(value)

    return (
        text == "carterton"
        or text.startswith("carterton ")
    )


def ics_escape(value: str) -> str:
    value = clean(value)

    return (
        value
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold_ics_line(line: str, limit: int = 73) -> list[str]:
    if len(line.encode("utf-8")) <= limit:
        return [line]

    output = []
    current = ""

    for character in line:
        candidate = current + character

        if len(candidate.encode("utf-8")) > limit:
            output.append(current)
            current = " " + character
        else:
            current = candidate

    if current:
        output.append(current)

    return output


# ============================================================
# DATES
# ============================================================

def parse_fixture_datetime(value: str) -> datetime:
    """
    FA currently uses DD/MM/YY HH:MM.
    A few fallback formats are included in case that changes slightly.
    """
    value = clean(value)

    formats = (
        "%d/%m/%y %H:%M",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y",
        "%d/%m/%Y",
    )

    for date_format in formats:
        try:
            parsed = datetime.strptime(value, date_format)

            if "%H" not in date_format:
                parsed = parsed.replace(hour=15, minute=0)

            return parsed.replace(tzinfo=UK_TIME)

        except ValueError:
            continue

    raise ValueError(f"Could not understand fixture date: {value}")


# ============================================================
# STABLE CALENDAR IDS
# ============================================================

def stable_uid(competition: str, home: str, away: str) -> str:
    """
    Date/time is intentionally NOT part of the UID.

    If a game moves from Saturday 3pm to Tuesday 7:45pm,
    calendar apps should update the existing event rather
    than create a duplicate.
    """

    raw = "|".join(
        [
            TEAM_ID,
            normalise(competition),
            normalise(home),
            normalise(away),
        ]
    )

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:24]

    return f"{digest}@carterton-fc-fixtures"


# ============================================================
# DOWNLOAD FA FULL-TIME
# ============================================================

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    return session


def download_url(session: requests.Session, url: str) -> str:
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):

        print(
            f"Request attempt {attempt}/{MAX_ATTEMPTS}: {url}"
        )

        try:
            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            print(
                f"HTTP status: {response.status_code}"
            )

            if response.status_code == 403:
                raise RuntimeError(
                    "FA Full-Time returned HTTP 403 Forbidden."
                )

            response.raise_for_status()

            page = response.text

            if len(page) < 3000:
                raise RuntimeError(
                    "FA returned an unexpectedly small webpage."
                )

            if "full-time" not in page.casefold():
                raise RuntimeError(
                    "Returned webpage does not appear to be FA Full-Time."
                )

            return page

        except Exception as exc:
            last_error = exc

            print(f"Attempt failed: {exc}")

            if attempt < MAX_ATTEMPTS:
                time.sleep(3 * attempt)

    raise RuntimeError(
        f"Unable to download {url}: {last_error}"
    )


def download_full_time_pages() -> list[tuple[str, str]]:
    """
    Try multiple public FA Full-Time pages.

    If one source is temporarily unavailable, the other
    can still be used.
    """

    session = make_session()

    # Establish a normal session first.
    try:
        session.get(
            "https://fulltime.thefa.com/",
            timeout=10,
        )
    except Exception:
        pass

    sources = []

    for source_name, url in (
        ("Carterton team page", TEAM_URL),
        ("Hellenic League fixtures page", LEAGUE_URL),
    ):

        try:
            page = download_url(session, url)

            if TEAM_NAME.casefold() in page.casefold():
                sources.append(
                    (source_name, page)
                )
                print(
                    f"Successfully downloaded: {source_name}"
                )
            else:
                print(
                    f"{source_name} downloaded but Carterton "
                    "was not found in the response."
                )

        except Exception as exc:
            print(
                f"{source_name} unavailable: {exc}"
            )

    if not sources:
        raise RuntimeError(
            "All FA Full-Time sources failed. "
            "The existing calendar has NOT been changed."
        )

    return sources


# ============================================================
# TABLE HELPERS
# ============================================================

def flatten_columns(table: pd.DataFrame) -> pd.DataFrame:

    if isinstance(table.columns, pd.MultiIndex):
        table.columns = [
            " ".join(
                clean(part)
                for part in column
                if clean(part)
            )
            for column in table.columns
        ]
    else:
        table.columns = [
            clean(column)
            for column in table.columns
        ]

    return table


def find_column(columns, choices):

    for column in columns:

        column_text = normalise(column)

        for choice in choices:

            if column_text == normalise(choice):
                return column

    return None


def column_contains(columns, word: str):

    target = normalise(word)

    for column in columns:

        if target in normalise(column):
            return column

    return None


# ============================================================
# EXTRACT FIXTURES FROM TABLES
# ============================================================

def extract_from_table(
    table: pd.DataFrame,
) -> list[dict]:

    table = flatten_columns(table)

    columns = list(table.columns)

    date_col = (
        find_column(
            columns,
            (
                "Date / Time",
                "Date/Time",
                "Date",
            ),
        )
        or column_contains(columns, "date")
    )

    home_col = (
        find_column(
            columns,
            (
                "Home Team",
                "Home",
            ),
        )
        or column_contains(columns, "home")
    )

    away_col = (
        find_column(
            columns,
            (
                "Away Team",
                "Away",
            ),
        )
        or column_contains(columns, "away")
    )

    venue_col = (
        find_column(
            columns,
            ("Venue",),
        )
        or column_contains(columns, "venue")
    )

    competition_col = (
        find_column(
            columns,
            (
                "Type",
                "Competition",
            ),
        )
        or column_contains(columns, "type")
        or column_contains(columns, "competition")
    )

    if not date_col or not home_col or not away_col:
        return []

    fixtures = []

    now_uk = datetime.now(UK_TIME)

    for _, row in table.iterrows():

        date_text = clean(row.get(date_col, ""))
        home = clean(row.get(home_col, ""))
        away = clean(row.get(away_col, ""))

        if not home or not away:
            continue

        if not (
            is_carterton(home)
            or is_carterton(away)
        ):
            continue

        try:
            fixture_datetime = parse_fixture_datetime(
                date_text
            )
        except ValueError:
            continue

        # Keep today's game around for a few hours after KO.
        if fixture_datetime < (
            now_uk - timedelta(hours=4)
        ):
            continue

        fixtures.append(
            {
                "date_time": fixture_datetime,
                "home": home,
                "away": away,
                "venue": clean(
                    row.get(venue_col, "")
                ) if venue_col else "",
                "competition": clean(
                    row.get(competition_col, "")
                ) if competition_col else "",
            }
        )

    return fixtures


def extract_fixtures(
    sources: list[tuple[str, str]],
) -> tuple[list[dict], str]:

    all_candidates = []

    source_names = []

    for source_name, page in sources:

        print(
            f"Reading tables from: {source_name}"
        )

        try:
            tables = pd.read_html(
                StringIO(page)
            )

        except Exception as exc:
            print(
                f"Could not parse {source_name}: {exc}"
            )
            continue

        source_fixture_count = 0

        for table in tables:

            fixtures = extract_from_table(
                table
            )

            if fixtures:
                all_candidates.extend(
                    fixtures
                )

                source_fixture_count += len(
                    fixtures
                )

        if source_fixture_count:

            source_names.append(
                source_name
            )

            print(
                f"{source_name}: found "
                f"{source_fixture_count} candidate fixtures."
            )

    if not all_candidates:

        raise RuntimeError(
            "No upcoming Carterton fixtures could be "
            "identified from FA Full-Time. "
            "Existing calendar preserved."
        )

    # ========================================================
    # REMOVE DUPLICATES
    # ========================================================

    unique = {}

    for fixture in all_candidates:

        key = (
            normalise(fixture["competition"]),
            normalise(fixture["home"]),
            normalise(fixture["away"]),
        )

        existing = unique.get(key)

        if existing is None:

            unique[key] = fixture

        else:

            # Prefer the version with more information.

            if (
                not existing["venue"]
                and fixture["venue"]
            ):
                existing["venue"] = fixture[
                    "venue"
                ]

            # If two sources disagree on time, prefer the
            # newest source encountered only if it is plausible.

            if fixture["date_time"]:
                existing["date_time"] = fixture[
                    "date_time"
                ]

    fixtures = list(
        unique.values()
    )

    fixtures.sort(
        key=lambda fixture: fixture[
            "date_time"
        ]
    )

    source_description = ", ".join(
        source_names
    )

    print(
        f"Final validated candidate count: "
        f"{len(fixtures)}"
    )

    return fixtures, source_description


# ============================================================
# VALIDATION
# ============================================================

def validate_fixtures(fixtures: list[dict]) -> None:

    if not fixtures:

        raise RuntimeError(
            "Zero fixtures detected. "
            "Calendar has NOT been changed."
        )

    if len(fixtures) > 100:

        raise RuntimeError(
            "More than 100 fixtures were detected. "
            "This looks wrong, so the existing "
            "calendar has been preserved."
        )

    seen = set()

    for fixture in fixtures:

        home = fixture["home"]
        away = fixture["away"]
        start = fixture["date_time"]

        if not (
            is_carterton(home)
            or is_carterton(away)
        ):

            raise RuntimeError(
                f"Unexpected fixture found: "
                f"{home} v {away}"
            )

        if not isinstance(
            start,
            datetime,
        ):

            raise RuntimeError(
                f"Fixture has invalid date: "
                f"{home} v {away}"
            )

        key = (
            normalise(fixture["competition"]),
            normalise(home),
            normalise(away),
        )

        if key in seen:

            raise RuntimeError(
                f"Duplicate fixture detected: "
                f"{home} v {away}"
            )

        seen.add(key)


# ============================================================
# EXISTING CALENDAR SAFETY
# ============================================================

def existing_event_count() -> int:

    if not OUTPUT_FILE.exists():
        return 0

    try:

        text = OUTPUT_FILE.read_text(
            encoding="utf-8",
            errors="ignore",
        )

        return text.count(
            "BEGIN:VEVENT"
        )

    except Exception:
        return 0


def suspicious_drop_check(
    new_count: int,
) -> None:

    old_count = existing_event_count()

    if old_count < 8:
        return

    # During a season the number will naturally fall,
    # but it should not suddenly collapse.

    minimum_safe = max(
        2,
        int(old_count * 0.35),
    )

    if new_count < minimum_safe:

        raise RuntimeError(
            f"Safety stop: existing calendar has "
            f"{old_count} fixtures but the FA source "
            f"only returned {new_count}. "
            "Calendar preserved."
        )


# ============================================================
# CREATE CALENDAR
# ============================================================

def make_calendar(
    fixtures: list[dict],
) -> str:

    now_utc = datetime.now(
        timezone.utc
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Carterton FC//CFC Fixtures//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Carterton FC Men's Fixtures",
        (
            "X-WR-CALDESC:Automatic Carterton FC "
            "fixture calendar from FA Full-Time"
        ),
        "X-WR-TIMEZONE:Europe/London",

        "BEGIN:VTIMEZONE",
        "TZID:Europe/London",
        "X-LIC-LOCATION:Europe/London",

        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0000",
        "TZOFFSETTO:+0100",
        "TZNAME:BST",
        "DTSTART:19700329T010000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "END:DAYLIGHT",

        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0000",
        "TZNAME:GMT",
        "DTSTART:19701025T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "END:STANDARD",

        "END:VTIMEZONE",
    ]

    for fixture in fixtures:

        competition = clean(
            fixture["competition"]
        )

        home = clean(
            fixture["home"]
        )

        away = clean(
            fixture["away"]
        )

        venue = clean(
            fixture["venue"]
        )

        start = fixture[
            "date_time"
        ]

        # Reserve a 2 hour match slot.
        end = start + timedelta(
            hours=2
        )

        if is_carterton(home):

            opponent = away
            home_away = "Home"

            summary = (
                f"Carterton FC v {opponent}"
            )

        else:

            opponent = home
            home_away = "Away"

            summary = (
                f"{opponent} v Carterton FC"
            )

        description = [
            "Carterton FC Men's First Team",
            f"{home_away} fixture",
        ]

        if competition:
            description.append(
                f"Competition: {competition}"
            )

        description.extend(
            [
                "",
                (
                    "Fixture information automatically "
                    "supplied from FA Full-Time."
                ),
                (
                    "Please check official club channels "
                    "for any very late changes."
                ),
            ]
        )

        description_text = "\\n".join(
            description
        )

        uid = stable_uid(
            competition,
            home,
            away,
        )

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            (
                "DTSTAMP:"
                f"{now_utc.strftime('%Y%m%dT%H%M%SZ')}"
            ),
            (
                "LAST-MODIFIED:"
                f"{now_utc.strftime('%Y%m%dT%H%M%SZ')}"
            ),
            (
                "DTSTART;TZID=Europe/London:"
                f"{start.strftime('%Y%m%dT%H%M%S')}"
            ),
            (
                "DTEND;TZID=Europe/London:"
                f"{end.strftime('%Y%m%dT%H%M%S')}"
            ),
            (
                "SUMMARY:"
                f"{ics_escape(summary)}"
            ),
            (
                "LOCATION:"
                f"{ics_escape(venue)}"
            ),
            (
                "DESCRIPTION:"
                f"{ics_escape(description_text)}"
            ),
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "END:VEVENT",
        ]

        lines.extend(
            event_lines
        )

    lines.append(
        "END:VCALENDAR"
    )

    folded = []

    for line in lines:
        folded.extend(
            fold_ics_line(line)
        )

    return (
        "\r\n".join(folded)
        + "\r\n"
    )


# ============================================================
# CALENDAR VALIDATION
# ============================================================

def validate_calendar(
    calendar_text: str,
    expected_events: int,
) -> None:

    if not calendar_text.startswith(
        "BEGIN:VCALENDAR"
    ):

        raise RuntimeError(
            "Generated calendar header is invalid."
        )

    if not calendar_text.rstrip().endswith(
        "END:VCALENDAR"
    ):

        raise RuntimeError(
            "Generated calendar is incomplete."
        )

    event_count = calendar_text.count(
        "BEGIN:VEVENT"
    )

    if event_count != expected_events:

        raise RuntimeError(
            f"Expected {expected_events} events "
            f"but generated {event_count}."
        )

    if "Carterton" not in calendar_text:

        raise RuntimeError(
            "Generated calendar does not contain Carterton."
        )

    if "DTSTART" not in calendar_text:

        raise RuntimeError(
            "Generated calendar contains no fixture times."
        )


# ============================================================
# SAFE FILE REPLACEMENT
# ============================================================

def safely_publish(
    calendar_text: str,
) -> None:

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        delete=False,
        dir=".",
        suffix=".tmp",
    ) as temporary:

        temporary.write(
            calendar_text
        )

        temporary_path = Path(
            temporary.name
        )

    try:

        verification = (
            temporary_path.read_text(
                encoding="utf-8",
                errors="strict",
            )
        )

        if (
            "BEGIN:VCALENDAR"
            not in verification
        ):

            raise RuntimeError(
                "Temporary calendar failed validation."
            )

        os.replace(
            temporary_path,
            OUTPUT_FILE,
        )

    finally:

        if temporary_path.exists():

            temporary_path.unlink()


# ============================================================
# STATUS FILE
# ============================================================

def write_status(
    fixture_count: int,
    success: bool,
    message: str,
    source: str,
) -> None:

    status = {
        "team": "Carterton FC Men's",
        "team_id": TEAM_ID,
        "source": source,
        "team_url": TEAM_URL,
        "league_url": LEAGUE_URL,
        "last_checked_utc": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
        "success": success,
        "fixture_count": fixture_count,
        "message": message,
    }

    STATUS_FILE.write_text(
        json.dumps(
            status,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# RUN
# ============================================================

def main() -> int:

    print("=" * 60)
    print(
        "CARTERTON FC AUTOMATIC FIXTURE CALENDAR"
    )
    print("=" * 60)

    try:

        sources = (
            download_full_time_pages()
        )

        fixtures, source_description = (
            extract_fixtures(
                sources
            )
        )

        validate_fixtures(
            fixtures
        )

        suspicious_drop_check(
            len(fixtures)
        )

        calendar_text = make_calendar(
            fixtures
        )

        validate_calendar(
            calendar_text,
            len(fixtures),
        )

        safely_publish(
            calendar_text
        )

        write_status(
            fixture_count=len(fixtures),
            success=True,
            message=(
                "Calendar updated successfully."
            ),
            source=source_description,
        )

        print()
        print("=" * 60)

        print(
            f"SUCCESS: {len(fixtures)} "
            "fixtures published."
        )

        print(
            "fixtures.ics updated safely."
        )

        print(
            f"Source: {source_description}"
        )

        print("=" * 60)

        return 0

    except Exception as exc:

        print()
        print("=" * 60)
        print(
            "UPDATE FAILED SAFELY"
        )
        print("=" * 60)

        print(
            str(exc)
        )

        print()

        print(
            "The existing fixtures.ics file "
            "has NOT been overwritten."
        )

        return 1


if __name__ == "__main__":
    sys.exit(
        main()
    )
