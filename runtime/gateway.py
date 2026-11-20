import os
import re
import secrets

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from runtime.common import call
from runtime.session_store import SESSION_KEY_HEADER
from runtime.streaming import stream_events

app = FastAPI(title="Client server protocol", version="0")
HARNESS = os.getenv("HARNESS_URL", "http://harness:8000")
RESOURCE = r"[a-zA-Z0-9_-]{1,64}"
PUBLIC_ROUTES = {
    "GET": re.compile(rf"(?:runs/{RESOURCE}(?:/events)?|sessions/{RESOURCE}/(?:files|history))"),
    "POST": re.compile(rf"(?:sessions|runs|runs/{RESOURCE}/cancel)"),
    "DELETE": re.compile(rf"sessions/{RESOURCE}"),
}


def authorize(request):
    expected = ("Bearer " + os.getenv("CLIENT_TOKEN", "local-checkpoint-token")).encode()
    if not secrets.compare_digest(request.headers.get("authorization", "").encode(), expected):
        raise HTTPException(401, "invalid token")
    key = request.headers.get(SESSION_KEY_HEADER)
    return {SESSION_KEY_HEADER: key} if key else {}


@app.get("/health")
def health():
    return call(HARNESS, "/health")


@app.get("/runs/{run_id}/stream")
def events(run_id: str, request: Request, after: int = 0):
    headers = authorize(request)
    if not re.fullmatch(RESOURCE, run_id):
        raise HTTPException(404, "unknown identifier")
    status = call(HARNESS, f"/runs/{run_id}", headers=headers)
    try:
        cursor = int(request.headers.get("last-event-id", after))
    except ValueError:
        raise HTTPException(422, "invalid event cursor")
    return StreamingResponse(
        stream_events(lambda value: call(HARNESS, f"/runs/{run_id}/events?after={value}",
                                         headers=headers)["events"], cursor,
                      terminal=status["status"] != "running"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy(path: str, request: Request):
    headers = authorize(request)
    if not PUBLIC_ROUTES[request.method].fullmatch(path):
        raise HTTPException(404, "unknown endpoint")
    body = await request.body()
    if len(body) > 9_000_000:
        raise HTTPException(413, "request too large")
    headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=80, trust_env=False) as client:
            upstream = await client.request(request.method, HARNESS + "/" + path,
                params=request.query_params, content=body, headers=headers)
            return Response(upstream.content, status_code=upstream.status_code,
                            media_type=upstream.headers.get("content-type", "application/json"),
                            headers={"Cache-Control": "no-store"})
    except httpx.HTTPError:
        raise HTTPException(502, "harness transport failed")
