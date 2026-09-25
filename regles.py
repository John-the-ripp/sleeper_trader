"""Alertes personnalisees par titre : "previens-moi des que X fait -2 %".

Types de regle :
  - var_bas  : variation du jour <= seuil %   (ex. -2)
  - var_haut : variation du jour >= seuil %   (ex. +5)
  - prix_bas : bid <= seuil EUR
  - prix_haut: bid >= seuil EUR

Verification toutes les 30 s pendant la seance (9h-17h30) : une seule
connexion websocket pour tous les titres surveilles, donc tres leger.
Hors seance on ne verifie pas : les cotations L&S du soir sont des
fourchettes defensives qui declencheraient de fausses alertes.

Une regle se declenche UNE fois par jour (sinon bip toutes les 30 s tant
que la condition reste vraie), puis se rearme toute seule le lendemain.
Reference de la variation = dernier prix de seance de la veille (meme
calcul que la vue Chutes & hausses), pas le champ `pre` de TR.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import explications
import picks
from tr_data import BASE, historiques, tickers

FICHIER = BASE / "regles.json"
FICHIER_CONFIG = BASE / "regles_config.json"
INTERVALLE_S = 30
TYPES = {"var_bas", "var_haut", "prix_bas", "prix_haut", "decroche"}
# "decroche" : le bid LSX passe a >= |seuil| % SOUS le vrai prix (marche d'origine), puis
# une 2e alerte quand il se realigne. Surveille de 7h30 a 23h : Anoto decroche surtout a 18h et 22h.
REALIGNE = -10.0

ETAT: dict = {"heure": None, "actif": False, "cours": {}}  # cours : isin -> {bid, ask, var, ref}
_REF: dict[str, tuple[str, float]] = {}  # isin -> (jour, cloture de seance de la veille)


def config() -> dict:
    try:
        return json.loads(FICHIER_CONFIG.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"etendu": False}


def regler(etendu: bool) -> dict:
    c = {**config(), "etendu": etendu}
    FICHIER_CONFIG.write_text(json.dumps(c), encoding="utf-8")
    return c


def surveille(maintenant: datetime) -> bool:
    """Seance 9h-17h30 par defaut ; 7h30-23h (heures LSX) si l'option est
    activee, en sachant que le soir les spreads s'elargissent."""
    if maintenant.weekday() >= 5:
        return False
    if config().get("etendu"):
        m = maintenant.hour * 60 + maintenant.minute
        return 7 * 60 + 30 <= m < 23 * 60
    return picks.en_seance(maintenant)


