"""Tweets qui parlent d'un titre, via FxTwitter (api.fxtwitter.com : gratuit, sans compte).

Deux sources, dans cet ordre :
  1. /2/search : la vraie recherche X. CASSEE depuis juillet 2026 (404 pour
     toutes les requetes, issue FxEmbed #2303). On l'essaie quand meme : le
     jour ou elle remarche, elle est utilisee sans rien changer.
  2. Timelines de comptes suivis (/2/profile/{compte}/statuses, ca marche) :
     - les medias boursiers de comptes_x.json["medias"], filtres sur le nom du titre ;
     - le compte officiel de la societe (comptes_x.json["titres"][isin]), en entier.

Limite : seuls les ~20 derniers tweets de chaque media sont lus. Pour un gros
compte (BFM Bourse), ca couvre environ une journee.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from news import nom_de_recherche
from tr_data import BASE

API = "https://api.fxtwitter.com/2"
UA = {"User-Agent": "sleeper-trader/0.1 (usage perso)"}
CONFIG = BASE / "comptes_x.json"
FLUX = {"crypto": "Crypto", "traders": "Traders"}  # cle de comptes_x.json -> nom affiche
CACHE_S = 600
_CACHE: dict[str, tuple[float, list[dict]]] = {}  # url -> (instant, tweets)

# Formes juridiques en fin de nom TR : "Toosla SA" doit matcher "Toosla" dans un tweet
FORME = re.compile(r"\s+(S\.?A\.?|S\.?E\.?|N\.?V\.?|AG|AB|ASA|Inc\.?|Corp\.?|plc|Ltd\.?|SpA|Oyj)$", re.IGNORECASE)


def _config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"medias": [], "titres": {}}


def _plat(s: str) -> str:
    """minuscules sans accents : 'Société' -> 'societe'"""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _get(chemin: str, params: dict | None = None, frais: bool = False) -> list[dict]:
    url = API + chemin + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
    if not frais and url in _CACHE and time.time() - _CACHE[url][0] < CACHE_S:
        return _CACHE[url][1]
    try:
        r = requests.get(API + chemin, params=params, headers=UA, timeout=12)
        d = r.json()
    except (requests.RequestException, ValueError):
        return []
    res = [x for x in d.get("results") or [] if x.get("type") == "status"] if d.get("code") == 200 else []
    _CACHE[url] = (time.time(), res)
    return res


def _format(x: dict) -> dict:
    quand = datetime.fromtimestamp(x["created_timestamp"], timezone.utc)
    return {"date": quand.isoformat(), "compte": (x.get("author") or {}).get("screen_name", "?"),
            "texte": x.get("text", ""), "lien": x.get("url", ""),
            "likes": x.get("likes", 0), "reposts": x.get("reposts", 0), "vues": x.get("views"),
            "id": x.get("id"), "liens": _liens(x.get("text", "")), "images": _images(x),
            "tags": list(dict.fromkeys(re.findall(r"[#$]([A-Za-z][A-Za-z0-9_]{1,19})", x.get("text", ""))))[:6]}


def _images(x: dict) -> list[dict]:
    """Photos du tweet (graphiques...) + miniature des videos. `mini` pour l'affichage,
    `url` en taille d'origine pour le clic."""
    media = x.get("media") or {}
    imgs = [{"url": p["url"], "mini": re.sub(r"name=\w+", "name=small", p["url"]) if "name=" in p["url"] else p["url"]}
            for p in media.get("photos") or [] if p.get("url")]
    imgs += [{"url": v.get("url") or v["thumbnail_url"], "mini": v["thumbnail_url"], "video": True}
             for v in media.get("videos") or [] if v.get("thumbnail_url")]
    return imgs[:4]


def _liens(texte: str) -> list[str]:
    """Liens cites dans le tweet (article, communique), sans les liens vers X lui-meme."""
    return [u.rstrip(".,;:!?)»") for u in re.findall(r"https?://\S+", texte)
            if not re.match(r"https?://(www\.)?(x|twitter)\.com/", u)][:3]


