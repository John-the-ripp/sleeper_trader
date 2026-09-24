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

CHUTE = 15.0     # % de baisse cloture/cloture pour parler de "forte chute"
REBOND = 10.0    # % de hausse le lendemain (sur le haut de seance) pour parler de rebond
MIN_CHUTES = 2   # en dessous, pas de motif : une seule occurrence n'est pas une habitude


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
