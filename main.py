import asyncio

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import scan
from tr_data import charger_isins

app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def index():
    return open("templates/index.html", encoding="utf-8").read()


@app.on_event("startup")
async def demarrage():
    asyncio.create_task(scan.boucle_scan())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/isins")
def route_isins(jurisdiction: str | None = None):
    jur = jurisdiction.split(",") if jurisdiction else None
    noms = charger_isins(jur)
    return {"total": len(noms), "exemple": dict(list(noms.items())[:5])}


@app.get("/chutes")
def route_chutes():
    return scan.DERNIER_SCAN
