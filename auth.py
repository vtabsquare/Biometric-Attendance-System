# auth.py
import os
from dotenv import load_dotenv
import msal

load_dotenv(".env")

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
RESOURCE = os.getenv("RESOURCE")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = [f"{RESOURCE}/.default"]

app = msal.ConfidentialClientApplication(
    client_id=CLIENT_ID,
    client_credential=CLIENT_SECRET,
    authority=AUTHORITY
)

def get_access_token():
    result = app.acquire_token_for_client(scopes=SCOPE)
    if "access_token" in result:
        return result["access_token"]
    else:
        raise Exception(f"Authentication failed: {result}")