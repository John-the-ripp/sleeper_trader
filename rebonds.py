"""Vue unique des rebonds : un profil par titre, 4 detecteurs combines, 5 categories.

Avant (09/2026), le meme sujet etait eclate en 5 endroits (Rebonds du jour, Avant le rebond,
DOWN/UP, Ranges, Oscillateurs). Question de l'utilisateur : "qui l'a deja fait et combien de
fois, et qui le fait normalement mais pas encore -> faut prendre action".

Pour chaque titre (series du dernier tour du screener) on mesure son HABITUDE de rebond avec
les 4 detecteurs de motifs.py, puis son ETAT maintenant :
  - habitude : chute_rebond (lendemain), down_up (rachat en <= 5 seances), plancher (meme support
    touche puis rachete), oscillation (allers support -> resistance). On garde le detecteur qui
    a vu le PLUS de rebonds (pas de somme : c'est souvent le meme evenement vu deux fois).
  - etat     : rebondi aujourd'hui ? en chute aujourd'hui ? en phase DOWN ? pres du support ?
Categories :
  "fait"       : a rebondi AUJOURD'HUI (chute hier >= 10 %, hausse aujourd'hui >= 10 %)
  "action"     : rebondit d'habitude (>= 2 fois, >= 60 %) ET est en zone de rebond maintenant,
                 mais ne l'a pas encore fait -> c'est la que se prend une decision
  "a_confirmer": en zone de rebond, avec un seul precedent
  "couteau"    : chute aujourd'hui sans aucun rebond connu
  "en_veille"  : rebondit d'habitude, mais n'est pas en zone de rebond maintenant (a surveiller)
Pas de prediction : on classe selon le passe du titre, sur un mois de seances LSX.
"""

from __future__ import annotations

import time
from datetime import datetime

import explications
import motifs
import picks
from simulateur import charger_series

SPREAD_MAX = 20.0
CHUTE_JOUR = -10.0     # "en chute aujourd'hui"
REBOND_JOUR = 10.0     # "a rebondi aujourd'hui" (apres une chute hier <= CHUTE_JOUR)
HABITUDE_MIN = 2       # rebonds passes pour parler d'habitude
TAUX_MIN = 60          # % de chutes rachetees
POTENTIEL_MIN = 10.0   # % minimum entre l'ask actuel et l'objectif pour parler de benefice possible
_CACHE: dict = {"cle": None, "res": None}


