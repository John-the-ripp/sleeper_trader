"""Detection de motifs repetitifs sur l'historique d'un titre. Pur Python,
sans IA : c'est mesurable, donc fiable et gratuit sur les 11 000 titres.

Motif principal (demande du 24/09) : "a chaque fois qu'elle descend fort,
elle remonte fort le lendemain". On compte les seances ou le titre chute
d'au moins CHUTE %, et parmi elles celles ou la seance SUIVANTE monte d'au
moins REBOND % au-dessus de la cloture de la chute.

Le rebond est mesure sur le HAUT de la seance suivante (prix de seance, pas
de meches) : c'est le prix auquel on aurait pu revendre. Mesurer sur la
cloture raterait un rebond du matin qui retombe l'apres-midi.
"""

from __future__ import annotations

import statistics

CHUTE = 15.0     # % de baisse cloture/cloture pour parler de "forte chute"
REBOND = 10.0    # % de hausse le lendemain (sur le haut de seance) pour parler de rebond
MIN_CHUTES = 2   # en dessous, pas de motif : une seule occurrence n'est pas une habitude


# ------------------------------------------------ DOWN/UP "Anoto-like"
# Profil mesure sur Anoto Group (SE0010415281) en septembre 2026 :
#   02-03/09 : -28 % puis -20 %  -> +85 % le 10/09 (5 seances plus tard)
#   14/09    : -27 %             -> +28 % le 16/09 (2 seances)
#   18/09    : -31 %             -> +50 % le 21/09 (1 seance)
# = titre "en range" : chaque grosse chute est rachetee en quelques seances.
# Plus large que chute_rebond (qui n'accepte que le lendemain).
DU_CHUTE = 20.0      # chute >= 20 % ...
DU_FENETRE_CHUTE = 2  # ... en 1 ou 2 seances
DU_REBOND = 20.0     # rebond >= 20 % au-dessus du creux ...
DU_DELAI = 5         # ... dans les 5 seances suivantes
DU_MIN = 2           # au moins 2 cycles termines
DU_TAUX = 66         # et au moins 2 sur 3 reussis


def down_up(jours: list[dict]) -> dict:
    cycles, i = [], 1
    while i < len(jours):
        creux = jours[i]["cloture"]
        avant = [j["cloture"] for j in jours[max(0, i - DU_FENETRE_CHUTE):i]]
        sommet = max(avant) if avant else 0
        baisse = (creux - sommet) / sommet * 100 if sommet > 0 else 0
        # la chute doit etre "finie" : le lendemain ne s'enfonce pas encore plus
        if baisse > -DU_CHUTE or (i + 1 < len(jours) and jours[i + 1]["cloture"] < creux * 0.97):
            i += 1
            continue
        suite = jours[i + 1:i + 1 + DU_DELAI]
        # i = position dans la serie de clotures : le front s'en sert pour placer les marqueurs
        cycle = {"i": i, "date": jours[i]["date"], "chute": round(baisse, 1), "rebond": None, "delai": None, "reussi": False}
        if suite:
            # 1er jour qui franchit le seuil (sinon le meilleur jour de la fenetre) : prendre le
            # plus haut de la fenetre "consommait" les seances suivantes et ratait le cycle
            # d'apres (Anoto : le 14/09 avalait la chute du 18/09).
            hausses = [(j["haut"] - creux) / creux * 100 for j in suite]
            k = next((n for n, h in enumerate(hausses) if h >= DU_REBOND), max(range(len(suite)), key=hausses.__getitem__))
            hausse = hausses[k]
            cycle.update(rebond=round(hausse, 1), delai=k + 1, reussi=hausse >= DU_REBOND)
            # fenetre pas encore complete et pas encore de rebond : le cycle est "en cours"
            if not cycle["reussi"] and len(suite) < DU_DELAI:
                cycle["rebond"] = None
        cycles.append(cycle)
        # on saute la fenetre du cycle pour ne pas compter deux fois la meme chute
        i += (cycle["delai"] or 1) + 1 if cycle["reussi"] else 1 + len(suite)

    termines = [c for c in cycles if c["rebond"] is not None]
    reussis = [c for c in termines if c["reussi"]]
    taux = round(len(reussis) / len(termines) * 100) if termines else None
    return {
        "cycles": len(termines), "reussis": len(reussis), "taux": taux,
        "chute_moy": round(sum(c["chute"] for c in termines) / len(termines), 1) if termines else None,
        "rebond_moy": round(sum(c["rebond"] for c in reussis) / len(reussis), 1) if reussis else None,
        "delai_moy": round(sum(c["delai"] for c in reussis) / len(reussis), 1) if reussis else None,
        # derniere grosse chute pas encore rachetee : c'est LE moment que guette ce motif
        "en_phase_down": bool(cycles) and cycles[-1]["rebond"] is None,
        "downup": len(termines) >= DU_MIN and (taux or 0) >= DU_TAUX,
        "detail": cycles,
    }


