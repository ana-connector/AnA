from fastapi import FastAPI

app = FastAPI(title="ANA Core Loop")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