def chercher(nom: str, isin: str | None = None, jours: int = 30, limite: int = 15) -> list[dict]:
    """Tweets des `jours` derniers jours sur le titre, plus recents d'abord."""
    mot = FORME.sub("", nom_de_recherche(nom)).strip()
    motif = re.compile(r"\b" + re.escape(_plat(mot)) + r"\b")
    cfg = _config()

    bruts = _get("/search", {"q": f'"{mot}"', "feed": "latest", "count": 30})
    for compte in cfg.get("medias", []):
        bruts += [x for x in _get(f"/profile/{compte}/statuses") if motif.search(_plat(x.get("text", "")))]
    officiel = (cfg.get("titres") or {}).get(isin or "")
    if officiel:
        bruts += _get(f"/profile/{officiel}/statuses")

    depuis = datetime.now(timezone.utc) - timedelta(days=jours)
    vus, tweets = set(), []
    for x in bruts:
        if x.get("id") in vus or not x.get("created_timestamp"):
            continue
        vus.add(x["id"])
        t = _format(x)
        if datetime.fromisoformat(t["date"]) >= depuis:
            tweets.append(t)
    tweets.sort(key=lambda t: t["date"], reverse=True)
    return tweets[:limite]


# ---------------------------------------------------------------- apercu des liens (article)
# Les tweets ne contiennent que des liens raccourcis (dlvr.it, ebx.sh, l.bfmtv.com). On suit le
# lien jusqu'a l'article et on lit ses balises Open Graph (titre, resume, image) : c'est ce que
# X affiche sous un tweet. Cache permanent : un lien n'est resolu qu'une fois.
FICHIER_LIENS = BASE / "tweets_liens.json"
NAVIGATEUR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
              "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}
_LIENS: dict | None = None


_BALISE_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTRIBUT = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")


def _metas(page: str) -> dict[str, str]:
    """{og:title: ..., description: ...} de toutes les balises <meta>, en UN passage lineaire.
    Bug du 26/09 : une regex `<meta[^>]+...content=(["'])(.*?)\\1` avec re.S parcourait toute la
    page depuis chaque <meta (quadratique sur 400 Ko) sans relacher le GIL -> serveur gele."""
    res: dict[str, str] = {}
    for balise in _BALISE_META.findall(page[:400_000]):
        attrs = {m.group(1).lower(): next(v for v in m.groups()[1:] if v is not None) for m in _ATTRIBUT.finditer(balise)}
        cle = (attrs.get("property") or attrs.get("name") or "").lower()
        if cle and "content" in attrs and cle not in res:
            res[cle] = html.unescape(attrs["content"]).strip()
    return res


def _meta(metas: dict[str, str], nom: str) -> str | None:
    return metas.get(nom) or None


def _lire_page(r: requests.Response, max_octets: int = 400_000, max_s: float = 10.0) -> str:
    """Lit AU PLUS max_octets en max_s secondes. Bug du 26/09 : `r.text` telechargeait tout
    (video, flux sans fin) puis lancait la detection d'encodage sur des Mo -> calcul qui garde
    le GIL et gele TOUT le serveur. timeout=8 ne borne que l'attente entre deux paquets."""
    debut, morceaux, taille = time.monotonic(), [], 0
    for bloc in r.iter_content(16_384):
        morceaux.append(bloc)
        taille += len(bloc)
        if taille >= max_octets or time.monotonic() - debut > max_s:
            break
    r.close()
    # sans charset dans l'en-tete, requests suppose ISO-8859-1 : les accents seraient casses
    brut = b"".join(morceaux)[:max_octets]
    enc = r.encoding if "charset" in r.headers.get("Content-Type", "").lower() else None
    if not enc:  # sinon <meta charset="..."> dans les 4 premiers Ko (ABC Bourse : "modèles" -> "mod?les")
        m = re.search(rb"charset=[\"']?([\w-]+)", brut[:4096], re.I)
        enc = m.group(1).decode() if m else "utf-8"
    try:
        return brut.decode(enc, errors="replace")
    except LookupError:
        return brut.decode("utf-8", errors="replace")


