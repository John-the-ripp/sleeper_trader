"""Boucle de fond : sonde tout l'univers, garde le dernier resultat en
memoire. Les routes FastAPI lisent DERNIER_SCAN, elles ne recalculent
jamais rien elles-memes.

Variation calculee UNIQUEMENT depuis bid/ask observes par nous-memes, pas
depuis le champ `pre` de TR (pas fiable, deja vu des valeurs perimees comme
H2 Core -> de faux +700%). Premier tour : pas de variation encore (rien a
comparer), on enregistre juste les prix de depart."""

import asyncio
from datetime import datetime

from tr_data import charger_isins, sonder_prix, calculer_variation

SEUIL_PAR_DEFAUT = 12.0
INTERVALLE_S = 300  # 5 minutes entre deux tours complets

DERNIER_SCAN: dict = {"lignes": [], "heure": None, "en_cours": False}
DERNIER_BID: dict[str, float] = {}  # isin -> bid du tour precedent, garde en memoire


async def un_tour(seuil: float = SEUIL_PAR_DEFAUT) -> list[dict]:
    """Un seul passage sur tout l'univers. Compare au tour PRECEDENT
    (DERNIER_BID), pas au champ pre de TR."""
    noms = charger_isins()
    live = await sonder_prix(list(noms))

    lignes = []
    for isin, t in live.items():
        if not t:
            continue
        try:
            bid = float(t["bid"]["price"])
            ask = float(t["ask"]["price"])
        except (KeyError, TypeError, ValueError):
            continue

        bid_precedent = DERNIER_BID.get(isin)
        if bid_precedent is not None:
            variation = calculer_variation(bid, bid_precedent)
            spread_pct = round((ask - bid) / bid * 100, 1) if bid > 0 else None
            if variation is not None and abs(variation) >= seuil:
                lignes.append({
                    "isin": isin,
                    "nom": noms.get(isin, "?"),
                    "variation_pct": round(variation, 1),
                    "bid": bid,
                    "ask": ask,
                    "spread_pct": spread_pct,
                    "bid_precedent": bid_precedent,
                })
        DERNIER_BID[isin] = bid

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
