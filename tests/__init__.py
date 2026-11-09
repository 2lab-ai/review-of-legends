"""Legacy dashboard identities are fixtures; production has no default owner."""
import os
import sys

os.environ["ROL_DASHBOARD_OWNER"] = "owner@company.example"
os.environ["ROL_DASHBOARD_VIEWER_DOMAINS"] = "company.example"
os.environ["ROL_DASHBOARD_PUBLIC_URL"] = "https://rol.company.example"
# unittest discovery may have imported the stdlib core before this package.
_auth = sys.modules.get("code_review_service.dashboard_auth")
if _auth is not None:
    _auth.ADMIN_EMAIL = os.environ["ROL_DASHBOARD_OWNER"]
    _auth.VIEWER_DOMAINS = ("company.example",)
