from fastapi import FastAPI

app = FastAPI(title="Pool Control")


@app.get("/api/health")
async def health():
    return {"ok": True}
