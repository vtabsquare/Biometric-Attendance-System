import os
import time
import functools
import requests
from dotenv import load_dotenv
import msal

# Load environment variables from .env
load_dotenv()

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
RESOURCE = os.getenv("RESOURCE")  # e.g., https://<yourorg>.crm.dynamics.com

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = [f"{RESOURCE}/.default"]

# Shared MSAL app instance — caches tokens automatically
_msal_app = msal.ConfidentialClientApplication(
    client_id=CLIENT_ID,
    client_credential=CLIENT_SECRET,
    authority=AUTHORITY
)


# -------------------- Retry Decorator --------------------

def retry_on_failure(max_retries=3, backoff_factor=1):
    """Retry decorator with exponential backoff for Dataverse API calls."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = Exception("Max retries exceeded")
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait = backoff_factor * (2 ** attempt)
                        print(f"[Retry {attempt + 1}/{max_retries}] {func.__name__} failed: {e}. Retrying in {wait}s...")
                        time.sleep(wait)
            raise last_exception
        return wrapper
    return decorator


# -------------------- Token --------------------

@retry_on_failure(max_retries=3)
def get_access_token():
    # Try silent (cached) first, then acquire new
    result = _msal_app.acquire_token_silent(scopes=SCOPE, account=None)
    if not result:
        result = _msal_app.acquire_token_for_client(scopes=SCOPE)

    if "access_token" in result:
        return result["access_token"]
    else:
        raise Exception(f"Failed to get token: {result}")


def _headers(token, content_type=True):
    """Build standard Dataverse API headers."""
    h = {
        "Authorization": f"Bearer {token}",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        "Accept": "application/json",
    }
    if content_type:
        h["Content-Type"] = "application/json"
        h["Prefer"] = "return=representation"
    return h


# -------------------- CRUD Functions --------------------

@retry_on_failure(max_retries=3)
def create_record(entity_name, data):
    """Create a new record in Dataverse. Returns the created record dict."""
    token = get_access_token()
    url = f"{RESOURCE}/api/data/v9.2/{entity_name}"
    response = requests.post(url, headers=_headers(token), json=data)
    if response.status_code in (200, 201):
        return response.json()
    else:
        raise Exception(f"Error creating record: {response.status_code} - {response.text}")


@retry_on_failure(max_retries=3)
def get_record(entity_name, record_id):
    """Retrieve a single record by its Dataverse GUID."""
    token = get_access_token()
    url = f"{RESOURCE}/api/data/v9.2/{entity_name}({record_id})"
    response = requests.get(url, headers=_headers(token, content_type=False))
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"Error getting record: {response.status_code} - {response.text}")


@retry_on_failure(max_retries=3)
def query_records(entity_name, filter_query=None, select=None, orderby=None, top=None):
    """
    Query Dataverse records with OData $filter, $select, $orderby, $top.
    Returns a list of matching record dicts.
    
    Examples:
        query_records("crc6f_faceappuserses", filter_query="crc6f_email eq 'test@x.com'")
        query_records("crc6f_hr_faceappattendances", filter_query="crc6f_date eq '2026-03-20'", orderby="crc6f_logintime desc")
    """
    token = get_access_token()
    url = f"{RESOURCE}/api/data/v9.2/{entity_name}"
    params = {}
    if filter_query:
        params["$filter"] = filter_query
    if select:
        params["$select"] = select
    if orderby:
        params["$orderby"] = orderby
    if top:
        params["$top"] = str(top)

    response = requests.get(url, headers=_headers(token, content_type=False), params=params)
    if response.status_code == 200:
        return response.json().get("value", [])
    else:
        raise Exception(f"Error querying records: {response.status_code} - {response.text}")


@retry_on_failure(max_retries=3)
def update_record(entity_name, record_id, data):
    """Update a record by GUID. Returns True on success."""
    token = get_access_token()
    url = f"{RESOURCE}/api/data/v9.2/{entity_name}({record_id})"
    headers = _headers(token)
    headers["If-Match"] = "*"
    # PATCH responses don't return body
    headers.pop("Prefer", None)
    response = requests.patch(url, headers=headers, json=data)
    if response.status_code in (204, 1223):
        return True
    else:
        raise Exception(f"Error updating record: {response.status_code} - {response.text}")


@retry_on_failure(max_retries=3)
def delete_record(entity_name, record_id):
    """Delete a record by GUID. Returns True on success."""
    token = get_access_token()
    url = f"{RESOURCE}/api/data/v9.2/{entity_name}({record_id})"
    headers = {
        "Authorization": f"Bearer {token}",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }
    response = requests.delete(url, headers=headers)
    if response.status_code == 204:
        return True
    else:
        raise Exception(f"Error deleting record: {response.status_code} - {response.text}")