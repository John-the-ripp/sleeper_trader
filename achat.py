"""Signal d'ACHAT : l'ask LSX passe SOUS le vrai prix (marche d'origine, en EUR).

Pourquoi (28/09) : l'utilisateur a deja gagne en achetant Anoto vers 20h et en revendant le
matin. Ca ne marche que les soirs ou L&S baisse AUSSI son ask sous le vrai prix : le lendemain,
L&S se recale sur Stockholm et le bid remonte. Les autres soirs (28/09 : bid -46 %, ask +51 %),
L&S ecarte sa fourchette des deux cotes et acheter coute plus cher que le vrai prix.

Le graphique TR suit le BID : un "prix qui chute" ne dit rien. Ce module ne regarde que l'ASK
(ce qu'on paie) face au vrai prix, toutes les SCAN_S secondes pendant les heures LSX (7h30-23h),
sur les titres suivis. Signal : ask <= vrai prix x (1 + SEUIL/100).
Risques rappeles dans l'interface : TR peut annuler un trade a un prix aberrant ("erreur de
cotation"), et le realignement du lendemain n'est pas garanti si le marche d'origine ouvre en baisse.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import origine
import picks
from tr_data import BASE, tickers

SEUIL = -10.0      # ask au moins 10 % sous le vrai prix
SCAN_S = 300
SUIVIS_MAX = 80    # Yahoo : ~0,5 s par titre ; au-dela le tour devient trop long
FRAICHEUR_J = 3    # le dernier echange sur le marche d'origine doit dater de moins de 3 jours
SPREAD_MAX = 20.0  # au-dela, la fourchette LSX n'est pas exploitable
ECART_MAX = -50.0  # ask plus de 50 % sous le vrai prix : operation sur titre ou cotation aberrante
FICHIER = BASE / "achat_ask.json"
ETAT: dict = {"heure": None, "suivis": 0, "signaux": [], "en_cours": False, "erreur": None}


def _suivis() -> list[dict]:
    """Titres surveilles : watchlist, DOWN/UP, et ceux de l'onglet Rebonds qui ont une habitude."""
    import explications
    import rebonds
    from tr_data import charger_isins
    tous = charger_isins()  # la watchlist ne stocke que des ISIN
    noms: dict[str, str] = {}
    for isin in explications._watchlist():
        noms[isin] = tous.get(isin, isin)
    for l in picks.charger_downup().get("lignes", []):
        if l.get("downup"):
            noms[l["isin"]] = l["nom"]
    for l in rebonds.profils()["lignes"]:
        if l["categorie"] in ("action", "ask_haut", "a_confirmer", "en_veille", "fait"):
            noms[l["isin"]] = l["nom"]
    return [{"isin": i, "nom": n} for i, n in list(noms.items())[:SUIVIS_MAX]]


def _heures_lsx(t: datetime) -> bool:
    m = t.hour * 60 + t.minute
    return t.weekday() < 5 and 7 * 60 + 30 <= m < 23 * 60


async def scanner(maintenant: datetime | None = None) -> dict:
    maintenant = maintenant or datetime.now(picks.PARIS)
    ETAT.update(en_cours=True, erreur=None)
    try:
        suivis = await asyncio.to_thread(_suivis)
        noms = {s["isin"]: s["nom"] for s in suivis}
        live = await tickers(list(noms), timeout=15)
        # vrai prix en parallele (Yahoo) ; cache 2 min dans origine.prix_reel
        with ThreadPoolExecutor(8) as ex:
            reels = dict(zip(noms, ex.map(origine.prix_reel, noms)))
        signaux = []
        for isin, t in live.items():
            bid, ask = picks._prix(t or {}, "bid"), picks._prix(t or {}, "ask")
            r = reels.get(isin)
            if not bid or not ask or not r or ask <= bid:
                continue
            # Intellabridge 29/09 : signal a 8h03, puis bid 0,006 / ask 0,037 (spread 517 %) a 8h39.
            # Une fourchette aussi large n'est pas une occasion : le prix affiche (bid) ne veut rien dire.
            spread = (ask - bid) / bid * 100
            if spread > SPREAD_MAX:
                continue
            vrai, quand = r
            # un "vrai prix" vieux de plusieurs jours (titre peu echange) ne dit rien du prix d'aujourd'hui
            age_j = (maintenant - datetime.fromisoformat(quand)).total_seconds() / 86400
            if age_j > FRAICHEUR_J:
                continue
            ecart_ask = (ask / vrai - 1) * 100
            # plus de 50 % sous le vrai prix : operation sur titre (division, regroupement : Capital B
            # 29/09 = /10) ou cotation aberrante, pas une affaire
            if ecart_ask > SEUIL or ecart_ask < ECART_MAX:
                continue
            signaux.append({
                "isin": isin, "nom": noms[isin] if noms[isin] != isin else (t.get("nom") or isin),
                "bid": bid, "ask": ask, "vrai_eur": round(vrai, 6), "vrai_heure": quand,
                "ecart_ask": round(ecart_ask, 1), "ecart_bid": round((bid / vrai - 1) * 100, 1), "spread": round(spread, 1),
                # si L&S se recale et rachete pres du vrai prix, moins son demi-spread habituel (~5 %)
                "gain_realignement": round((vrai * 0.95 / ask - 1) * 100, 1),
                "heure": maintenant.isoformat(timespec="seconds"),
            })
        signaux.sort(key=lambda s: s["ecart_ask"])
        ETAT.update(heure=maintenant.isoformat(timespec="seconds"), suivis=len(noms), signaux=signaux)
        FICHIER.write_text(json.dumps(ETAT, ensure_ascii=False), encoding="utf-8")
        _alerter(signaux, maintenant)
        return ETAT
    except Exception as exc:
        ETAT["erreur"] = f"{type(exc).__name__}: {exc}"[:300]
        raise
    finally:
        ETAT["en_cours"] = False


def _alerter(signaux: list[dict], maintenant: datetime) -> None:
    """Une alerte "ask_bas" par titre et par jour (bip + notification via /api/alertes)."""
    import explications
    jour = maintenant.date().isoformat()
    for s in signaux:
        explications.alerter(
            "ask_bas",
            {"isin": s["isin"], "nom": s["nom"], "date_jour": jour, "var_1j": s["ecart_ask"], "courant": s["ask"]},
            {"cause_jour": f"Ask LSX {s['ask']:g} € = {s['ecart_ask']:+.0f} % SOUS le vrai prix ({s['vrai_eur']:.4g} €). "
                           f"Si L&S se réaligne : ~{s['gain_realignement']:+.0f} %. Vérifie avant d'acheter (ordre limite)."},
            cle=f"ask_bas:{s['isin']}:{jour}")


def charger() -> dict:
    if ETAT["heure"]:
        return ETAT
    try:
        return json.loads(FICHIER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ETAT


async def boucle() -> None:
    while True:
        if _heures_lsx(datetime.now(picks.PARIS)):
            try:
                await scanner()
            except Exception as exc:  # une panne (TR, Yahoo) ne doit pas tuer la boucle
                print("achat.boucle :", type(exc).__name__, exc)
        await asyncio.sleep(SCAN_S)