def charger() -> list[dict]:
    try:
        return json.loads(FICHIER.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def sauver(regles: list[dict]) -> None:
    FICHIER.write_text(json.dumps(regles, ensure_ascii=False, indent=1), encoding="utf-8")


def ajouter(isin: str, nom: str, type_: str, seuil: float) -> dict:
    if type_ not in TYPES:
        raise ValueError(f"type inconnu : {type_}")
    regles = charger()
    r = {"id": max((x["id"] for x in regles), default=0) + 1, "isin": isin, "nom": nom,
         "type": type_, "seuil": seuil, "cree": datetime.now(picks.PARIS).isoformat(timespec="seconds"),
         "declenchee": None}
    regles.append(r)
    sauver(regles)
    return r


def supprimer(id_: int) -> None:
    sauver([r for r in charger() if r["id"] != id_])


def rearmer(id_: int) -> None:
    regles = charger()
    for r in regles:
        if r["id"] == id_:
            r["declenchee"] = None
    sauver(regles)


def libelle(r: dict) -> str:
    s = r["seuil"]
    if r["type"] == "decroche":
        return f"décrochage LSX ≥ {abs(s):g} % sous le vrai prix"
    return {"var_bas": f"variation ≤ {s:+g} %", "var_haut": f"variation ≥ {s:+g} %",
            "prix_bas": f"prix ≤ {s:g} €", "prix_haut": f"prix ≥ {s:g} €"}[r["type"]]


async def _references(isins: list[str], jour: str) -> None:
    """Cloture de seance de la veille, calculee une fois par jour et par titre."""
    manquants = [i for i in isins if _REF.get(i, ("", 0))[0] != jour]
    if not manquants:
        return
    hist = await historiques(manquants, range="1m", timeout=15)
    for isin in manquants:
        jours = picks.jours_depuis_bougies((hist.get(isin) or {}).get("aggregates", []))
        passes = [j for j in jours if j["date"] < jour]  # seances terminees
        if passes:
            _REF[isin] = (jour, passes[-1]["cloture"])


def _vrai(r: dict, var: float | None, bid: float) -> bool:
    t, s = r["type"], r["seuil"]
    if t == "var_bas":
        return var is not None and var <= s
    if t == "var_haut":
        return var is not None and var >= s
    return bid <= s if t == "prix_bas" else bid >= s


async def verifier() -> None:
    regles = charger()
    maintenant = datetime.now(picks.PARIS)
    jour = maintenant.date().isoformat()
    isins = sorted({r["isin"] for r in regles})
    if not isins:
        return
    await _references(isins, jour)
    live = await tickers(isins, timeout=8)
    # vrai prix (marche d'origine) pour les regles de decrochage, dans un thread (yfinance bloquant)
    import origine
    reels = {}
    for isin in {r["isin"] for r in regles if r["type"] == "decroche"}:
        reels[isin] = await asyncio.to_thread(origine.prix_reel, isin)

    cours = {}
    for isin in isins:
        t = live.get(isin) or {}
        bid, ask = picks._prix(t, "bid"), picks._prix(t, "ask")
        if not bid:
            continue
        ref = _REF.get(isin, (None, None))[1]
        var = round((bid - ref) / ref * 100, 2) if ref else None
        cours[isin] = {"bid": bid, "ask": ask, "var": var, "ref": ref,
                       "spread": round((ask - bid) / bid * 100, 1) if ask else None}
        if reels.get(isin):
            cours[isin]["reel"] = reels[isin][0]
            cours[isin]["ecart_reel"] = round((bid / reels[isin][0] - 1) * 100, 1)
    ETAT.update(heure=maintenant.isoformat(timespec="seconds"), cours=cours)

    change = False
    for r in regles:
        c = cours.get(r["isin"])
        if r["type"] == "decroche":
            change |= _decrochage(r, c, jour, maintenant)
            continue
        deja = r.get("declenchee") and r["declenchee"][:10] == jour
        if not c or deja or not _vrai(r, c["var"], c["bid"]):
            continue
        r["declenchee"] = maintenant.isoformat(timespec="seconds")
        change = True
        message = f"{libelle(r)} atteint : {c['bid']:g} € ({c['var']:+.2f} % sur la séance)" if c["var"] is not None \
            else f"{libelle(r)} atteint : {c['bid']:g} €"
        if c["spread"] and c["spread"] > 10:
            message += f" · attention spread {c['spread']:g} %"
        explications.alerter(
            "regle",
            {"isin": r["isin"], "nom": r["nom"], "date_jour": jour, "var_1j": c["var"], "var_veille": None, "courant": c["bid"]},
            {"categorie": None, "cause_jour": message, "cause_veille": None},
            cle=f"regle{r['id']}:{jour}",
        )
    if change:
        sauver(regles)


def _decrochage(r: dict, c: dict | None, jour: str, maintenant: datetime) -> bool:
    """Alerte a l'entree en decrochage, puis a la sortie (realignement)."""
    if not c or c.get("ecart_reel") is None:
        return False
    e = c["ecart_reel"]
    if r.get("etat") != "decroche" and e <= r["seuil"]:
        r["etat"], r["declenchee"] = "decroche", maintenant.isoformat(timespec="seconds")
        explications.alerter(
            "decroche",
            {"isin": r["isin"], "nom": r["nom"], "date_jour": jour, "var_1j": e, "var_veille": None, "courant": c["bid"]},
            {"categorie": None, "cause_veille": None,
             "cause_jour": f"Bid LSX {c['bid']:g} € = {e:+.0f} % sous le vrai prix ({c['reel']:.4g} €). "
                           "C'est LSX qui décroche, pas le titre : ne vends pas maintenant, attends le réalignement."},
            cle=f"decroche{r['id']}:{maintenant.isoformat(timespec='minutes')}",
        )
        return True
    if r.get("etat") == "decroche" and e >= REALIGNE:
        r["etat"] = "aligne"
        explications.alerter(
            "realigne",
            {"isin": r["isin"], "nom": r["nom"], "date_jour": jour, "var_1j": e, "var_veille": None, "courant": c["bid"]},
            {"categorie": None, "cause_veille": None,
             "cause_jour": f"Bid LSX réaligné : {c['bid']:g} € ({e:+.0f} % vs vrai prix {c['reel']:.4g} €). Si tu voulais vendre, c'est le moment."},
            cle=f"realigne{r['id']}:{maintenant.isoformat(timespec='minutes')}",
        )
        return True
    return False


async def boucle_regles() -> None:
    while True:
        maintenant = datetime.now(picks.PARIS)
        ETAT["actif"] = surveille(maintenant)
        m = maintenant.hour * 60 + maintenant.minute
        soir = maintenant.weekday() < 5 and 7 * 60 + 30 <= m < 23 * 60 and any(r["type"] == "decroche" for r in charger())
        if ETAT["actif"] or soir:
            try:
                await verifier()
            except Exception as exc:  # un aleas reseau ne doit pas tuer la boucle
                ETAT["erreur"] = f"{type(exc).__name__}: {exc}"[:200]
        await asyncio.sleep(INTERVALLE_S)