def _resoudre(url: str) -> dict:
    try:
        r = requests.get(url, headers=NAVIGATEUR, timeout=8, allow_redirects=True, stream=True)
        if "html" not in r.headers.get("Content-Type", "html").lower():  # image, video, pdf : pas d'apercu
            r.close()
            return {"url": r.url, "titre": None}
        page = _lire_page(r)
    except requests.RequestException:
        return {"url": url, "titre": None}
    metas = _metas(page)
    fin = _meta(metas, "og:url") or r.url
    ap = {"url": fin if fin.startswith("http") else r.url, "titre": _meta(metas, "og:title") or _meta(metas, "twitter:title"),
          "resume": (_meta(metas, "og:description") or _meta(metas, "description") or "")[:300] or None,
          "image": _meta(metas, "og:image"), "site": _meta(metas, "og:site_name")}
    if not ap["titre"] and re.search(r"(youtube\.com|youtu\.be)/", r.url):  # YouTube cache ses balises : oEmbed
        try:
            o = requests.get("https://www.youtube.com/oembed", params={"url": r.url, "format": "json"}, timeout=8).json()
            ap.update(titre=o.get("title"), image=o.get("thumbnail_url"), site="YouTube · " + (o.get("author_name") or ""))
        except (requests.RequestException, ValueError):
            pass
    if not ap["site"]:
        ap["site"] = re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*", r"\1", ap["url"]))
    return ap