def _habitude(jours: list[dict]) -> dict:
    """Ce que chaque detecteur a vu sur le mois, et le meilleur des quatre."""
    cr = motifs.chute_rebond(jours)
    du = motifs.down_up(jours)
    pl = motifs.plancher(jours)
    osc = motifs.oscillation(jours)
    if osc and osc["casse"]:  # le prix est passe sous tous les creux : le range n'existe plus (MyHotelMatch 28/09)
        osc = None
    sources = []
    if cr["chutes"]:
        sources.append({"detecteur": "chute_rebond", "rebonds": cr["rebonds"], "occasions": cr["chutes"],
                        "moyen": cr["rebond_moyen"], "texte": f"{cr['rebonds']}/{cr['chutes']} chutes ≥ {motifs.CHUTE:g} % rachetées le lendemain"})
    if du["cycles"]:
        sources.append({"detecteur": "down_up", "rebonds": du["reussis"], "occasions": du["cycles"], "moyen": du["rebond_moy"],
                        "texte": f"{du['reussis']}/{du['cycles']} chutes ≥ {motifs.DU_CHUTE:g} % rachetées en ≤ {motifs.DU_DELAI} séances"
                                 + (f" (~{du['delai_moy']} séances)" if du["reussis"] else "")})
    if pl:
        sources.append({"detecteur": "plancher", "rebonds": pl["reussis"], "occasions": pl["testes"], "moyen": pl["rebond_moy"],
                        "texte": f"support {pl['plancher']:g} touché {pl['contacts']} fois, {pl['reussis']}/{pl['testes']} rebonds ≥ {motifs.PL_REBOND:g} %"})
    if osc:
        # une oscillation "approximative" peut etre une simple tendance (VerifyMe : supports qui montent) :
        # elle est affichee, mais ne compte pas comme rebonds prouves
        sources.append({"detecteur": "oscillation", "rebonds": osc["n"] if osc["regularite"] == "reguliere" else 0,
                        "occasions": osc["n"], "moyen": osc["hausse_med"],
                        "texte": f"{osc['n']} allers support {osc['support']:g} → résistance {osc['resistance']:g} (+{osc['hausse_med']:g} % méd.)"})
    meilleur = max(sources, key=lambda s: (s["rebonds"], -s["occasions"]), default=None)
    # un detecteur qui a vu surtout des ECHECS contredit l'habitude (MyHotelMatch : 1/4 lendemains)
    echecs = [s for s in sources if s["occasions"] >= 3 and s["rebonds"] / s["occasions"] < 0.5]
    return {"cr": cr, "du": du, "pl": pl, "osc": osc, "sources": sources, "meilleur": meilleur,
            "rebonds": meilleur["rebonds"] if meilleur else 0, "occasions": meilleur["occasions"] if meilleur else 0,
            "taux": round(meilleur["rebonds"] / meilleur["occasions"] * 100) if meilleur and meilleur["occasions"] else None,
            "moyen": meilleur["moyen"] if meilleur else None,
            # ... sauf si un rachat sur plusieurs seances est confirme : Anoto rebondit en 2-5 seances,
            # "pas de rebond LE LENDEMAIN" ne le contredit pas
            "contredit": bool(echecs) and not (du["downup"] or bool(pl and pl["regulier"]))}


def _etat(h: dict, mvt: dict | None) -> dict:
    """Ou en est le titre MAINTENANT."""
    var_j = (mvt or {}).get("var_1j")
    var_h = (mvt or {}).get("var_veille")
    pl, osc, du = h["pl"], h["osc"], h["du"]
    raisons = []
    if var_j is not None and var_j <= CHUTE_JOUR:
        raisons.append(f"chute aujourd'hui ({var_j:+.1f} %)")
    if du["cycles"] and du["en_phase_down"]:
        raisons.append("grosse chute pas encore rachetée (phase DOWN)")
    if pl and pl["pres_du_plancher"] and not pl["casse"]:
        raisons.append(f"au support {pl['plancher']:g} ({pl['distance_pct']:+.0f} %)")
    elif osc and osc["position_pct"] is not None and osc["position_pct"] <= 25:
        raisons.append(f"bas du range ({osc['position_pct']} %, support {osc['support']:g})")
    return {"zone": bool(raisons), "raisons": raisons,
            "rebondi_aujourdhui": var_h is not None and var_j is not None and var_h <= CHUTE_JOUR and var_j >= REBOND_JOUR,
            "var_1j": var_j, "var_veille": var_h}


def _potentiel_ask(h: dict, t: dict) -> float | None:
    """Gain si j'achete MAINTENANT a l'ask et revends a l'objectif (niveau que le BID a atteint aux
    rebonds passes). Remarque de l'utilisateur (Erda, 28/09) : quand le prix chute, c'est surtout le
    bid qui descend ; l'ask reste haut. Un rebond "sur le graphique" (qui suit le bid) ne dit donc
    pas si on peut acheter au creux : c'est l'ask actuel qui compte."""
    objectif = (h["pl"] or {}).get("objectif") or (h["osc"] or {}).get("resistance")
    moyen = h["moyen"]
    if not t.get("ask"):
        return None
    if objectif:
        return round((objectif / t["ask"] - 1) * 100, 1)
    if moyen and t.get("bid"):  # pas de niveau : rebond moyen applique au bid, revendu... au bid
        return round((t["bid"] * (1 + moyen / 100) / t["ask"] - 1) * 100, 1)
    return None


