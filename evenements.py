"""Operations sur titre a venir ou recentes : dividendes, regroupements, divisions, fusions,
augmentations de capital.

Pourquoi (cas VerifyMe du 28/09) : le bid LSX etait -21 % sous la cloture Nasdaq de vendredi et
l'outil criait au "decrochage". En realite un dividende de 0,15 $ (~16 % du prix) se detachait
le lendemain, avec un regroupement 1 pour 10 : L&S avait en grande partie RAISON de baisser.

Deux sources, parce qu'aucune ne suffit seule :
  - Yahoo (calendar) : dates OFFICIELLES de detachement / paiement du dividende, resultats.
    Mais ni le montant d'un dividende exceptionnel, ni les regroupements a venir.
  - titres des news (news.chercher) : regroupements, divisions, fusions, augmentations de capital,
    dividendes speciaux, avec le ratio ("1-for-10") et le montant ("$0.15") quand ils sont ecrits.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime

import yfinance as yf

import news
import picks

CACHE_S = 6 * 3600
_CACHE: dict[str, tuple[float, dict]] = {}

MOIS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                     "september", "october", "november", "december"), 1)}
MOIS.update({m: i for i, m in enumerate(("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout",
                                          "septembre", "octobre", "novembre", "decembre"), 1)})

# type -> (motif dans le titre d'une news, libelle, effet mecanique a expliquer)
TYPES = {
    "regroupement": (r"reverse (stock )?split|regroupement|share consolidation|consolidation of (its )?(common )?shares",
                     "Regroupement d'actions",
                     "Le prix est multiplié par le ratio et le nombre d'actions divisé d'autant : neutre pour ta valeur totale. "
                     "Mais c'est souvent le signe d'une action tombée trop bas, souvent suivi de baisses, et les cotations LSX sont chaotiques ce jour-là."),
    "division": (r"(?<!reverse )(?<!reverse stock )\b(stock|share) split\b|forward split|division (du nominal|des actions)",
                 "Division d'actions",
                 "Le prix est divisé par le ratio et le nombre d'actions multiplié d'autant : neutre pour ta valeur totale."),
    "dividende": (r"dividend|dividende",
                  "Dividende",
                  "À la date de détachement, le cours baisse mécaniquement du montant du dividende. Pour le toucher, il faut être "
                  "actionnaire enregistré à la date d'enregistrement ; un achat via Trade Republic / LSX se règle en 2 jours."),
    "fusion": (r"\bmerger\b|\bfusion\b|\bto acquire\b|\bacquisition\b|\btakeover\b|tender offer|offre publique|\bOPA\b|\brachat par\b",
               "Fusion / acquisition",
               "Le cours dépend de la réalisation de l'opération ; une fusion peut s'accompagner d'une émission d'actions (dilution)."),
    "augmentation_capital": (r"augmentation de capital|capital increase|\boffering\b|private placement|registered direct|"
                             r"\bwarrants?\b|\bBSA\b|convertible|\bdilution\b|"
                             # Biophytis 09/2026 : "raises EUR5.3M", "received $2.6 million in funding"
                             r"\braise[sd]?\b.{0,20}(\$|€|eur|million|m\b)|\bfunding\b|lev[ée]e de fonds|\bl[èe]ve\b.{0,20}(m€|millions)",
                             "Augmentation de capital / dilution",
                             "De nouvelles actions arrivent : chaque action existante représente une part plus petite, et le cours "
                             "tend souvent vers le prix d'émission."),
}


def _plat(s: str) -> str:
    return (s.lower().replace("é", "e").replace("è", "e").replace("û", "u").replace("ô", "o")
            .replace("’", "'"))


def _ratio(titre: str) -> str | None:
    m = re.search(r"(\d+)\s*-?\s*for\s*-?\s*(\d+)|(\d+)\s*(?:pour|contre)\s*(\d+)|\b(\d+)\s*:\s*(\d+)\b", titre, re.I)
    if not m:
        return None
    a, b = [g for g in m.groups() if g][:2]
    return f"{a} pour {b}"


def _montant(titre: str) -> str | None:
    m = re.search(r"(\$|€|US\$)\s?(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s?(€|euros?|cents?|\$)(?:\s| par| per|$)", titre, re.I)
    if not m:
        return None
    return (m.group(1) or "") + (m.group(2) or m.group(3)) + ("" if m.group(1) else " " + m.group(4))


def _date_ecrite(titre: str, annee: int) -> str | None:
    """'effective September 29, 2026' / 'le 29 septembre' -> '2026-09-29'."""
    t = _plat(titre)
    m = re.search(r"(" + "|".join(MOIS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?", t) \
        or re.search(r"(\d{1,2})\s+(" + "|".join(MOIS) + r")\s*(\d{4})?", t)
    if not m:
        return None
    g = m.groups()
    mois, jour = (MOIS[g[0]], int(g[1])) if g[0] in MOIS else (MOIS[g[1]], int(g[0]))
    try:
        return date(int(g[2]) if g[2] else annee, mois, jour).isoformat()
    except ValueError:
        return None


def _depuis_news(articles: list[dict]) -> dict[str, dict]:
    trouves: dict[str, dict] = {}
    for a in articles:  # plus recentes d'abord
        titre = a["titre"]
        for type_, (motif, _, _) in TYPES.items():
            if not re.search(motif, titre, re.I):
                continue
            if type_ == "division" and re.search(TYPES["regroupement"][0], titre, re.I):
                continue
            e = trouves.setdefault(type_, {"type": type_, "sources": [], "ratio": None, "montant": None, "date": None})
            if len(e["sources"]) < 3:
                e["sources"].append({"titre": titre, "lien": a["lien"], "source": a["source"], "date": a["date"][:10]})
            e["ratio"] = e["ratio"] or (_ratio(titre) if type_ in ("regroupement", "division") else None)
            e["montant"] = e["montant"] or (_montant(titre) if type_ == "dividende" else None)
            e["date"] = e["date"] or _date_ecrite(titre, int(a["date"][:4]))
            e["special"] = e.get("special") or bool(re.search(r"special|sp[ée]cial|exceptionnel|extraordinary", titre, re.I))
    return trouves


def _depuis_yahoo(isin: str) -> dict:
    import origine  # import tardif : origine importe deja beaucoup de modules
    sym = origine.symbole(isin)
    if not sym:
        return {}
    try:
        cal = yf.Ticker(sym).calendar or {}
    except Exception:
        return {}
    iso = lambda d: d.isoformat() if isinstance(d, (date, datetime)) else None  # noqa: E731
    res = {"symbole": sym, "detachement": iso(cal.get("Ex-Dividend Date")), "paiement": iso(cal.get("Dividend Date"))}
    resultats = cal.get("Earnings Date") or []
    res["resultats"] = iso(resultats[0]) if resultats else None
    return res


def evenements(isin: str, nom: str, articles: list[dict] | None = None) -> dict:
    """{"evenements": [...], "resultats": date} ; chaque evenement : type, libelle, date, nature de la
    date, a_venir, ratio / montant, effet mecanique, sources. Cache 6 h."""
    if isin in _CACHE and time.time() - _CACHE[isin][0] < CACHE_S:
        return _CACHE[isin][1]
    aujourd_hui = datetime.now(picks.PARIS).date().isoformat()
    articles = articles if articles is not None else news.chercher(nom, 20)
    trouves = _depuis_news(articles)
    y = _depuis_yahoo(isin)

    # la date officielle Yahoo prime pour le dividende (detachement = moment ou le cours baisse)
    if y.get("detachement") and y["detachement"] >= _moins_jours(aujourd_hui, 30):
        e = trouves.setdefault("dividende", {"type": "dividende", "sources": [], "ratio": None, "montant": None, "special": False})
        e["date"], e["nature_date"], e["paiement"] = y["detachement"], "détachement", y.get("paiement")

    res = []
    for type_, e in trouves.items():
        _, libelle, effet = TYPES[type_]
        e.setdefault("nature_date", "effet" if e.get("date") else None)
        date_ref = e.get("date") or (e["sources"][0]["date"] if e["sources"] else None)
        res.append({**e, "libelle": libelle + (" exceptionnel" if e.get("special") and type_ == "dividende" else ""),
                    "effet": effet, "date": date_ref,
                    "nature_date": e.get("nature_date") or "annonce",
                    "a_venir": bool(date_ref and date_ref >= aujourd_hui and e.get("nature_date") in ("effet", "détachement"))})
    res.sort(key=lambda e: (not e["a_venir"], e["date"] or ""))
    out = {"evenements": res, "resultats": y.get("resultats"), "symbole": y.get("symbole"), "jour": aujourd_hui}
    _CACHE[isin] = (time.time(), out)
    return out


def _moins_jours(jour: str, n: int) -> str:
    from datetime import timedelta
    return (date.fromisoformat(jour) - timedelta(days=n)).isoformat()


def justifie_ecart(ev: dict, jours: int = 3) -> list[dict]:
    """Dividende qui se detache, regroupement ou division effectifs dans les `jours` prochains jours
    (ou juste passes) : un ecart LSX / marche d'origine est alors en partie MECANIQUE, pas un
    "decrochage". Dividende en premier : c'est lui qui fait baisser le cours."""
    if not ev:
        return []
    auj = date.fromisoformat(ev["jour"])
    ordre = {"dividende": 0, "regroupement": 1, "division": 2}
    res = [e for e in ev["evenements"] if e["type"] in ordre and e.get("date") and e.get("nature_date") != "annonce"
           and -1 <= (date.fromisoformat(e["date"]) - auj).days <= jours]
    return sorted(res, key=lambda e: ordre[e["type"]])
