"""
One-time migration script: Google Sheets → Microsoft Dataverse

Usage:
    python migration_script.py              # Full migration
    python migration_script.py --dry-run    # Preview only, no writes

Requirements:
    - google_creds.json must exist in the working directory
    - id.env must contain valid Dataverse credentials
    - pip install gspread oauth2client msal requests python-dotenv
"""

import sys
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from werkzeug.security import generate_password_hash
from dataverse_helper import create_record, query_records

# --- Config ---

USERS_TABLE = "crc6f_faceappuserses"
ATTENDANCE_TABLE = "crc6f_hr_faceappattendances"

DRY_RUN = "--dry-run" in sys.argv


def connect_google_sheets():
    """Connect to the Google Sheets spreadsheet."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name("google_creds.json", scope)
    client = gspread.authorize(creds)
    spreadsheet = client.open("Office_Attendance_System")
    return spreadsheet.worksheet("users"), spreadsheet.worksheet("Attendance")


def check_user_exists(email: str) -> bool:
    """Check if a user already exists in Dataverse by email."""
    records = query_records(
        USERS_TABLE,
        filter_query=f"crc6f_email eq '{email.lower()}'",
        select="crc6f_email",
        top=1,
    )
    return len(records) > 0


def check_attendance_exists(first_name: str, date_str: str, login_time: str) -> bool:
    """Check if an attendance record already exists (to avoid duplicates)."""
    records = query_records(
        ATTENDANCE_TABLE,
        filter_query=(
            f"crc6f_firstname eq '{first_name}' "
            f"and crc6f_date eq '{date_str}' "
            f"and crc6f_logintime eq '{login_time}'"
        ),
        select="crc6f_hr_faceappattendanceid",
        top=1,
    )
    return len(records) > 0


def migrate_users(user_sheet):
    """Migrate all users from Google Sheets to Dataverse."""
    print("\n" + "=" * 60)
    print("MIGRATING USERS")
    print("=" * 60)

    all_users = user_sheet.get_all_records()
    success, skipped, failed = 0, 0, 0

    for i, row in enumerate(all_users, start=1):
        first_name = str(row.get("First Name", "")).strip()
        last_name = str(row.get("Last Name", "")).strip()
        email = str(row.get("Email", "")).strip().lower()
        password = str(row.get("Password", "")).strip()
        role = str(row.get("Role", "employee")).strip().lower()
        face_encoding = str(row.get("Face Encoding", "")).strip()
        must_reset = str(row.get("Must Reset", "")).strip()

        if not email:
            print(f"  [{i}] SKIP — no email")
            skipped += 1
            continue

        # Check for duplicates
        if not DRY_RUN and check_user_exists(email):
            print(f"  [{i}] SKIP — {email} already exists in Dataverse")
            skipped += 1
            continue

        # Determine status (True = must reset, False = active)
        status = True if must_reset == "1" else False

        # Use email as employee ID
        employee_id = email

        data = {
            "crc6f_firstname": first_name,
            "crc6f_lastname": last_name,
            "crc6f_email": email,
            "crc6f_password": password,  # Already hashed from Google Sheets
            "crc6f_role": role,
            "crc6f_employeeid": employee_id,
            "crc6f_status": status,
            "crc6f_faceencoding1": face_encoding if face_encoding else "",
        }

        if DRY_RUN:
            print(f"  [{i}] DRY RUN — would create: {first_name} {last_name} ({email})")
            success += 1
        else:
            try:
                result = create_record(USERS_TABLE, data)
                record_id = result.get("crc6f_faceappusersid", "???")
                print(f"  [{i}] OK — {first_name} {last_name} ({email}) → {record_id}")
                success += 1
            except Exception as e:
                print(f"  [{i}] FAIL — {email}: {e}")
                failed += 1

    print(f"\nUsers: {success} migrated, {skipped} skipped, {failed} failed")
    return success, skipped, failed


def migrate_attendance(attn_sheet):
    """Migrate all attendance records from Google Sheets to Dataverse."""
    print("\n" + "=" * 60)
    print("MIGRATING ATTENDANCE")
    print("=" * 60)

    all_records = attn_sheet.get_all_records()
    success, skipped, failed = 0, 0, 0

    for i, row in enumerate(all_records, start=1):
        first_name = str(row.get("First Name", "")).strip()
        last_name = str(row.get("Last Name", "")).strip()
        date_str = str(row.get("Date", "")).strip()
        login_time = str(row.get("Login Time", "")).strip()
        logout_time = str(row.get("Logout Time", "")).strip()
        status = str(row.get("Status", "")).strip()
        login_loc = str(row.get("Login Location", "")).strip()
        logout_loc = str(row.get("Logout Location", "")).strip()

        if not first_name or not date_str:
            print(f"  [{i}] SKIP — missing name or date")
            skipped += 1
            continue

        # Check for duplicates
        if not DRY_RUN and check_attendance_exists(first_name, date_str, login_time):
            print(f"  [{i}] SKIP — duplicate: {first_name} on {date_str} at {login_time}")
            skipped += 1
            continue

        # Derive employee_id (email) — not available in attendance sheet,
        # use a placeholder. The employeeid can be backfilled later if needed.
        employee_id = f"{first_name.lower()}"

        data = {
            "crc6f_firstname": first_name,
            "crc6f_lastname": last_name,
            "crc6f_date": date_str,
            "crc6f_logintime": login_time,
            "crc6f_logouttime": logout_time if logout_time else "",
            "crc6f_status": status if status else "Present",
            "crc6f_loginlocation": login_loc if login_loc else "",
            "crc6f_logoutlocation": logout_loc if logout_loc else "",
            "crc6f_employeeid": employee_id,
        }

        if DRY_RUN:
            print(f"  [{i}] DRY RUN — would create: {first_name} {date_str} {login_time}")
            success += 1
        else:
            try:
                result = create_record(ATTENDANCE_TABLE, data)
                record_id = result.get("crc6f_hr_faceappattendanceid", "???")
                print(f"  [{i}] OK — {first_name} {date_str} {login_time} → {record_id}")
                success += 1
            except Exception as e:
                print(f"  [{i}] FAIL — {first_name} {date_str}: {e}")
                failed += 1

    print(f"\nAttendance: {success} migrated, {skipped} skipped, {failed} failed")
    return success, skipped, failed


def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE MIGRATION"
    print(f"\n{'#' * 60}")
    print(f"  Google Sheets → Dataverse Migration ({mode})")
    print(f"{'#' * 60}")

    if not DRY_RUN:
        confirm = input("\n⚠️  This will write to your production Dataverse. Continue? (yes/no): ")
        if confirm.lower() != "yes":
            print("Aborted.")
            return

    print("\nConnecting to Google Sheets...")
    user_sheet, attn_sheet = connect_google_sheets()

    u_ok, u_skip, u_fail = migrate_users(user_sheet)
    a_ok, a_skip, a_fail = migrate_attendance(attn_sheet)

    print(f"\n{'=' * 60}")
    print(f"MIGRATION COMPLETE ({mode})")
    print(f"{'=' * 60}")
    print(f"  Users:      {u_ok} created, {u_skip} skipped, {u_fail} failed")
    print(f"  Attendance: {a_ok} created, {a_skip} skipped, {a_fail} failed")
    print()


if __name__ == "__main__":
    main()
