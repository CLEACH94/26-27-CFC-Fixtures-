from __future__ import annotations

import hashlib
import html
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests


# ============================================================
# CFC FIXTURE CALENDAR
# Automatically generated from FA Full-Time
# ============================================================

TEAM_NAME = "Carterton"
TEAM_ID = "200955811"

FULL_TIME_URL = (
    f"https://fulltime.thefa.com/displayTeam.html?id={TEAM_ID}"
)

OUTPUT_FILE = Path("fixtures.ics")
STATUS_FILE = Path("status.json")

UK_TIME = ZoneInfo("Europe/London")

REQUEST_TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CartertonFCFixtureCalendar/1.0; "
        "+https://github.com/)"
    )
}


# ============================================================
# HELPERS
# ============================================================

def clean(value) -> str:
    """Clean text returned by the webpage."""
    if value is None:
        return ""

    text = str(value)

    if text.lower() == "nan":
        return ""

    return " ".join(text.split()).strip()


def normalise(value: str) -> str:
    """Normalise text for comparisons."""
    return clean(value).casefold()


def ics_escape(value: str) -> str:
    """Escape text safely for an ICS calendar."""
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
    """
    Fold long ICS lines so calendars such as Apple Calendar,
    Google Calendar and Outlook can read them reliably.
    """
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


def stable_uid(competition: str, home: str, away: str) -> str:
    """
    Create a permanent fixture ID.

    IMPORTANT:
    The date is deliberately NOT included.

    If a match is moved to another day or kick-off time,
    calendar apps see it as the SAME fixture and update it
    rather than creating a duplicate.
    """
    raw = "|".join(
        [
            TEAM_ID,
            normalise(competition),
            normalise(home),
            normalise(away),
        ]
    )

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    return f"{digest}@carterton-fc-fixtures"


def find_column(columns, *wanted):
    """Find a dataframe column even if FA changes spacing/capitalisation."""
    for column in columns:
        column_text = normalise(column)

        for target in wanted:
            if normalise(target) == column_text:
                return column

    return None


# ============================================================
# DOWNLOAD FULL-TIME
# ============================================================