def _ecart_entree(ask: float | None, support: float | None) -> float | None:
    return round((ask / support - 1) * 100, 1) if ask and support else None


def _categorie(h: dict, e: dict, pot: float | None) -> str:
    habitude = not h["contredit"] and (
        (h["rebonds"] >= HABITUDE_MIN and (h["taux"] or 0) >= TAUX_MIN) or h["du"]["downup"]
        or bool(h["pl"] and h["pl"]["regulier"]) or bool(h["osc"] and h["osc"]["regularite"] == "reguliere"))
    if e["rebondi_aujourdhui"]:
        return "fait"
    if e["zone"] and habitude:
        # "a prendre" seulement s'il reste un vrai benefice en achetant a l'ask d'aujourd'hui
        return "action" if pot is not None and pot >= POTENTIEL_MIN else "ask_haut"
    if e["zone"] and h["rebonds"] >= 1:
        return "a_confirmer"
    if e["var_1j"] is not None and e["var_1j"] <= CHUTE_JOUR:
        return "couteau"
    return "en_veille" if habitude else ""


COTE_MAX_H = 2.0       # une cotation plus vieille que ca (en journee) est figee : on ne s'y fie pas


def cotation_valide(t: dict | None, maintenant: datetime) -> tuple[float | None, float | None, str | None]:
    """(bid, ask, raison du rejet). Cas Mondo TV 28/09 : bid = ask = last = 0,021 avec la meme
    quantite, fige depuis 9h55 -> la trace d'UN echange, pas une fourchette. L'appli montrait en
    realite un grand ecart."""
    t = t or {}
    bid, ask = picks._prix(t, "bid"), picks._prix(t, "ask")
    if not bid or not ask:
        return bid, ask, "pas de cotation"
    if ask <= bid:
        return bid, ask, "bid = ask : pas une vraie fourchette"
    quand = max(((t.get(k) or {}).get("time") or 0) for k in ("bid", "ask"))
    age_h = (maintenant.timestamp() * 1000 - quand) / 3_600_000 if quand else None
    if age_h is not None and age_h > COTE_MAX_H and maintenant.weekday() < 5 and 8 <= maintenant.hour < 22:
        return bid, ask, f"cotation figée depuis {age_h:.0f} h"
    return bid, ask, None


def avec_cotes_live(res: dict, live: dict, maintenant: datetime) -> dict:
    """Refait prix, spread, benefice et categorie avec les cotations EN DIRECT (les series datent du
    dernier tour du screener, jusqu'a 15 min). Les titres a cotation invalide sont retires."""
    lignes = []
    for l in res["lignes"]:
        if l["isin"] not in live:  # pas redemande (categories repliees) : on garde tel quel
            lignes.append(l)
            continue
        bid, ask, rejet = cotation_valide(live[l["isin"]], maintenant)
        if rejet:
            continue
        spread = round((ask - bid) / bid * 100, 1)
        if spread > SPREAD_MAX:
            continue
        pot = round((l["objectif"] / ask - 1) * 100, 1) if l.get("objectif") else \
            (round((bid * (1 + l["rebond_moyen"] / 100) / ask - 1) * 100, 1) if l.get("rebond_moyen") else None)
        cat = l["categorie"]
        if cat in ("action", "ask_haut"):
            cat = "action" if pot is not None and pot >= POTENTIEL_MIN else "ask_haut"
        lignes.append({**l, "bid": bid, "ask": ask, "spread_pct": spread, "potentiel_ask": pot, "categorie": cat, "live": True,
                       "ecart_entree": _ecart_entree(ask, l.get("support"))})
    return {**res, "lignes": lignes, "live": maintenant.isoformat(timespec="seconds")}


