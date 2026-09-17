from fastapi import FastAPI

from apps.server.app.websocket import router as chat_router

app = FastAPI(title="ANA Core Loop")
app.include_router(chat_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
