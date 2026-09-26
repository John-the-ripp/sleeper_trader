"""Explications courtes par le LLM : "pourquoi ce titre a chute / monte ?".

Sert a deux vues :
  - Chutes & hausses : la cause du mouvement du jour, pour le haut du classement ;
  - Rebonds du jour  : les titres qui ont chute >= 10 % HIER et remonte >= 10 %
    AUJOURD'HUI, avec la cause de la chute ET celle du rebond.

Difference avec analyste.py : ici on veut une reponse COURTE et STRUCTUREE
(JSON : categorie, cause, confiance) pour des dizaines de titres, affichable
dans une ligne de tableau. analyste.py fait l'analyse longue d'un seul titre.

Une explication est gardee pour la journee (fichier explications/<date>.json) :
le LLM n'est rappele que pour les nouveaux titres, ou sur demande (force).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta

import analyste
import news
import picks
from tr_data import BASE

DOSSIER = BASE / "explications"
CHUTE_HIER = -10.0    # rapport rebonds : variation d'hier <= -10 %
REBOND_AUJ = 10.0     # ... et variation du jour >= +10 %
TOP_AUTO = 15         # explications automatiques : les 15 plus grosses chutes et hausses
ALERTE_CHUTE = -15.0  # alerte navigateur : chute du jour <= -15 %
ALERTE_WATCH = -10.0  # ... ou <= -10 % pour un titre de la watchlist
FICHIER_ALERTES = BASE / "alertes.json"
CATEGORIES = ("news", "secteur", "technique", "artefact", "inconnu")

ETAT: dict = {"en_cours": False, "fait": 0, "total": 0, "erreur": None}
_verrou = asyncio.Lock()

SYSTEME = """Tu expliques en une phrase pourquoi une action a bougé, pour un tableau de bord boursier.
Utilise UNIQUEMENT les données fournies (cours datés, news datées). N'invente rien.
Réponds par un objet JSON et rien d'autre, avec exactement ces clés :
{"categorie": "news" | "secteur" | "technique" | "artefact" | "inconnu",
 "cause_jour": "cause du mouvement du jour, 20 mots max, cite la source entre parenthèses si une news l'explique",
 "cause_veille": "cause du mouvement de la veille, 20 mots max, ou null si non demandé",
 "confiance": "haute" | "moyenne" | "faible"}
