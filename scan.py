"""Boucle de fond : sonde tout l'univers, garde le dernier resultat en
memoire. Les routes FastAPI lisent DERNIER_SCAN, elles ne recalculent
jamais rien elles-memes.

Variation calculee UNIQUEMENT depuis bid/ask observes par nous-memes, pas
depuis le champ `pre` de TR (pas fiable, deja vu des valeurs perimees comme
H2 Core -> de faux +700%). Premier tour : pas de variation encore (rien a
comparer), on enregistre juste les prix de depart.

Les titres dont le spread depasse SPREAD_MAX sont ignores : hors seance le
teneur de marche ecarte ses prix et ca fabrique de faux +/-50 % sur les
penny stocks (vu sur Biophytis a 23h : bid 0,0288 / ask 0,0706)."""

import asyncio
from datetime import datetime

from tr_data import charger_isins, sonder_prix, calculer_variation

SEUIL_PAR_DEFAUT = 12.0
SPREAD_MAX = 20.0
INTERVALLE_S = 300  # 5 minutes entre deux tours complets

DERNIER_SCAN: dict = {"lignes": [], "heure": None, "en_cours": False, "tours": 0}
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
        if bid <= 0:
            continue
        spread_pct = round((ask - bid) / bid * 100, 1)

        bid_precedent = DERNIER_BID.get(isin)
        DERNIER_BID[isin] = bid
        if bid_precedent is None or spread_pct > SPREAD_MAX:
            continue
        variation = calculer_variation(bid, bid_precedent)
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

    lignes.sort(key=lambda l: -abs(l["variation_pct"]))
    return lignes


async def boucle_scan(seuil: float = SEUIL_PAR_DEFAUT, intervalle: float = INTERVALLE_S) -> None:
    global DERNIER_SCAN
    while True:
        DERNIER_SCAN["en_cours"] = True
        try:
            lignes = await un_tour(seuil)
            DERNIER_SCAN = {"lignes": lignes, "heure": datetime.now().isoformat(timespec="seconds"),
                            "en_cours": False, "tours": DERNIER_SCAN.get("tours", 0) + 1}
        except Exception as exc:
            DERNIER_SCAN["en_cours"] = False
            DERNIER_SCAN["erreur"] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(intervalle)
