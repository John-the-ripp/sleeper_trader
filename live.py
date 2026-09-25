"""Cotations en direct pour la fiche ouverte (flux serveur -> navigateur).

Mesure du 24/09 : sur une souscription `ticker` qui reste ouverte, TR pousse
chaque changement de bid/ask en moins d'une seconde (Apple : 5 mises a jour
en 20 s). Mais sur les petites valeurs L&S se tait parfois apres le premier
envoi (constat de l'ancien cockpit) : on rouvre donc la connexion toutes les
REFRAICHIR_S secondes par securite, et tout de suite quand un titre s'ajoute.

Une seule connexion websocket pour tous les titres suivis en direct, quel
que soit le nombre d'onglets ouverts (compteur par titre).
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

import picks
from tr_client import TrClient
from tr_data import place

REFRAICHIR_S = 15

COURS: dict[str, dict] = {}   # isin -> derniere cotation {bid, ask, maj, recu, seq}
_abonnes: Counter = Counter()  # isin -> nombre de pages qui l'ecoutent
_change = asyncio.Event()


def ajouter(isin: str) -> None:
    _abonnes[isin] += 1
    if _abonnes[isin] == 1:
        _change.set()  # nouveau titre : on relance la connexion tout de suite


def retirer(isin: str) -> None:
    _abonnes[isin] -= 1
    if _abonnes[isin] <= 0:
        del _abonnes[isin]


def _callback(isin: str):
    def cb(p: dict) -> None:
        bid, ask = picks._prix(p, "bid"), picks._prix(p, "ask")
        if not bid:
            return
        ancien = COURS.get(isin, {})
        if ancien.get("bid") == bid and ancien.get("ask") == ask:
            return  # rien de neuf : pas besoin de reveiller les pages
        COURS[isin] = {"bid": bid, "ask": ask, "maj": (p.get("bid") or {}).get("time"),
                       "pre": picks._prix(p, "pre"), "recu": time.time(), "seq": ancien.get("seq", 0) + 1}
    return cb


async def boucle() -> None:
    while True:
        isins = sorted(_abonnes)
        if not isins:
            _change.clear()
            await _change.wait()
            continue
        _change.clear()
        tr = TrClient()
        tache = None
        try:
            for isin in isins:
                await tr.ticker(isin, exchange=place(isin), callback=_callback(isin))
            tache = asyncio.create_task(tr.start())
            try:  # on garde la connexion jusqu'au prochain rafraichissement ou nouveau titre
                await asyncio.wait_for(_change.wait(), timeout=REFRAICHIR_S)
            except asyncio.TimeoutError:
                pass
        except Exception:
            await asyncio.sleep(2)  # alea reseau : on retente
        finally:
            if tache:
                tache.cancel()
            await tr.close()
