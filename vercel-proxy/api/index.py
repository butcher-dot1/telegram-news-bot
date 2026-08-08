
import os
import httpx

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response

app = FastAPI()

# Secret shared between Hugging Face and Vercel.
# We will set the same value in both places later.
PROXY_SECRET = os.getenv("PROXY_SECRET", "")

TELEGRAM_API = "https://api.telegram.org"


@app.api_route(
    "/api/telegram/{secret}/{telegram_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def telegram_proxy(
    request: Request,
    secret: str,
    telegram_path: str,
):
    # Protect the proxy from random public use.
    if not PROXY_SECRET or secret != PROXY_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    target_url = f"{TELEGRAM_API}/{telegram_path}"

    body = await request.body()

    # Forward useful headers but don't forward host-specific headers.
    headers = {}

    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=30.0,
            read=50.0,
            write=50.0,
            pool=30.0,
        ),
        follow_redirects=True,
    ) as client:

        response = await client.request(
            method=request.method,
            url=target_url,
            params=request.query_params,
            content=body,
            headers=headers,
        )

    return Response(
        content=response.content,
        status_code=response.status_code,
        headers={
            "content-type": response.headers.get(
                "content-type",
                "application/json",
            )
        },
    )


@app.get("/api")
async def health_check():
    return {
        "status": "ok",
        "message": "Telegram proxy is running",
    }