# ------------------------------------------------ "rebond sur plancher"
# "A chaque fois il touche la meme chose, il remonte" : un titre en RANGE.
# Plus regulier qu'Anoto : on sait OU acheter (le plancher), pas seulement QUAND.
PL_TOL = 6.0       # un "contact" = bas de seance a moins de 6 % du plancher
PL_REBOND = 15.0   # rebond >= 15 % au-dessus du contact...
PL_DELAI = 5       # ... dans les 5 seances
PL_MIN = 3         # au moins 3 contacts
PL_CASSE = 8.0     # le plancher est "casse" si un bas passe 8 % en dessous


def plancher(jours: list[dict], courant: float | None = None) -> dict | None:
    """Cherche le niveau bas le plus souvent touche puis rachete. None si rien."""
    n = len(jours)
    if n < 8:
        return None
    bas = [j["bas"] for j in jours]
    meilleur = None
    for niveau in sorted(set(bas)):
        # contacts : seances dont le bas est dans [niveau, niveau x (1+tol)] ;
        # des seances consecutives au plancher ne comptent que pour UN contact
        contacts, i = [], 0
        while i < n:
            if niveau <= bas[i] <= niveau * (1 + PL_TOL / 100):
                debut = i
                while i + 1 < n and bas[i + 1] <= niveau * (1 + PL_TOL / 100):
                    i += 1
                contacts.append((debut, i))
            i += 1
        if len(contacts) < PL_MIN:
            continue
        # plancher casse apres le 1er contact -> ce n'est pas un vrai support
        if min(bas[contacts[0][0]:]) < niveau * (1 - PL_CASSE / 100):
            continue
        rebonds = []
        for debut, fin in contacts:
            suite = jours[fin + 1:fin + 1 + PL_DELAI]
            if not suite:
                continue  # contact en cours : lendemain pas encore joue
            creux = min(bas[debut:fin + 1])
            hausse = (max(j["haut"] for j in suite) - creux) / creux * 100
            rebonds.append({"date": jours[debut]["date"], "i": debut, "bas": creux, "rebond": round(hausse, 1),
                            "sommet": max(j["haut"] for j in suite), "reussi": hausse >= PL_REBOND})
        reussis = [r for r in rebonds if r["reussi"]]
        if len(reussis) < PL_MIN - 1 or len(reussis) / max(1, len(rebonds)) < 0.66:
            continue
        cand = {"niveau": niveau, "contacts": len(contacts), "rebonds": rebonds, "reussis": len(reussis)}
        # on garde le niveau avec le plus de rebonds reussis (puis le plus de contacts)
        if meilleur is None or (cand["reussis"], cand["contacts"]) > (meilleur["reussis"], meilleur["contacts"]):
            meilleur = cand
    if meilleur is None:
        return None

    reussis = [r for r in meilleur["rebonds"] if r["reussi"]]
    plafond = sorted(r["sommet"] for r in reussis)[len(reussis) // 2]  # mediane des sommets atteints
    niveau = meilleur["niveau"]
    prix = courant if courant else jours[-1]["cloture"]
    # objectif REALISTE : le plus bas des sommets atteints = un niveau touche a CHAQUE rebond reussi
    # (revendre au plafond median supposerait de vendre pile au sommet)
    objectif = min(r["sommet"] for r in reussis)
    return {
        "plancher": niveau, "plafond": plafond, "objectif": objectif,
        "regulier": len(reussis) >= PL_MIN,  # 3 rebonds reussis ou plus ; sinon "a confirmer"
        "largeur_pct": round((plafond - niveau) / niveau * 100, 1),
        "contacts": meilleur["contacts"], "reussis": meilleur["reussis"], "testes": len(meilleur["rebonds"]),
        "rebond_moy": round(sum(r["rebond"] for r in reussis) / len(reussis), 1),
        # distance au plancher : proche de 0 = zone d'achat du range
        "distance_pct": round((prix - niveau) / niveau * 100, 1),
        "pres_du_plancher": niveau * (1 - PL_CASSE / 100) <= prix <= niveau * (1 + PL_TOL / 100),
        # le prix actuel est passe sous le plancher : le range n'existe plus (E.P.H. : -91 %)
        "casse": prix < niveau * (1 - PL_CASSE / 100),
        "detail": meilleur["rebonds"],
    }


# ------------------------------------------------ oscillateur support <-> resistance
# Demande du 26/09 : "des trucs comme Erda et Anoto qui font ca en regulier". Mesure sur
# les seances LSX (09/2026) :
#   Erda  : creux 0,056 (08/09) -> 0,073 (11/09) +30 % ; creux 0,046 (23/09) -> 0,063 +37 %
#   Anoto : 0,008 -> 0,014 +75 % ; 0,0074 -> 0,015 +103 % ; 0,011 -> 0,0151 +37 % ; 0,0103 -> 0,0162 +57 %
# Methode "zigzag" : un creux est confirme quand le prix remonte d'au moins OS_HAUSSE % au-dessus,
# un sommet quand il retombe d'au moins OS_BAISSE % en dessous. Ensuite on verifie que les creux
# retombent toujours au meme niveau (support) et les sommets au meme plafond (resistance).
OS_HAUSSE = 25.0     # creux -> sommet minimal pour compter un aller (on filtre ensuite a 30 %)
OS_BAISSE = 15.0     # sommet -> creux minimal pour compter un retour
OS_SERRE = 1.35      # plus haut creux / plus bas creux <= 1,35 : support "regulier"
OS_LACHE = 1.6       # <= 1,6 : support "approximatif" (Anoto : 0,0074 -> 0,011 = 1,49)


def oscillation(jours: list[dict], rebond_min: float = 30.0, hausse: float = OS_HAUSSE, baisse: float = OS_BAISSE) -> dict | None:
    """Allers-retours support <-> resistance d'au moins `rebond_min` %. jours = [{date, bas, haut, cloture}].
    None si moins de 2 allers."""
    if len(jours) < 8:
        return None
    pivots: list[tuple[str, int, float]] = []  # ("creux" | "sommet", index, prix)
    mode, lo_i, hi_i = None, 0, 0
    for i, j in enumerate(jours):
        if mode != "haut":  # on cherche un creux
            if j["bas"] < jours[lo_i]["bas"]:
                lo_i = i
            if j["haut"] >= jours[lo_i]["bas"] * (1 + hausse / 100) and j["haut"] > 0:
                pivots.append(("creux", lo_i, jours[lo_i]["bas"]))
                mode, hi_i = "haut", i
                continue
        if mode == "haut":  # on cherche un sommet
            if j["haut"] > jours[hi_i]["haut"]:
                hi_i = i
            if j["bas"] <= jours[hi_i]["haut"] * (1 - baisse / 100):
                pivots.append(("sommet", hi_i, jours[hi_i]["haut"]))
                mode, lo_i = "bas", i
    if mode == "haut":  # dernier sommet pas encore confirme par une baisse : on le garde (aller en cours)
        pivots.append(("sommet", hi_i, jours[hi_i]["haut"]))

    allers = []
    for (k1, i1, p1), (k2, i2, p2) in zip(pivots, pivots[1:]):
        if k1 == "creux" and k2 == "sommet" and p1 > 0:
            allers.append({"date_creux": jours[i1]["date"], "creux": p1, "date_sommet": jours[i2]["date"], "sommet": p2,
                           "i": i1, "hausse": round((p2 / p1 - 1) * 100, 1), "seances": max(1, i2 - i1)})
    # les petits allers (+25 % d'Anoto le 02/09) fausseraient support et resistance
    allers = [a for a in allers if a["hausse"] >= rebond_min]
    if len(allers) < 2:
        return None

    creux = [a["creux"] for a in allers]
    sommets = [a["sommet"] for a in allers]
    support, resistance = statistics.median(creux), statistics.median(sommets)
    ratio_creux = max(creux) / min(creux)
    ratio_sommets = max(sommets) / min(sommets)
    debuts = [a["i"] for a in allers]
    periode = statistics.median(b - a for a, b in zip(debuts, debuts[1:])) if len(debuts) > 1 else None
    prix = jours[-1]["cloture"]
    return {
        "allers": allers, "n": len(allers),
        "support": support, "resistance": resistance,
        "hausse_med": round(statistics.median(a["hausse"] for a in allers), 1),
        "hausse_min": min(a["hausse"] for a in allers),
        "seances_med": statistics.median(a["seances"] for a in allers),
        "periode": periode,  # seances entre deux creux
        "ratio_creux": round(ratio_creux, 2), "ratio_sommets": round(ratio_sommets, 2),
        "regularite": "reguliere" if ratio_creux <= OS_SERRE and ratio_sommets <= OS_SERRE
                      else "approximative" if ratio_creux <= OS_LACHE and ratio_sommets <= OS_LACHE else "irreguliere",
        # 0 % = au support (zone d'achat), 100 % = a la resistance (zone de vente)
        "position_pct": round(max(0.0, min(100.0, (prix - support) / (resistance - support) * 100)))
                        if resistance > support else None,
        "casse": prix < min(creux) * 0.85,  # passe nettement sous tous les creux : le range est rompu
        "dernier_creux": allers[-1]["date_creux"],
    }


def chute_rebond(jours: list[dict], chute: float = CHUTE, rebond: float = REBOND) -> dict:
    """jours = sortie de picks.jours_depuis_bougies (date, bas, haut, cloture)."""
    occurrences = []
    for i in range(1, len(jours)):
        veille, jour = jours[i - 1]["cloture"], jours[i]["cloture"]
        if veille <= 0:
            continue
        baisse = (jour - veille) / veille * 100
        if baisse > -chute:
            continue
        lendemain = jours[i + 1] if i + 1 < len(jours) else None
        hausse = (lendemain["haut"] - jour) / jour * 100 if lendemain and jour > 0 else None
        occurrences.append({
            "date": jours[i]["date"],
            "chute": round(baisse, 1),
            # None = la chute est la derniere seance connue : lendemain pas encore joue
            "rebond": round(hausse, 1) if hausse is not None else None,
            "reussi": hausse is not None and hausse >= rebond,
        })

    jouees = [o for o in occurrences if o["rebond"] is not None]
    reussies = [o for o in jouees if o["reussi"]]
    return {
        "chutes": len(jouees),
        "rebonds": len(reussies),
        "taux": round(len(reussies) / len(jouees) * 100) if jouees else None,
        "rebond_moyen": round(sum(o["rebond"] for o in reussies) / len(reussies), 1) if reussies else None,
        # la derniere seance est une forte chute dont le lendemain n'est pas encore connu
        "chute_en_cours": bool(occurrences) and occurrences[-1]["rebond"] is None,
        "motif": len(jouees) >= MIN_CHUTES and len(reussies) / len(jouees) >= 0.6,
        "occurrences": occurrences,
    }