def profils() -> dict:
    """Tous les titres classes. Cache jusqu'au prochain tour du screener (series + mouvements)."""
    series = charger_series()
    mvts = picks.charger_mouvements()
    jour_cle = next((l["date_jour"] for l in mvts.get("lignes", []) if l.get("date_jour")), None)
    cle = (series.get("heure"), mvts.get("heure"), len(explications.charger(jour_cle)) if jour_cle else 0)
    if _CACHE["cle"] == cle:
        return _CACHE["res"]
    t0 = time.time()
    par_isin = {l["isin"]: l for l in mvts.get("lignes", [])}
    jour = next((l["date_jour"] for l in mvts.get("lignes", []) if l.get("date_jour")), None)
    expl = explications.charger(jour) if jour else {}
    lignes = []
    for isin, t in (series.get("titres") or {}).items():
        # spread de 0 % = bid = ask : la trace d'un echange, pas une fourchette (Mondo TV 28/09)
        if not 0 < t.get("spread", 99) <= SPREAD_MAX or len(t["j"]) < 8:
            continue
        jours = [{"date": d, "bas": b, "haut": hh, "cloture": c} for d, b, hh, c in t["j"]]
        h = _habitude(jours)
        mvt = par_isin.get(isin)
        # pas de cotation du jour (Fantasia) ou historique arrete (suspension) : etat inconnu
        if not mvt or mvt.get("var_1j") is None or mvt.get("trou_jours", 0) > 4:
            continue
        e = _etat(h, mvt)
        pot = _potentiel_ask(h, t)
        cat = _categorie(h, e, pot)
        if not cat:
            continue
        lignes.append({
            "isin": isin, "nom": t["nom"], "bid": t["bid"], "ask": t["ask"], "spread_pct": t["spread"], "hors_fuseau": t.get("hf"),
            "categorie": cat, "rebonds": h["rebonds"], "occasions": h["occasions"], "taux": h["taux"], "rebond_moyen": h["moyen"],
            # ce qui reste d'un rebond moyen une fois l'aller-retour ask/bid paye
            "net_attendu": round(h["moyen"] - t["spread"], 1) if h["moyen"] else None,
            "habitude": [s["texte"] for s in h["sources"]], "zone": e["raisons"],
            "var_1j": e["var_1j"], "var_veille": e["var_veille"],
            "support": (h["pl"] or {}).get("plancher") or (h["osc"] or {}).get("support"),
            "objectif": (h["pl"] or {}).get("objectif") or (h["osc"] or {}).get("resistance"),
            "clotures": [j["cloture"] for j in jours],
            "potentiel_ask": pot,
            # prix d'entree = support ; ecart de l'ask (ce qu'on paie) a ce prix (demande du 29/09 :
            # "que les titres dont l'opportunite matche vraiment le prix d'entree")
            "ecart_entree": _ecart_entree(t.get("ask"), (h["pl"] or {}).get("plancher") or (h["osc"] or {}).get("support")),
            "cause": (expl.get(isin) or {}).get("cause_jour"), "cause_veille": (expl.get(isin) or {}).get("cause_veille"),
            "cause_categorie": (expl.get(isin) or {}).get("categorie"),
        })
    ordre = {"fait": 0, "action": 1, "ask_haut": 2, "a_confirmer": 3, "couteau": 4, "en_veille": 5}
    # a prendre : le plus gros benefice possible (a l'ask d'aujourd'hui) d'abord
    lignes.sort(key=lambda l: (ordre[l["categorie"]], -(l["potentiel_ask"] or -99) if l["categorie"] == "action" else -l["rebonds"],
                               -(l["net_attendu"] or -99)))
    res = {"heure": series.get("heure"), "jour": jour,
           "lignes": lignes, "duree_s": round(time.time() - t0, 1),
           "seuils": {"spread_max": SPREAD_MAX, "chute_jour": CHUTE_JOUR, "rebond_jour": REBOND_JOUR,
                      "habitude_min": HABITUDE_MIN, "taux_min": TAUX_MIN, "potentiel_min": POTENTIEL_MIN}}
    _CACHE.update(cle=cle, res=res)
    return res