def apercus(urls: list[str]) -> dict[str, dict]:
    """url courte -> apercu {url, titre, resume, image, site}. Resout en parallele ce qui manque."""
    global _LIENS
    if _LIENS is None:
        try:
            _LIENS = json.loads(FICHIER_LIENS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _LIENS = {}
    manquants = [u for u in dict.fromkeys(urls) if u not in _LIENS]
    if manquants:
        with ThreadPoolExecutor(8) as ex:
            for u, ap in zip(manquants, ex.map(_resoudre, manquants)):
                _LIENS[u] = ap
        FICHIER_LIENS.write_text(json.dumps(_LIENS, ensure_ascii=False), encoding="utf-8")
    return {u: _LIENS[u] for u in urls if u in _LIENS}


def _ajouter_apercus(groupes: list[dict]) -> None:
    tous = [t for g in groupes for t in g["tweets"]]
    ap = apercus([u for t in tous for u in t.get("liens", [])])
    for t in tous:
        t["apercus"] = [ap[u] for u in t.get("liens", []) if ap.get(u, {}).get("titre")]


# ---------------------------------------------------------------- onglet "News Twitter · signaux"
# Sens inverse de chercher() : on lit TOUS les comptes suivis et on repere les
# titres de l'univers TR cites dans chaque tweet.

# Noms TR qui sont aussi des mots courants : trop de faux positifs
MOTS_COURANTS = {
    "global", "energy", "energie", "gold", "silver", "digital", "capital", "group", "groupe", "holding", "holdings",
    "international", "technology", "technologies", "solutions", "systems", "industries", "resources", "mining",
    "invest", "finance", "bank", "banque", "trust", "media", "health", "sante", "pharma", "bio", "green", "power",
    "next", "vision", "alpha", "beta", "delta", "omega", "nova", "apex", "summit", "target", "zoom", "block", "match",
    "public", "premier", "first", "united", "general", "national", "american", "europe", "europa", "france", "china",
    "africa", "asia", "pacific", "atlantic", "north", "south", "east", "west", "marche", "bourse", "record", "leader",
    "value", "growth", "future", "smart", "impact", "select", "prime", "plus", "pro", "one", "sun", "star", "sky",
    "orange", "total", "via", "victoria", "nasdaq", "euronext", "bourse direct",
}
MIN_LETTRES = 5

POSITIF = ("hausse", "bondit", "flambe", "s'envole", "grimpe", "rebond", "contrat", "commande", "releve", "objectif de cours",
           "rachat d'actions", "acquisition", "benefice", "record", "surperform", "achat", "upgrade", "beat", "soars", "jumps",
           "surges", "rallies", "partenariat", "autorisation", "approbation", "fda", "succes")
NEGATIF = ("baisse", "chute", "plonge", "recule", "degringol", "abaisse", "augmentation de capital", "dilution",
           "avertissement", "perte nette", "creuse sa perte", "redressement", "sauvegarde", "liquidation", "suspension", "sous-perform", "vente",
           "downgrade", "plunges", "falls", "drops", "profit warning", "enquete", "amende", "retard", "echec")

_INDEX: dict = {"n": 0, "regex": None, "cle_isin": {}}


def _index(noms: dict[str, str], prefere: set[str]) -> tuple[re.Pattern | None, dict[str, str]]:
    """Une seule regex pour les ~11 000 noms TR (du plus long au plus court)."""
    if _INDEX["n"] == len(noms) and _INDEX["regex"] is not None:
        return _INDEX["regex"], _INDEX["cle_isin"]
    cle_isin: dict[str, str] = {}
    for isin, nom in noms.items():
        cle = _plat(FORME.sub("", nom_de_recherche(nom)).strip(" -,."))
        if len(re.sub(r"[^a-z]", "", cle)) < MIN_LETTRES or cle in MOTS_COURANTS:
            continue
        # meme nom pour plusieurs ISIN (ADR, lignes etrangeres) : on garde celui qui a des cotations
        if cle not in cle_isin or (isin in prefere and cle_isin[cle] not in prefere):
            cle_isin[cle] = isin
    alternance = "|".join(re.escape(c) for c in sorted(cle_isin, key=len, reverse=True))
    regex = re.compile(r"(?<![a-z0-9])(" + alternance + r")(?![a-z0-9])") if alternance else None
    _INDEX.update(n=len(noms), regex=regex, cle_isin=cle_isin)
    return regex, cle_isin


def _ton(texte_plat: str) -> tuple[str, list[str]]:
    pos = [m for m in POSITIF if m in texte_plat]
    neg = [m for m in NEGATIF if m in texte_plat]
    if len(pos) > len(neg):
        return "positif", pos
    if len(neg) > len(pos):
        return "negatif", neg
    return "neutre", pos + neg


def signaux(noms: dict[str, str], cotations: dict[str, dict], jours: int = 7) -> dict:
    """Titres TR cites dans les tweets des comptes suivis, regroupes par titre.

    noms : isin -> nom TR ; cotations : isin -> ligne de mouvements.json (prix, variations).
    Le "ton" vient de mots-cles (hausse, contrat, augmentation de capital...) : c'est
    un indice grossier, pas une analyse."""
    cfg = _config()
    regex, cle_isin = _index(noms, set(cotations))
    officiels = {compte.lower(): isin for isin, compte in (cfg.get("titres") or {}).items()}
    comptes = list(dict.fromkeys(cfg.get("medias", []) + list((cfg.get("titres") or {}).values())))
    depuis = datetime.now(timezone.utc) - timedelta(days=jours)

    par_titre: dict[str, dict] = {}
    lus, vus = 0, set()
    for compte in comptes:
        for x in _get(f"/profile/{compte}/statuses"):
            if x.get("id") in vus or not x.get("created_timestamp"):
                continue
            vus.add(x["id"])
            lus += 1
            t = _format(x)
            if datetime.fromisoformat(t["date"]) < depuis:
                continue
            brut = t["texte"]
            # caractere par caractere : meme longueur que le texte d'origine (emojis -> espace)
            plat = "".join((_plat(ch) or " ")[:1] for ch in brut)
            isins = set()
            if regex:
                for m in regex.finditer(plat):
                    # nom propre : 1re lettre en majuscule dans le texte d'origine
                    # (evite "orange" la couleur, un nom dans une url...)
                    if not brut[m.start()].isupper():
                        continue
                    isins.add(cle_isin[m.group(1)])
            if t["compte"].lower() in officiels:
                isins.add(officiels[t["compte"].lower()])
            if not isins:
                continue
            ton, mots = _ton(plat)
            for isin in isins:
                g = par_titre.setdefault(isin, {"isin": isin, "nom": noms.get(isin, isin), "tweets": []})
                g["tweets"].append({**t, "ton": ton, "mots": mots})

    lignes = []
    for isin, g in par_titre.items():
        g["tweets"].sort(key=lambda t: t["date"], reverse=True)
        tons = [t["ton"] for t in g["tweets"]]
        g["signal"] = ("positif" if tons.count("positif") > tons.count("negatif")
                       else "negatif" if tons.count("negatif") > tons.count("positif") else "neutre")
        g["dernier"] = g["tweets"][0]["date"]
        g["engagement"] = sum(t["likes"] + t["reposts"] for t in g["tweets"])
        c = cotations.get(isin) or {}
        g.update({k: c.get(k) for k in ("bid", "ask", "spread_pct", "courant", "var_1j", "var_5j")})
        lignes.append(g)
    lignes.sort(key=lambda g: g["dernier"], reverse=True)

    # flux "crypto" et "traders" : tout ce que ces comptes publient, sans filtre sur les noms TR
    # (ils parlent en #Bitcoin / $NET, pas avec le nom complet de la societe)
    flux = {}
    for cat in FLUX:
        posts = []
        for compte in cfg.get(cat, []):
            for x in _get(f"/profile/{compte}/statuses"):
                if x.get("id") in vus or not x.get("created_timestamp"):
                    continue
                vus.add(x["id"])
                lus += 1
                t = _format(x)
                if datetime.fromisoformat(t["date"]) >= depuis:
                    posts.append({**t, "ton": "neutre", "mots": []})
        posts.sort(key=lambda t: t["date"], reverse=True)
        flux[cat] = {"isin": cat.upper(), "nom": FLUX[cat], "comptes": cfg.get(cat, []), "tweets": posts}

    _ajouter_apercus(lignes + list(flux.values()))
    return {"heure": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "comptes": comptes, "tweets_lus": lus, "jours": jours, "lignes": lignes, "flux": flux}


def _groupes(data: dict) -> list[dict]:
    """Groupes a faire lire par le LLM : un par action + un par flux (crypto, traders)."""
    return data["lignes"] + [f for f in data.get("flux", {}).values() if f["tweets"]]


def _sujet(g: dict, t: dict) -> str:
    if g["isin"] not in ("CRYPTO", "TRADERS"):
        return f"titre concerné : {g['nom']}"
    return g["nom"].lower() + (f" ({', '.join(t['tags'])})" if t.get("tags") else "")


# ---------------------------------------------------------------- notification a chaque tweet
# Comptes de comptes_x.json["notifier"] : toutes les 2 min on relit leur timeline ; chaque tweet
# jamais vu devient une alerte "tweet" (bip + notification Chrome via /api/alertes, comme les
# autres alertes). Les id deja vus sont dans tweets_vus.json. Au 1er passage sur un compte on
# enregistre l'existant SANS notifier (sinon 20 notifications d'un coup).

NOTIF_S = 120
FICHIER_VUS = BASE / "tweets_vus.json"


def _vus() -> dict[str, list[str]]:
    try:
        return json.loads(FICHIER_VUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def verifier_nouveaux() -> int:
    """Un passage : cree une alerte par nouveau tweet des comptes a notifier. Renvoie leur nombre."""
    import explications  # import tardif : evite un import circulaire au demarrage
    vus, n = _vus(), 0
    for compte in _config().get("notifier", []):
        cle = compte.lower()
        posts = [x for x in _get(f"/profile/{compte}/statuses", frais=True)
                 if (x.get("author") or {}).get("screen_name", "").lower() == cle]  # ses tweets, pas ses reposts
        if not posts:
            continue  # compte vide ou FxTwitter en panne : on reessaie au prochain passage
        deja = set(vus.get(cle, []))
        if cle in vus:
            for x in sorted(posts, key=lambda x: x.get("created_timestamp", 0)):
                if x["id"] in deja:
                    continue
                t = _format(x)
                texte = " ".join(t["texte"].split())
                explications.alerter(
                    "tweet",
                    {"isin": "", "nom": "@" + t["compte"], "date_jour": t["date"][:10], "var_1j": None, "courant": None},
                    {"cause_jour": texte[:280]},
                    cle=f"tweet:{x['id']}",
                    extra={"lien": t["lien"], "image": (t["images"][0]["mini"] if t["images"] else None)},
                )
                n += 1
        vus[cle] = [x["id"] for x in posts] + [i for i in vus.get(cle, []) if i not in {x["id"] for x in posts}][:200]
    FICHIER_VUS.write_text(json.dumps(vus), encoding="utf-8")
    return n


async def boucle_notif() -> None:
    import asyncio
    while True:
        try:
            await asyncio.to_thread(verifier_nouveaux)
        except Exception as exc:  # une panne FxTwitter ne doit pas tuer la boucle
            print("tweets.boucle_notif :", type(exc).__name__, exc)
        await asyncio.sleep(NOTIF_S)


# ---------------------------------------------------------------- lecture par le LLM
# Le LLM lit chaque tweet et extrait ce que l'utilisateur cherche (par defaut : predictions,
# objectifs de cours, recommandations). Resultats gardes dans tweets_llm.json par
# (consigne, tweet, titre) : un tweet deja lu ne repart jamais au LLM.

CONSIGNE_DEFAUT = "prédictions de cours, objectifs de cours, recommandations d'achat ou de vente, avis d'analystes"
FICHIER_LLM = BASE / "tweets_llm.json"
PAQUET = 20  # tweets par appel LLM
TYPES_IA = ("prediction", "objectif", "recommandation", "resultats", "news", "technique", "avis", "autre")

SYSTEME_LLM = """Tu lis des tweets boursiers pour un particulier. Pour CHAQUE tweet, dis s'il contient ce que l'utilisateur cherche.
Les tweets peuvent être en arabe, anglais ou autre langue : réponds toujours en français.
Utilise UNIQUEMENT le texte du tweet et de l'article lié quand il est fourni. N'invente aucun chiffre : un objectif de cours n'est rempli que s'il est écrit dans le tweet ou l'article.
Réponds par un tableau JSON et rien d'autre, un objet par tweet, dans le même ordre, avec exactement ces clés :
{"n": numéro du tweet,
 "pertinent": true si le tweet contient ce que l'utilisateur cherche, sinon false,
 "type": "prediction" | "objectif" | "recommandation" | "resultats" | "news" | "technique" | "avis" | "autre",
 "sens": "hausse" | "baisse" | "neutre",
 "objectif": objectif de cours (ou de prix pour une crypto) en nombre s'il est écrit, sinon null,
 "horizon": échéance écrite dans le tweet (ex. "12 mois", "fin 2026"), sinon null,
 "qui": qui fait la prévision ou la recommandation (ex. "UBS", "Jefferies", l'auteur), sinon null,
 "resume": ce que le tweet dit sur le titre, 20 mots max, en français}
"prediction" = quelqu'un prévoit un mouvement ; "objectif" = objectif de cours d'un analyste ; "recommandation" = achat/vente/relèvement/abaissement ;
"technique" = analyse graphique (support, résistance, RSI) ; "avis" = opinion sans prévision chiffrée."""


def hash_consigne(consigne: str) -> str:
    return hashlib.sha1(consigne.strip().lower().encode()).hexdigest()[:8]


def _cle(consigne: str, t: dict, isin: str) -> str:
    return f"{hash_consigne(consigne)}:{t['id']}:{isin}"


def _lire_cache() -> dict:
    try:
        return json.loads(FICHIER_LLM.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _tableau_du_llm(texte: str) -> list[dict]:
    """Les modeles entourent parfois le JSON de ```json ... ``` : on prend le premier [ ... ]."""
    m = re.search(r"\[.*\]", texte, re.DOTALL)
    try:
        d = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        d = []
    return [x for x in d if isinstance(x, dict)]


def _propre(x: dict) -> dict:
    obj = x.get("objectif")
    try:
        obj = float(str(obj).replace(",", ".").replace("€", "").strip()) if obj not in (None, "", "null") else None
    except ValueError:
        obj = None
    return {"pertinent": x.get("pertinent") is True,
            "type": x.get("type") if x.get("type") in TYPES_IA else "autre",
            "sens": x.get("sens") if x.get("sens") in ("hausse", "baisse", "neutre") else "neutre",
            "objectif": obj, "horizon": x.get("horizon") or None, "qui": x.get("qui") or None,
            "resume": str(x.get("resume") or "")[:200]}


def lire(data: dict, consigne: str = CONSIGNE_DEFAUT) -> dict:
    """Envoie au LLM les tweets pas encore lus pour cette consigne, puis
    ajoute t["ia"] a chaque tweet de `data` (resultat de signaux())."""
    import analyste  # import tardif : analyste importe deja ce module
    cache = _lire_cache()
    a_lire = [(g, t) for g in _groupes(data) for t in g["tweets"] if _cle(consigne, t, g["isin"]) not in cache]
    erreur = None
    for i in range(0, len(a_lire), PAQUET):
        paquet = a_lire[i:i + PAQUET]
        texte = "\n\n".join(f"Tweet {n} — {_sujet(g, t)} — @{t['compte']} le {t['date'][:10]} :\n"
                            f"{' '.join(t['texte'].split())[:600]}"
                            + "".join(f"\nArticle lié ({a['site']}) : {a['titre']} — {a.get('resume') or ''}"
                                      for a in t.get("apercus", []))
                            for n, (g, t) in enumerate(paquet, 1))
        try:
            rep = analyste._llm([
                {"role": "system", "content": SYSTEME_LLM},
                {"role": "user", "content": f"Ce que je cherche : {consigne}\n\n{texte}"},
            ], max_tokens=180 * len(paquet))
        except Exception as exc:  # LLM absent ou en panne : on garde ce qui est deja lu
            erreur = f"{type(exc).__name__}: {exc}"[:300]
            break
        for x in _tableau_du_llm(rep):
            try:
                g, t = paquet[int(x.get("n")) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            cache[_cle(consigne, t, g["isin"])] = _propre(x)
        FICHIER_LLM.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return {**annoter(data, consigne, cache), "erreur_ia": erreur}


def annoter(data: dict, consigne: str = CONSIGNE_DEFAUT, cache: dict | None = None) -> dict:
    """Ajoute les lectures IA deja en cache (sans appeler le LLM)."""
    cache = _lire_cache() if cache is None else cache
    lus = 0
    for g in _groupes(data):
        for t in g["tweets"]:
            t["ia"] = cache.get(_cle(consigne, t, g["isin"]))
            lus += t["ia"] is not None
        g["pertinents"] = sum(1 for t in g["tweets"] if (t["ia"] or {}).get("pertinent"))
    total = sum(len(g["tweets"]) for g in _groupes(data))
    return {**data, "consigne": consigne, "ia_lus": lus, "ia_total": total}


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    isin = args.pop(0) if args and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", args[0]) else None
    for t in chercher(" ".join(args) or "Biophytis", isin):
        print(t["date"][:10], "@" + t["compte"], f"♥{t['likes']}", "|", t["texte"][:110].replace("\n", " "))
