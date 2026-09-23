"""Boucle de fond : sonde tout l'univers, garde le dernier resultat en
memoire. Les routes FastAPI lisent DERNIER_SCAN, elles ne recalculent
jamais rien elles-memes."""

import asyncio
from datetime import datetime

from tr_data import charger_isins, sonder_prix, calculer_variation

SEUIL_PAR_DEFAUT = 12.0
INTERVALLE_S = 300  # 5 minutes entre deux tours complets

DERNIER_SCAN: dict = {"lignes": [], "heure": None, "en_cours": False}


async def un_tour(seuil: float = SEUIL_PAR_DEFAUT) -> list[dict]:
    """Un seul passage sur tout l'univers. Isole du bouclage pour pouvoir
    le tester seul, sans attendre 5 minutes entre deux tours."""
    noms = charger_isins()
    live = await sonder_prix(list(noms))

    lignes = []
    for isin, t in live.items():
        if not t:
            continue
        try:
            bid = float(t["bid"]["price"])
            pre = float(t["pre"]["price"])
        except (KeyError, TypeError, ValueError):
            continue
        variation = calculer_variation(bid, pre)
        if variation is not None and abs(variation) >= seuil:
            lignes.append({
                "isin": isin,
                "nom": noms.get(isin, "?"),
                "variation_pct": round(variation, 1),
                "bid": bid,
                "pre": pre,
            })
    lignes.sort(key=lambda l: -abs(l["variation_pct"]))
    return lignes


async def boucle_scan(seuil: float = SEUIL_PAR_DEFAUT, intervalle: float = INTERVALLE_S) -> None:
    global DERNIER_SCAN
    while True:
        DERNIER_SCAN["en_cours"] = True
        try:
            lignes = await un_tour(seuil)
            DERNIER_SCAN = {"lignes": lignes, "heure": datetime.now().isoformat(), "en_cours": False}
        except Exception as exc:
            DERNIER_SCAN["en_cours"] = False
            DERNIER_SCAN["erreur"] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(intervalle)
