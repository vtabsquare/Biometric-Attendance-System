from dataverse_helper import (
    create_record,
    get_record,
    query_records,
    update_record,
    delete_record,
)

# -------------------- Table & Field Constants --------------------

USERS_TABLE = "crc6f_faceappuserses"
USERS_ID_FIELD = "crc6f_faceappusersid"

ATTENDANCE_TABLE = "crc6f_hr_faceappattendances"
ATTENDANCE_ID_FIELD = "crc6f_hr_faceappattendanceid"


# ==================== USER FUNCTIONS ====================

def get_user_by_email(email: str):
    """
    Look up a user by email address (case-insensitive OData filter).
    Returns the user dict or None if not found.
    """
    records = query_records(
        USERS_TABLE,
        filter_query=f"crc6f_email eq '{email.lower()}'",
    )
    return records[0] if records else None


def get_user_by_id(record_id: str):
    """
    Fetch a single user by Dataverse GUID.
    Returns the user dict.
    """
    return get_record(USERS_TABLE, record_id)


def get_user_by_employeeid(employee_id: str):
    """
    Look up a user by crc6f_employeeid.
    Returns the user dict or None.
    """
    if not employee_id:
        raise Exception("Employee ID missing in query")
        
    filter_str = f"crc6f_employeeid eq '{employee_id}'"
    print("QUERY FILTER:", filter_str)
    
    records = query_records(
        USERS_TABLE,
        filter_query=filter_str,
    )
    return records[0] if records else None


def create_user(first_name: str, last_name: str, email: str,
                hashed_password: str, role: str, employee_id: str,
                status: bool = True):
    """
    Create a new user record in Dataverse.
    Returns the created record dict (includes the GUID).
    """
    data = {
        "crc6f_firstname": first_name,
        "crc6f_lastname": last_name,
        "crc6f_email": email.lower(),
        "crc6f_password": hashed_password,
        "crc6f_role": role,
        "crc6f_employeeid": employee_id,
        "crc6f_status": status,
        "crc6f_faceencoding1": "",
        "crc6f_allowmobile": True,
        "crc6f_allowdesktop": True,
        "crc6f_requiregps": True,
    }
    return create_record(USERS_TABLE, data)


def update_user_face_encoding(record_id: str, encoding_json: str):
    """Store the face encoding JSON string for a user."""
    return update_record(USERS_TABLE, record_id, {
        "crc6f_faceencoding1": encoding_json,
    })


def update_user_password(record_id: str, hashed_password: str,
                         status: bool = False):
    """Update a user's password and reset the must-change flag."""
    return update_record(USERS_TABLE, record_id, {
        "crc6f_password": hashed_password,
        "crc6f_status": status,
    })

# --- NEW: General Update Function for GPS/Login Stats ---
def update_user_fields(record_id: str, fields_dict: dict):
    """
    General purpose function to update any set of fields for a user.
    Used for saving Login_Lat and Login_Long.
    """
    return update_record(USERS_TABLE, record_id, fields_dict)


def get_all_employees():
    """
    Get all users with role='employee'.
    Returns a list of user dicts.
    """
    return query_records(
        USERS_TABLE,
        filter_query="crc6f_role eq 'employee'",
    )


def delete_user(record_id: str):
    """Delete a user record by GUID."""
    return delete_record(USERS_TABLE, record_id)


# ==================== ATTENDANCE FUNCTIONS ====================

def create_attendance(first_name: str, last_name: str, date_str: str,
                      login_time: str, status: str,
                      login_location: str, employee_id: str,
                      logout_time: str = "", logout_location: str = ""):
    """
    Create a new attendance record.
    Returns the created record dict.
    """
    data = {
        "crc6f_firstname": first_name,
        "crc6f_lastname": last_name,
        "crc6f_date": date_str,
        "crc6f_logintime": login_time,
        "crc6f_status": status,
        "crc6f_loginlocation": login_location,
        "crc6f_employeeid": employee_id,
    }
    # Only add optional fields if they have a non-empty value
    if logout_time: data["crc6f_logouttime"] = logout_time
    if logout_location: data["crc6f_logoutlocation"] = logout_location
    
    return create_record(ATTENDANCE_TABLE, data)


def find_open_attendance(first_name: str, date_str: str):
    """
    Find the attendance row for a user on a given date where
    logout time is empty (i.e., the user hasn't logged out yet).
    Returns the record dict or None.
    """
    records = query_records(
        ATTENDANCE_TABLE,
        filter_query=(
            f"crc6f_firstname eq '{first_name}' "
            f"and crc6f_date eq '{date_str}' "
            f"and crc6f_logouttime eq null"
        ),
    )
    return records[0] if records else None


def find_open_meeting_attendance(first_name: str, date_str: str):
    """
    Find an 'In Meeting' attendance row with no logout time.
    Returns the record dict or None.
    """
    records = query_records(
        ATTENDANCE_TABLE,
        filter_query=(
            f"crc6f_firstname eq '{first_name}' "
            f"and crc6f_date eq '{date_str}' "
            f"and crc6f_status eq 'In Meeting' "
            f"and crc6f_logouttime eq null"
        ),
    )
    return records[0] if records else None


def update_attendance(record_id: str, updates: dict):
    """
    Update an attendance record with arbitrary fields.
    `updates` is a dict of Dataverse field names → values.
    """
    return update_record(ATTENDANCE_TABLE, record_id, updates)


def get_attendance_by_date(date_str: str):
    """Get all attendance records for a given date."""
    return query_records(
        ATTENDANCE_TABLE,
        filter_query=f"crc6f_date eq '{date_str}'",
    )


def get_attendance_by_name_and_date(first_name: str, date_str: str):
    """Get attendance records for a specific user on a given date."""
    return query_records(
        ATTENDANCE_TABLE,
        filter_query=(
            f"crc6f_firstname eq '{first_name}' "
            f"and crc6f_date eq '{date_str}'"
        ),
    )


def delete_attendance_by_employee(first_name: str):
    """
    Delete ALL attendance records for a given employee.
    Used when an admin deletes an employee.
    """
    records = query_records(
        ATTENDANCE_TABLE,
        filter_query=f"crc6f_firstname eq '{first_name}'",
        select=ATTENDANCE_ID_FIELD,
    )
    for rec in records:
        delete_record(ATTENDANCE_TABLE, rec[ATTENDANCE_ID_FIELD])
    return len(records)