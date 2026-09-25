import asyncio
import json
from pathlib import Path

from tr_client import fetch_many_history, fetch_many_tickers

# Dossier du projet : les chemins ne dependent plus du dossier d'ou on lance
# Python (avant, lancer depuis un autre dossier donnait un univers vide).
BASE = Path(__file__).resolve().parent

# ------------------------------------------------------------------ places
# Trade Republic ne passe pas tout par Lang & Schwarz (LSX). Constat du 24/09 :
# Invictus Energy (AU0000016560) n'a AUCUNE cotation LSX, TR la traite sur
# Tradegate (TDG). 153 titres de l'univers etaient dans ce cas (122 cotes sur
# TDG : surtout US, CA, AU) et le cockpit les ignorait sans le dire.
# On demande donc LSX, puis TDG pour ceux qui n'ont pas repondu, et on retient
# la place de chaque titre pour interroger directement la bonne la fois suivante.
FICHIER_PLACES = BASE / "places.json"
REPLI = "TDG"
try:
    PLACES: dict[str, str] = json.loads(FICHIER_PLACES.read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    PLACES = {}


def place(isin: str) -> str:
    return PLACES.get(isin, "LSX")


def _retenir(nouvelles: dict[str, str]) -> None:
    if nouvelles and any(PLACES.get(i) != p for i, p in nouvelles.items()):
        PLACES.update(nouvelles)
        FICHIER_PLACES.write_text(json.dumps(PLACES, indent=0), encoding="utf-8")


async def _avec_repli(fonction, isins: list[str], **kw) -> dict:
    """Appelle `fonction` sur la place connue de chaque titre, puis retente sur
    Tradegate ceux qui n'ont rien renvoye sur LSX."""
    par_place: dict[str, list[str]] = {}
    for i in isins:
        par_place.setdefault(place(i), []).append(i)
    res: dict = {}
    for pl, lot in par_place.items():
        res.update(await fonction(lot, exchange=pl, **kw))
    # "rien" = pas de reponse, OU un historique vide : LSX renvoie un historique vide (et pas
    # une absence) pour les titres qu'il ne cote pas -> sans ce test, pas de repli (Invictus).
    vide = lambda v: not v or (isinstance(v, dict) and "aggregates" in v and not v["aggregates"])
    manquants = [i for i in isins if vide(res.get(i)) and place(i) == "LSX"]
    if manquants:
        repli = await fonction(manquants, exchange=REPLI, **kw)
        trouves = {i: REPLI for i, v in repli.items() if not vide(v)}
        res.update({i: v for i, v in repli.items() if not vide(v)})
        _retenir(trouves)
    return res


async def tickers(isins: list[str], timeout: float = 20.0) -> dict:
    """Comme fetch_many_tickers, mais sur la bonne place (LSX, sinon Tradegate)."""
    return await _avec_repli(fetch_many_tickers, isins, timeout=timeout) if isins else {}


async def historiques(isins: list[str], range: str = "1m", timeout: float = 25.0) -> dict:
    """Comme fetch_many_history, mais sur la bonne place (LSX, sinon Tradegate)."""
    return await _avec_repli(fetch_many_history, isins, range=range, timeout=timeout) if isins else {}


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
            resultats.update(await tickers(morceau, timeout=20))

    await asyncio.gather(*(lot(isins[i:i + 250]) for i in range(0, len(isins), 250)))
    return resultats


def calculer_variation(bid: float, pre: float) -> float | None:
    if pre <= 0:
        return None
    return (bid - pre) / pre * 100
