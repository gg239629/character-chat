"""Cloud entry point: require a password and read credentials from Secrets."""
import base64
import os
import secrets
from server import app

if not os.getenv("APP_PASSWORD"):
    raise RuntimeError("Set APP_PASSWORD in Space Settings > Secrets before starting")

@app.middleware("http")
async def require_password(request, call_next):
    from fastapi.responses import Response
    if request.url.path == "/healthz":
        return Response("ok", media_type="text/plain")
    expected_user = os.getenv("APP_USERNAME", "owner")
    expected_password = os.environ["APP_PASSWORD"]
    valid = False
    try:
        scheme, value = request.headers.get("authorization", "").split(" ", 1)
        user, password = base64.b64decode(value, validate=True).decode("utf-8").split(":", 1)
        valid = (scheme.lower() == "basic" and
                 secrets.compare_digest(user.encode(), expected_user.encode()) and
                 secrets.compare_digest(password.encode(), expected_password.encode()))
    except (ValueError, UnicodeError):
        pass
    if not valid:
        return Response("Authentication required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Private chat", charset="UTF-8"',
                                 "Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response

# Platform Secrets take precedence over values saved in the local config.
import server
_original_load_config = server.load_config

def cloud_config():
    config = _original_load_config()
    for key in ("api_key", "base_url", "model"):
        value = os.getenv("MODEL_" + key.upper())
        if value:
            config[key] = value
    return config

server.load_config = cloud_config
