import asyncio
import json
from pathlib import Path

from tr_client import fetch_many_tickers

# Dossier du projet : les chemins ne dependent plus du dossier d'ou on lance
# Python (avant, lancer depuis un autre dossier donnait un univers vide).
BASE = Path(__file__).resolve().parent


def charger_isins(jurisdictions: list[str] | None = None) -> dict[str, str]:
    noms = {}
    fichiers = (
        [BASE / f"isins_cache_{j}.json" for j in jurisdictions]
        if jurisdictions
        else sorted(BASE.glob("isins_cache_*.json"))
    )
    for f in fichiers:
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            continue
        for item in data["items"]:
            noms[item["isin"]] = item["name"]
    return noms


async def sonder_prix(isins: list[str], parallele: int = 6) -> dict:
    """Tickers de toute une liste, par lots de 250, `parallele` lots a la fois
    (meme raison que dans picks.py : un lot lent ne bloque plus les autres)."""
    resultats = {}
    sem = asyncio.Semaphore(parallele)

    async def lot(morceau):
        async with sem:
            resultats.update(await fetch_many_tickers(morceau, timeout=20))

    await asyncio.gather(*(lot(isins[i:i + 250]) for i in range(0, len(isins), 250)))
    return resultats


def calculer_variation(bid: float, pre: float) -> float | None:
    if pre <= 0:
        return None
    return (bid - pre) / pre * 100