def download_full_time_page() -> str:
    print("Downloading FA Full-Time...")

    response = requests.get(
        FULL_TIME_URL,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    page = response.text

    # Basic sanity checks.
    if len(page) < 5000:
        raise RuntimeError(
            "FA Full-Time returned an unexpectedly small page. "
            "Calendar has NOT been changed."
        )

    if TEAM_NAME.casefold() not in page.casefold():
        raise RuntimeError(
            f"Could not confirm {TEAM_NAME} on the FA page. "
            "Calendar has NOT been changed."
        )

    if "Upcoming Fixtures".casefold() not in page.casefold():
        raise RuntimeError(
            "Could not find the Upcoming Fixtures section. "
            "Calendar has NOT been changed."
        )

    print("FA Full-Time downloaded successfully.")

    return page


# ============================================================
# FIND THE FIXTURE TABLE
# ============================================================

def get_fixture_table(page: str) -> pd.DataFrame:
    print("Looking for CFC fixture table...")

    try:
        tables = pd.read_html(StringIO(page))
    except Exception as exc:
        raise RuntimeError(
            f"Could not read tables from FA Full-Time: {exc}"
        ) from exc

    if not tables:
        raise RuntimeError(
            "No tables were found on FA Full-Time. "
            "Calendar has NOT been changed."
        )

    for table in tables:
        # Flatten column names if pandas creates multi-level headings.
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
            table.columns = [clean(column) for column in table.columns]

        columns = list(table.columns)

        date_col = find_column(
            columns,
            "Date / Time",
            "Date/Time",
            "Date",
        )

        home_col = find_column(
            columns,
            "Home Team",
            "Home",
        )

        away_col = find_column(
            columns,
            "Away Team",
            "Away",
        )

        venue_col = find_column(
            columns,
            "Venue",
        )

        type_col = find_column(
            columns,
            "Type",
            "Competition",
        )

        if date_col and home_col and away_col:
            # Make a clean standard table independent of FA column names.
            cleaned = pd.DataFrame()

            cleaned["date_time"] = table[date_col].map(clean)
            cleaned["home"] = table[home_col].map(clean)
            cleaned["away"] = table[away_col].map(clean)

            if venue_col:
                cleaned["venue"] = table[venue_col].map(clean)
            else:
                cleaned["venue"] = ""

            if type_col:
                cleaned["competition"] = table[type_col].map(clean)
            else:
                cleaned["competition"] = ""

            # Only keep Carterton fixtures.
            cleaned = cleaned[
                (cleaned["home"].map(normalise) == normalise(TEAM_NAME))
                |
                (cleaned["away"].map(normalise) == normalise(TEAM_NAME))
            ]

            # A results table may also contain Carterton.
            # Upcoming fixtures should contain genuine future dates.
            valid_dates = 0

            for raw_date in cleaned["date_time"]:
                try:
                    datetime.strptime(
                        raw_date,
                        "%d/%m/%y %H:%M",
                    )
                    valid_dates += 1
                except ValueError:
                    pass

            if valid_dates:
                now_uk = datetime.now(UK_TIME)

                future_rows = []

                for _, row in cleaned.iterrows():
                    try:
                        fixture_datetime = datetime.strptime(
                            row["date_time"],
                            "%d/%m/%y %H:%M",
                        ).replace(tzinfo=UK_TIME)

                        # Small grace period means today's recently-started
                        # match is not instantly removed.
                        if fixture_datetime >= now_uk - timedelta(hours=4):
                            future_rows.append(row)

                    except ValueError:
                        continue

                if future_rows:
                    result = pd.DataFrame(future_rows)

                    print(
                        f"Found {len(result)} upcoming Carterton fixtures."
                    )

                    return result

    raise RuntimeError(
        "Could not identify a valid upcoming Carterton fixture table. "
        "Calendar has NOT been changed."
    )


# ============================================================
# VALIDATE FIXTURES
# ============================================================

def validate_fixtures(fixtures: pd.DataFrame) -> None:
    if fixtures.empty:
        raise RuntimeError(
            "FA Full-Time returned zero upcoming fixtures. "
            "Existing calendar has been preserved."
        )

    if len(fixtures) > 100:
        raise RuntimeError(
            "An unrealistic number of fixtures was detected. "
            "Existing calendar has been preserved."
        )

    for _, fixture in fixtures.iterrows():
        home = clean(fixture["home"])
        away = clean(fixture["away"])

        if (
            normalise(home) != normalise(TEAM_NAME)
            and normalise(away) != normalise(TEAM_NAME)
        ):
            raise RuntimeError(
                f"Unexpected non-Carterton fixture: {home} v {away}. "
                "Existing calendar has been preserved."
            )

        try:
            datetime.strptime(
                clean(fixture["date_time"]),
                "%d/%m/%y %H:%M",
            )
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid fixture date: {fixture['date_time']}. "
                "Existing calendar has been preserved."
            ) from exc


def existing_event_count() -> int:
    """Count fixtures in the currently published calendar."""
    if not OUTPUT_FILE.exists():
        return 0

    try:
        content = OUTPUT_FILE.read_text(
            encoding="utf-8",
            errors="ignore",
        )

        return content.count("BEGIN:VEVENT")

    except Exception:
        return 0


def suspicious_drop_check(new_count: int) -> None:
    """
    Fail-safe against a partial/broken FA response.

    A legitimate season naturally loses fixtures one at a time.
    A sudden drop from, for example, 25 fixtures to 2 probably means
    the website or parser failed.
    """
    old_count = existing_event_count()

    if old_count < 8:
        return

    minimum_safe = max(2, int(old_count * 0.35))

    if new_count < minimum_safe:
        raise RuntimeError(
            f"Safety stop: existing calendar has {old_count} fixtures "
            f"but FA Full-Time only returned {new_count}. "
            "This looks suspicious, so the existing calendar has "
            "NOT been overwritten."
        )


# ============================================================
# CREATE ICS
# ============================================================

