"""Cloud entry point: require a password and read credentials from Secrets."""
import base64
import os
import secrets
from server import app
import accounts

@app.middleware("http")
async def require_password(request, call_next):
    from fastapi.responses import Response
    if request.url.path == "/healthz":
        return Response("ok", media_type="text/plain")
    if accounts.enabled():
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        return response
    if not os.getenv('APP_PASSWORD'):
        return Response('Configure account login or APP_PASSWORD before opening the site',status_code=503)
    if request.url.path=='/api/jobs/tick':
        token=os.getenv('ROLE_CRON_TOKEN','')
        if len(token)>=32 and secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+token):
            return await call_next(request)
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
