

import glob
import json
from tr_client import fetch_many_tickers



def charger_isins(jurisdictions: list[str] | None = None) -> dict[str, str]:
    noms = {}
    fichiers = (
        [f"isins_cache_{j}.json" for j in jurisdictions]
        if jurisdictions
        else glob.glob("isins_cache_*.json")
    )
    for f in fichiers:
        try:
            data = json.load(open(f, encoding="utf-8"))
            for item in data["items"]:
                noms[item["isin"]] = item["name"]
        except FileNotFoundError:
            continue
    return noms



async def sonder_prix(isins: list[str]) -> dict:
    resultats = {}
    for i in range(0, len(isins), 250):
        lot = isins[i:i+250]
        resultats.update(await fetch_many_tickers(lot, timeout=25))
    return resultats



def calculer_variation(bid: float, pre: float) -> float | None:
    if pre <= 0:
        return None
    return (bid - pre) / pre * 100