def make_calendar(fixtures: pd.DataFrame) -> str:
    now_utc = datetime.now(timezone.utc)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Carterton FC//CFC Fixtures//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Carterton FC Men's Fixtures",
        "X-WR-CALDESC:Automatic Carterton FC fixture calendar from FA Full-Time",
        "X-WR-TIMEZONE:Europe/London",

        # British timezone definition.
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

    sorted_fixtures = fixtures.copy()

    sorted_fixtures["_sort"] = sorted_fixtures[
        "date_time"
    ].map(
        lambda value: datetime.strptime(
            clean(value),
            "%d/%m/%y %H:%M",
        )
    )

    sorted_fixtures = sorted_fixtures.sort_values("_sort")

    for _, fixture in sorted_fixtures.iterrows():
        competition = clean(fixture["competition"])
        home = clean(fixture["home"])
        away = clean(fixture["away"])
        venue = clean(fixture["venue"])

        start = datetime.strptime(
            clean(fixture["date_time"]),
            "%d/%m/%y %H:%M",
        ).replace(tzinfo=UK_TIME)

        # Two-hour calendar slot.
        end = start + timedelta(hours=2)

        if normalise(home) == normalise(TEAM_NAME):
            home_away = "Home"
            opponent = away
        else:
            home_away = "Away"
            opponent = home

        summary = f"Carterton FC v {opponent}"

        if home_away == "Away":
            summary = f"{opponent} v Carterton FC"

        description_parts = [
            "Carterton FC Men's First Team",
            f"{home_away} fixture",
        ]

        if competition:
            description_parts.append(
                f"Competition: {competition}"
            )

        description_parts.extend(
            [
                "",
                "Fixture information automatically supplied from FA Full-Time.",
                "Please check official club channels for any late changes.",
            ]
        )

        description = "\\n".join(description_parts)

        uid = stable_uid(
            competition,
            home,
            away,
        )

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"LAST-MODIFIED:{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;TZID=Europe/London:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID=Europe/London:{end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{ics_escape(summary)}",
            f"LOCATION:{ics_escape(venue)}",
            f"DESCRIPTION:{ics_escape(description)}",
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "END:VEVENT",
        ]

        lines.extend(event_lines)

    lines.append("END:VCALENDAR")

    # Properly fold long ICS lines.
    folded = []

    for line in lines:
        folded.extend(fold_ics_line(line))

    return "\r\n".join(folded) + "\r\n"


# ============================================================
# FINAL SAFETY CHECK
# ============================================================

def validate_calendar(calendar_text: str, expected_events: int) -> None:
    if not calendar_text.startswith("BEGIN:VCALENDAR"):
        raise RuntimeError("Generated calendar is invalid.")

    if not calendar_text.rstrip().endswith("END:VCALENDAR"):
        raise RuntimeError("Generated calendar is incomplete.")

    event_count = calendar_text.count("BEGIN:VEVENT")

    if event_count != expected_events:
        raise RuntimeError(
            f"Calendar validation failed: expected "
            f"{expected_events} events but generated {event_count}."
        )

    if "Carterton" not in calendar_text:
        raise RuntimeError(
            "Calendar validation failed: Carterton was not found."
        )


# ============================================================
# SAFE WRITE
# ============================================================

def safely_publish(calendar_text: str) -> None:
    """
    Write to a temporary file first.

    The real fixtures.ics is only replaced AFTER everything has
    downloaded, parsed and validated successfully.
    """
    directory = OUTPUT_FILE.parent

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        delete=False,
        dir=directory,
        suffix=".tmp",
    ) as temporary:
        temporary.write(calendar_text)
        temporary_path = Path(temporary.name)

    try:
        # One final read-back test.
        check = temporary_path.read_text(
            encoding="utf-8",
            errors="strict",
        )

        if "BEGIN:VCALENDAR" not in check:
            raise RuntimeError(
                "Temporary calendar file failed validation."
            )

        os.replace(
            temporary_path,
            OUTPUT_FILE,
        )

    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def write_status(fixture_count: int, success: bool, message: str) -> None:
    status = {
        "team": "Carterton FC Men's",
        "source": "FA Full-Time",
        "source_url": FULL_TIME_URL,
        "last_checked_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "success": success,
        "fixture_count": fixture_count,
        "message": message,
    }

    STATUS_FILE.write_text(
        json.dumps(status, indent=2),
        encoding="utf-8",
    )


# ============================================================
# RUN
# ============================================================

def main() -> int:
    print("=" * 60)
    print("CARTERTON FC AUTOMATIC FIXTURE CALENDAR")
    print("=" * 60)

    try:
        page = download_full_time_page()

        fixtures = get_fixture_table(page)

        validate_fixtures(fixtures)

        suspicious_drop_check(len(fixtures))

        calendar_text = make_calendar(fixtures)

        validate_calendar(
            calendar_text,
            len(fixtures),
        )

        safely_publish(calendar_text)

        write_status(
            fixture_count=len(fixtures),
            success=True,
            message="Calendar updated successfully.",
        )

        print()
        print(
            f"SUCCESS: {len(fixtures)} fixtures published."
        )
        print("fixtures.ics updated safely.")

        return 0

    except Exception as exc:
        print()
        print("UPDATE FAILED SAFELY")
        print(str(exc))
        print()
        print(
            "The existing fixtures.ics file has NOT been overwritten."
        )

        # We deliberately do NOT overwrite the good calendar
        # when anything goes wrong.
        return 1


if __name__ == "__main__":
    sys.exit(main())