Catégories : "news" = une news datée du jour ou de la veille explique le mouvement ; "secteur" = tout le secteur ou le marché a bougé
(d'après les news) ; "technique" = aucune news, rebond ou prise de bénéfices après un gros mouvement ; "artefact" = mouvement
incohérent avec les news et un spread large ou des cotations erratiques, probable erreur de cotation ; "inconnu" = impossible à dire.
Une news vieille de plus de 3 jours n'explique pas un mouvement du jour, sauf si elle annonce un événement daté de ce jour."""


def _fichier(jour: str):
    return DOSSIER / f"{jour}.json"


def charger(jour: str) -> dict:
    try:
        return json.loads(_fichier(jour).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _sauver(jour: str, data: dict) -> None:
    DOSSIER.mkdir(exist_ok=True)
    _fichier(jour).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _json_du_llm(texte: str) -> dict:
    """Les modeles entourent parfois le JSON de ```json ... ``` ou de texte :
    on prend le premier bloc { ... } et on valide les champs."""
    m = re.search(r"\{.*\}", texte, re.DOTALL)
    try:
        d = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        d = {}
    cat = d.get("categorie") if d.get("categorie") in CATEGORIES else "inconnu"
    conf = d.get("confiance") if d.get("confiance") in ("haute", "moyenne", "faible") else "faible"
    return {"categorie": cat, "cause_jour": d.get("cause_jour") or texte[:160],
            "cause_veille": d.get("cause_veille"), "confiance": conf}


def _prompt(l: dict, articles: list[dict], avec_veille: bool) -> str:
    c = l["clotures"][-6:]
    lignes = [
        f"Titre : {l['nom']} (ISIN {l['isin']}), séance du {l['date_jour']}",
        f"Mouvement du jour : {l['var_1j']:+.1f} % (de {l['ref_1j']} à {l['courant']} EUR)",
    ]
    if l.get("var_veille") is not None:
        lignes.append(f"Mouvement de la veille ({l['date_1j']}) : {l['var_veille']:+.1f} %")
    lignes.append(f"Dernières clôtures de séance : {', '.join(str(x) for x in c)}")
    lignes.append(f"Spread actuel : {l['spread_pct']} %" + (" ; bourse d'origine hors fuseau européen" if l.get("hors_fuseau") else ""))
    lignes.append("Explique aussi le mouvement de la veille (cause_veille)." if avec_veille else "cause_veille = null.")
    lignes.append(f"\nNews récentes ({len(articles)}) :")
    lignes += [f"- {a['date'][:10]} | {a['source']} | {a['titre']}" for a in articles] or ["(aucune news trouvée)"]
    return "\n".join(lignes)


async def expliquer(l: dict, avec_veille: bool = False, force: bool = False) -> dict:
    """l = une ligne de mouvements.json. Resultat garde pour la journee."""
    jour = l["date_jour"]
    deja = charger(jour)
    if not force and l["isin"] in deja:
        return deja[l["isin"]]

    articles = await asyncio.to_thread(news.chercher, l["nom"], 10)
    # on ne garde que les 10 derniers jours : au-dela, ca n'explique pas un mouvement du jour
    limite = (datetime.fromisoformat(jour) - timedelta(days=10)).date().isoformat()
    articles = [a for a in articles if a["date"][:10] >= limite]
    try:
        texte = await asyncio.to_thread(analyste._llm, [
            {"role": "system", "content": SYSTEME},
            {"role": "user", "content": _prompt(l, articles, avec_veille)},
        ])
        res = _json_du_llm(texte)
    except Exception as exc:
        res = {"categorie": "inconnu", "cause_jour": None, "cause_veille": None, "confiance": "faible",
               "erreur": f"{type(exc).__name__}: {exc}"[:200]}
    res.update(isin=l["isin"], var_1j=l["var_1j"], var_veille=l.get("var_veille"),
               heure=datetime.now(picks.PARIS).isoformat(timespec="seconds"), news=articles[:4])

    if not res.get("erreur"):  # une erreur reseau ne doit pas bloquer un nouvel essai
        deja = charger(jour)  # relu : un autre appel a pu ecrire entre-temps
        deja[l["isin"]] = res
        _sauver(jour, deja)
    return res


# ------------------------------------------------------------ selections

def _propre(l: dict) -> bool:
    return l.get("var_1j") is not None and l["spread_pct"] <= 20 and l.get("trou_jours", 0) <= 4


def candidats_rebond(lignes: list[dict]) -> list[dict]:
    """Chute >= 10 % hier ET hausse >= 10 % aujourd'hui."""
    c = [l for l in lignes if _propre(l) and (l.get("var_veille") or 0) <= CHUTE_HIER and l["var_1j"] >= REBOND_AUJ]
    return sorted(c, key=lambda l: -l["var_1j"])


def a_expliquer(lignes: list[dict]) -> list[tuple[dict, bool]]:
    """(ligne, avec_veille) : les rebonds d'abord, puis les tops chutes/hausses."""
    rebonds = candidats_rebond(lignes)
    propres = [l for l in lignes if _propre(l) and not l.get("hors_fuseau")]
    chutes = sorted([l for l in propres if l["var_1j"] < 0], key=lambda l: l["var_1j"])[:TOP_AUTO]
    hausses = sorted([l for l in propres if l["var_1j"] > 0], key=lambda l: -l["var_1j"])[:TOP_AUTO]
    suivis = set(_watchlist())
    watch = [l for l in lignes if l["isin"] in suivis and _propre(l) and l["var_1j"] <= ALERTE_WATCH]
    vus, out = set(), []
    for l, veille in [(l, True) for l in rebonds] + [(l, False) for l in watch + chutes + hausses]:
        if l["isin"] not in vus:
            vus.add(l["isin"])
            out.append((l, veille))
    return out


async def tour_explications() -> None:
    """Lance apres chaque passage du screener : explique ce qui ne l'est pas encore."""
    if _verrou.locked():
        return
    async with _verrou:
        rapport_rebonds()  # memorise la liste des rebonds du jour sur disque
        lignes = picks.charger_mouvements()["lignes"]
        todo = a_expliquer(lignes)
        if not todo:
            return
        deja = charger(todo[0][0]["date_jour"])
        todo = [(l, v) for l, v in todo if l["isin"] not in deja]
        ETAT.update(en_cours=True, fait=0, total=len(todo), erreur=None)
        suivis = set(_watchlist())
        try:
            for l, veille in todo:
                e = await expliquer(l, avec_veille=veille)
                ETAT["fait"] += 1
                # Alertes : seulement pour les titres NOUVEAUX de ce tour (deja filtres)
                if veille:
                    alerter("rebond", l, e)
                elif l["isin"] in suivis and l["var_1j"] <= ALERTE_WATCH:
                    alerter("watchlist", l, e)
                elif l["var_1j"] <= ALERTE_CHUTE:
                    alerter("chute", l, e)
        except Exception as exc:
            ETAT["erreur"] = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            ETAT["en_cours"] = False


# ---------------------------------------------------------------- alertes

def _watchlist() -> list[str]:
    try:
        return json.loads((BASE / "watchlist.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def charger_alertes() -> list[dict]:
    try:
        return json.loads(FICHIER_ALERTES.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def alerter(type_: str, l: dict, e: dict, cle: str | None = None, extra: dict | None = None) -> None:
    """Ajoute une alerte (une seule par type, titre et jour). Le navigateur
    les recupere via /api/alertes et bipe pour chaque nouvel id."""
    alertes = charger_alertes()
    cle = cle or f"{type_}:{l['isin']}:{l['date_jour']}"
    if any(a["cle"] == cle for a in alertes):
        return
    alertes.append({
        "id": (alertes[-1]["id"] + 1) if alertes else 1, "cle": cle, "type": type_,
        "isin": l["isin"], "nom": l["nom"], "jour": l["date_jour"],
        "var_1j": l["var_1j"], "var_veille": l.get("var_veille"), "courant": l["courant"],
        "categorie": e.get("categorie"), "cause": e.get("cause_jour"), "cause_veille": e.get("cause_veille"),
        "heure": datetime.now(picks.PARIS).isoformat(timespec="seconds"), **(extra or {}),
    })
    FICHIER_ALERTES.write_text(json.dumps(alertes[-300:], ensure_ascii=False), encoding="utf-8")


async def journal_cotes() -> None:
    """Bid/ask des titres DOWN/UP et de la watchlist a chaque tour : pour savoir, dans
    quelques semaines, si l'ASK decroche aussi quand le bid LSX chute."""
    import origine
    suivis = set(_watchlist())
    lignes = [l for l in picks.charger_downup()["lignes"] if l["downup"] or l["isin"] in suivis]
    await asyncio.to_thread(origine.journaliser, lignes)


async def prechauffer_origine() -> None:
    """Ecart LSX / vrai marche des titres type Anoto + watchlist, calcule a l'avance."""
    import origine
    suivis = set(_watchlist())
    isins = [l["isin"] for l in picks.charger_downup()["lignes"] if l["downup"]] + list(suivis)
    await asyncio.to_thread(origine.prechauffer, list(dict.fromkeys(isins)))


async def alertes_downup() -> None:
    """Apres chaque tour : un titre DOWN/UP (type Anoto) qui vient de faire une
    grosse chute pas encore rachetee = le moment que guette ce motif."""
    data = picks.charger_downup()
    jour = datetime.now(picks.PARIS).date().isoformat()
    for l in data["lignes"]:
        if not (l["downup"] and l["en_phase_down"] and l["spread_pct"] <= 20):
            continue
        c = l["detail"][-1]
        message = (f"Chute {c['chute']:+.0f} % pas encore rachetée · historique {l['reussis']}/{l['cycles']} rebonds "
                   f"(moy. {l['rebond_moy']:+.0f} % en {l['delai_moy']:g} séance(s))")
        alerter("downup", {"isin": l["isin"], "nom": l["nom"], "date_jour": jour, "var_1j": c["chute"],
                           "var_veille": None, "courant": l["bid"]},
                {"categorie": None, "cause_jour": message, "cause_veille": None})


async def alertes_plancher() -> None:
    """Un titre en range revient toucher son plancher = zone d'achat du range."""
    import simulateur  # import local : evite une boucle d'imports au demarrage
    jour = datetime.now(picks.PARIS).date().isoformat()
    for l in simulateur.ranges(spread_max=20)["lignes"]:
        if not l["pres_du_plancher"] or l["gain_cycle_pct"] <= 0:
            continue
        message = (f"Au plancher {l['plancher']:g} € ({l['reussis']}/{l['testes']} rebonds, moy. +{l['rebond_moy']:g} %) · "
                   f"plafond {l['plafond']:g} € · gain net par cycle ≈ {l['gain_cycle_pct']:+g} % après spread")
        alerter("plancher", {"isin": l["isin"], "nom": l["nom"], "date_jour": jour, "var_1j": l["distance_pct"],
                             "var_veille": None, "courant": l["bid"]},
                {"categorie": None, "cause_jour": message, "cause_veille": None})


def rapport_rebonds(jour: str | None = None) -> dict:
    """Rapport du jour ; pour un jour passe, on relit le fichier sauve."""
    lignes = picks.charger_mouvements()["lignes"]
    aujourd_hui = next((l["date_jour"] for l in lignes if l.get("date_jour")), None)
    jour = jour or aujourd_hui
    expl = charger(jour) if jour else {}
    if jour == aujourd_hui:
        cands = candidats_rebond(lignes)
        # memorise la liste du jour pour pouvoir la relire les jours suivants
        if cands and jour:
            DOSSIER.mkdir(exist_ok=True)
            (DOSSIER / f"rebonds_{jour}.json").write_text(json.dumps(cands, ensure_ascii=False), encoding="utf-8")
    else:
        try:
            cands = json.loads((DOSSIER / f"rebonds_{jour}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            cands = []
    return {"jour": jour, "aujourd_hui": aujourd_hui, "seuils": {"chute_hier": CHUTE_HIER, "rebond_auj": REBOND_AUJ},
            "lignes": [{**l, "explication": expl.get(l["isin"])} for l in cands], "etat": ETAT}


def jours_disponibles() -> list[str]:
    if not DOSSIER.exists():
        return []
    return sorted((f.stem.removeprefix("rebonds_") for f in DOSSIER.glob("rebonds_*.json")), reverse=True)
