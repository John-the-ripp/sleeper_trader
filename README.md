# sleeper·trader

Cockpit personnel pour repérer les actions qui **bougent fort et souvent** sur Trade Republic : chutes, hausses, pics intra-journaliers et motifs « chute → rebond ».

Le projet lit les cotations publiques de Trade Republic (Lang & Schwarz, bourse `LSX`) sur environ 11 000 titres, filtre les faux signaux, fait expliquer les mouvements par un LLM à partir des news du jour et affiche le tout dans une interface web locale.

> ⚠️ Outil d'observation personnel. Ce n'est pas un conseil en investissement. Les cotations LSX des petites valeurs sont parfois peu fiables (voir [Pièges connus](#pièges-connus)).

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Installation](#installation)
- [Lancement](#lancement)
- [Architecture](#architecture)
- [Récupération des données](#récupération-des-données)
- [Algorithmes](#algorithmes)
- [Couche IA](#couche-ia)
- [Référence de l'API](#référence-de-lapi)
- [Paramètres](#paramètres)
- [Fichiers générés](#fichiers-générés)
- [Pièges connus](#pièges-connus)
- [Limites](#limites)

## Fonctionnalités

L'interface web a cinq vues :

| Vue | Ce qu'elle montre | Rafraîchissement |
|---|---|---|
| **Picks** | Les titres qui font souvent de gros écarts dans la journée (≥ 20 %) sur le dernier mois, classés par score | toutes les 15 min en séance, 6 h sinon |
| **Chutes / hausses** | Les plus fortes variations sur 1 jour, 5 jours ou 1 mois, avec filtres (spread, prix max, motif de rebond…) et la **cause du mouvement** expliquée par l'IA | idem |
| **Rebonds du jour** | Les titres qui ont chuté d'au moins 10 % hier puis remonté d'au moins 10 % aujourd'hui, avec la cause de la chute et celle du rebond. Les jours passés restent consultables | idem |
| **Live** | Ce qui bouge **maintenant** : variation du bid entre deux scans successifs | toutes les 5 min |
| **Watchlist** | Tes titres suivis, avec leur prix live et leur position dans la fourchette du mois | à la demande |

S'y ajoutent :
- une **fiche titre** avec graphique en bougies, infos générales et **analyse IA détaillée** (news des 30 derniers jours, motif, points de vigilance) ;
- un bouton **« Pourquoi ? »** qui relance l'explication IA d'un mouvement ;
- des **alertes navigateur** (avec bip sonore) : forte chute du jour, chute d'un titre de la watchlist ou nouveau rebond ;
- une **recherche** par nom ou ISIN sur tout l'univers, sans appel réseau.

## Installation

Prérequis : Python 3.12 ou plus récent, sous Windows (les commandes ci-dessous sont pour PowerShell).

```powershell
py -m venv .venv
.venv\Scripts\pip install fastapi "uvicorn[standard]" websockets requests python-dotenv
.venv\Scripts\pip install git+https://github.com/Zarathustra2/TradeRepublicApi.git
```

### Univers de titres

Le projet lit la liste des titres dans des fichiers `isins_cache_<PAYS>.json` (par exemple `isins_cache_FR.json`), placés à la racine. **Ces fichiers ne sont pas versionnés** et doivent être générés à part. Format attendu :

```json
{ "items": [ { "isin": "FR001400OLP5", "name": "Biophytis Actions Nominatives" } ] }
```

Sans ces fichiers, l'univers est vide et les scans ne trouvent rien.

### Analyse IA (facultatif)

Crée un fichier `.env` à la racine. Il est ignoré par git, ne le commite jamais.

```env
VLLM_BASE_URL=http://localhost:8001/v1
VLLM_API_KEY=...
MODEL_NAME=....
```

N'importe quelle API compatible OpenAI (`POST /chat/completions`) fonctionne : vLLM, Ollama ou un fournisseur en ligne. Sans `.env`, tout le reste du cockpit fonctionne normalement : les explications restent vides et les erreurs sont affichées dans l'interface.

## Lancement

```powershell
.venv\Scripts\python -m uvicorn main:app --port 8000
```

Puis ouvre http://127.0.0.1:8000.

Au démarrage (`lifespan` FastAPI), trois tâches se lancent en arrière-plan :
1. `scan.boucle_scan` : le scan live ;
2. `picks.boucle_picks` : le screener. Il tourne tout de suite si `mouvements.json` est absent ou trop ancien ; un tour complet prend environ 40 s ;
3. `explications.tour_explications` : explique les mouvements déjà sur disque. Elle est ensuite relancée après chaque tour du screener.

Outils en ligne de commande :

```powershell
.venv\Scripts\python test.py FR001400OLP5          # résumé d'un titre
.venv\Scripts\python test.py FR001400OLP5 --full   # JSON brut de Trade Republic
.venv\Scripts\python picks.py                      # un tour de screener, top 15 affiché
```

## Architecture

```
 isins_cache_*.json ──► tr_data.charger_isins ──► ~11 000 ISIN
                                                     │
          ┌──────────────────────────────────────────┼────────────────────────┐
          ▼                                          ▼                        ▼
   scan.py (5 min)                        picks.py (15 min / 6 h)       main.py (à la demande)
   variation bid/bid live                 historique 1 mois →           fiche titre, watchlist
                                          pics, variations, motifs      analyste.py (analyse longue)
          │                                          │                        │
          │                          picks.json + mouvements.json             │
          │                                          │                        │
          │                          APRES_TOUR ──► explications.py           │
          │                                          │  news.py + LLM          │
          │                                          ▼                        │
          │                          explications/<jour>.json, alertes.json   │
          │                                                                   │
          └──────────── tr_client.py (websocket Trade Republic) ◄─────────────┘

                          main.py (FastAPI) ──► templates/index.html
```

| Fichier | Rôle |
|---|---|
| `main.py` | API FastAPI et service du front. Démarre les tâches de fond. |
| `tr_client.py` | Client websocket Trade Republic, corrigé par rapport à la librairie d'origine. Fonctions `fetch_one`, `fetch_many_tickers`, `fetch_many_history`. |
| `tr_data.py` | Chargement de l'univers d'ISIN (`charger_isins`) et sondage des prix par lots en parallèle (`sonder_prix`). |
| `scan.py` | Scan live : compare le bid de chaque titre à celui du tour précédent. |
| `picks.py` | Screener sur un mois : reconstruction des séances, amplitude intra-jour, score, variations 1j/5j/1m/veille, filtres anti-faux-positifs. |
| `motifs.py` | Détection du motif « forte chute suivie d'un rebond le lendemain ». Pur Python, sans IA. |
| `news.py` | News des 30 derniers jours via Google News RSS, en français et en anglais, sans clé API. |
| `analyste.py` | Analyse longue d'un titre par le LLM (fiche titre). |
| `explications.py` | Explications courtes en JSON pour des dizaines de titres (tableaux), rapport des rebonds, alertes. |
| `test.py` | Script pour inspecter un titre à partir de son ISIN. |
| `templates/index.html` | Interface web, en un seul fichier. |

**Pourquoi `APRES_TOUR` ?** `explications` importe `picks`. Si `picks` importait `explications` pour la relancer, on aurait un import circulaire. `main.py` enregistre donc `explications.tour_explications` dans la liste `picks.APRES_TOUR`, que `picks.lancer` appelle après chaque tour réussi.

## Récupération des données

Trade Republic n'a pas d'API REST publique : tout passe par un websocket vers `wss://api.traderepublic.com`.

### Protocole

```
→ connect 31 {"locale":"fr","platformId":"webtrading","clientId":"app.traderepublic.com",...}
← connected
→ sub 1 {"type":"ticker","id":"FR001400OLP5.LSX"}
← 1 A {"bid":{"price":"0.0288",...},"ask":{...},"last":{...},"pre":{...},"open":{...}}
← 1 D =12 -3 +0.029 =40        (delta sur la réponse précédente)
→ echo 1790197199925           (keepalive toutes les 30 s)
```

| État | Signification |
|---|---|
| `A` | Réponse complète (JSON) |
| `D` | Delta : suite d'opérations `=n` (garder n caractères), `-n` (en supprimer n), `+texte` (insérer), appliquées à la dernière réponse |
| `C` | Fin de souscription |
| `E` | Erreur (ex. `AUTHENTICATION_ERROR`) |

### Corrections apportées à la librairie `trapi`

`TrClient` hérite de `TRApi` (Zarathustra2/TradeRepublicApi) et corrige quatre problèmes, vérifiés le 04/09/2026 :

1. **Handshake** : la librairie envoie `connect 21`, que le serveur refuse (`failed 34`). Les versions 26, 30, 31 et 32 sont acceptées ; le projet utilise 31 avec le payload client complet.
2. **Parsing** : la librairie découpe chaque message avec `split()` sans limite, ce qui casse tout JSON contenant des espaces. Ici, le message est découpé en 3 champs maximum et seuls les deltas sont tokenisés.
3. **`aggregateHistoryLight`** : si le champ `resolution` est présent, le serveur ne répond plus rien. Il est donc omis par défaut.
4. **Robustesse** : une exception dans un callback ne coupe plus le flux, et un keepalive `echo` évite la déconnexion.

### Flux utilisés

| Flux | Contenu | Authentification |
|---|---|---|
| `ticker` | bid, ask, dernier prix, ouverture, clôture de la veille (`pre`) | non (LSX uniquement) |
| `instrument` | fiche du titre : nom, symbole, pays, capitalisation, bourses | non |
| `aggregateHistoryLight` | historique en bougies (4 h sur 1 mois) | non |
| `ticker` sur `XPAR`, `XFRA`… | prix des autres bourses | **oui**, non utilisé |

### Montée en charge

Les ISIN sont regroupés par **lots de 250 par websocket**, avec **6 lots en parallèle** (sémaphore), soit environ 45 lots pour tout l'univers. En séquentiel, chaque lot attendrait son timeout complet dès qu'un seul titre ne répond pas, soit environ 45 × 40 s. En parallèle, un tour prend environ 40 s.

## Algorithmes

### 1. Reconstruction des séances (`picks.jours_depuis_bougies`)

Trade Republic fournit des bougies de 4 h sur un mois. Pour chaque bougie, le projet garde deux **prix observés** : l'ouverture, à l'heure de début, et la clôture, 4 h plus tard.

- **Seuls les points entre 9 h et 17 h 30 (heure de Paris) sont gardés.** Les prix du soir sont la fourchette défensive de L&S.
- **Les mèches (haut/bas des bougies) sont ignorées** : ce sont souvent des prints isolés.
- **`despike`** retire ensuite les points aberrants. Un point (ou deux points consécutifs) est supprimé s'il s'écarte d'un facteur ≥ 1,5 de ses voisins alors que ces voisins sont d'accord entre eux à 5 % près.
- Pour chaque jour, on obtient `{date, bas, haut, cloture, h_bas, h_haut}`. Le **dernier prix de la veille** est ajouté à chaque jour : un gap entre deux séances compte donc comme un pic.

### 2. Détection des pics (`picks.analyser`)

- Amplitude du jour = `(haut − bas) / bas × 100`
- **Jour à pic** : amplitude ≥ `SEUIL_PIC` (20 %)
- **Rebond** (au sens des picks) : une clôture qui perd ≥ 20 %, suivie dans les 3 jours d'un haut qui repasse ≥ 20 % au-dessus du creux
- **Pic mécanique** : si ≥ 70 % des jours à pic ont le même couple (heure du creux, heure du sommet), c'est la cotation L&S qui change de régime, pas le marché. Le titre est signalé.

Un titre est rejeté si :
- il a moins de 5 séances ;
- son prix est passé sous 0,01 € ;
- il a moins de 2 jours à pic ;
- plus de 50 % de ses jours sont plats ;
- son spread dépasse 50 % ;
- il est dans la liste `EXCLUS` (données cassées confirmées).

### 3. Score

```
score = jours_pic + 3 × rebonds − 0,3 × plats_pct − 0,3 × spread_pct
```

Plus un titre pique souvent, rebondit souvent et cote serré, plus il monte. Les poids viennent d'un screener chute-rebond validé le 07/09.

La **position dans la fourchette du mois**, `(bid − bas_mois) / (haut_mois − bas_mois) × 100`, indique où est le prix : 0 = au plus bas du mois, 100 = au plus haut.

### 4. Variations (`picks.variations`)

**Prix courant** : c'est le bid live seulement si on est en séance (jour ouvré, 9 h–17 h 30) **et** que le spread est ≤ 20 %. Sinon, c'est la dernière clôture de séance.

| Variation | Référence |
|---|---|
| `var_1j` | Clôture de la séance précédente, **seulement si elle date de 4 jours ou moins**. Au-delà, l'historique est interrompu et la variation serait fausse (Gossamer Bio : historique arrêté à 12 €, bid live à 0,12 € → faux −99 %). |
| `var_5j` | Clôture d'il y a 5 séances |
| `var_1m` | Première clôture du mois |
| `var_veille` | Séance précédente comparée à celle d'avant. Sert au rapport des rebonds. |

### 5. Motif chute → rebond (`motifs.chute_rebond`)

- **Forte chute** : clôture en baisse d'au moins 15 % par rapport à la veille.
- **Rebond réussi** : le **haut** de la séance suivante dépasse d'au moins 10 % la clôture de la chute. On prend le haut parce que c'est le prix auquel on aurait pu revendre : un rebond du matin qui retombe l'après-midi compte.
- **Motif validé** : au moins 2 chutes terminées **et** au moins 60 % de rebonds réussis.
- `chute_en_cours` : la dernière séance est une forte chute dont le lendemain n'est pas encore joué.

### 6. Scan live (`scan.py`)

Toutes les 5 minutes, le bid de chaque titre est comparé au bid **du tour précédent**, gardé en mémoire. Le champ `pre` de Trade Republic n'est pas utilisé. Un titre est remonté si `|variation| ≥ 12 %` et que son spread est ≤ 20 %. Au premier tour, il n'y a pas de variation : on enregistre seulement les prix de départ.

## Couche IA

Le LLM **ne cherche rien lui-même** (RAG « à la main ») :
1. `news.py` récupère les titres d'articles via Google News RSS ;
2. le code calcule tous les chiffres : variations, motif, drapeaux de fiabilité ;
3. le tout est envoyé au LLM avec l'interdiction d'inventer un chiffre, une news ou une date.

### Recherche de news (`news.py`)

Les libellés Trade Republic sont nettoyés avant la recherche : `"Biophytis Actions Nominatives"` devient `"Biophytis"`, et `"MAAT PHARMA S.A. EO-,1"` devient `"MAAT PHARMA S.A."`. La requête est faite en français **et** en anglais sur 30 jours, avec dédoublonnage et tri par date.

### Deux niveaux d'analyse

| | `analyste.py` | `explications.py` |
|---|---|---|
| Usage | Fiche d'un titre | Lignes de tableau, pour des dizaines de titres |
| Sortie | Texte en markdown, 220 mots maximum | JSON : `categorie`, `cause_jour`, `cause_veille`, `confiance` |
| Données | 1 mois de séances, motif détaillé, 30 jours de news | 6 dernières clôtures, news des 10 derniers jours |
| Cache | 1 h en mémoire | Toute la journée, sur disque (`explications/<jour>.json`) |

Catégories d'explication :

| Catégorie | Signification |
|---|---|
| `news` | Une news du jour ou de la veille explique le mouvement |
| `secteur` | Tout le secteur ou le marché a bougé |
| `technique` | Aucune news : rebond ou prise de bénéfices |
| `artefact` | Mouvement incohérent avec un spread large : probable erreur de cotation |
| `inconnu` | Impossible à dire |

La réponse du modèle est validée : on extrait le premier bloc `{…}`, et toute catégorie ou confiance hors liste est remplacée par une valeur sûre (`inconnu`, `faible`).

### Explications automatiques et alertes

Après chaque tour du screener, `tour_explications` traite, dans cet ordre et sans doublon :
1. les **rebonds du jour** (chute ≥ 10 % hier, hausse ≥ 10 % aujourd'hui), avec la cause de la veille ;
2. les titres de la **watchlist** en baisse d'au moins 10 % ;
3. les **15 plus fortes chutes** et les **15 plus fortes hausses**, hors titres asiatiques et hors spread > 20 %.

Seuls les titres pas encore expliqués aujourd'hui sont envoyés au LLM. Chaque nouveau titre expliqué peut déclencher une **alerte** (une seule par type, titre et jour, 300 maximum conservées) :

| Type | Condition |
|---|---|
| `rebond` | Nouveau rebond du jour |
| `watchlist` | Titre suivi en baisse d'au moins 10 % |
| `chute` | Baisse du jour d'au moins 15 % |

Le navigateur interroge `/api/alertes?depuis=<id>` et bipe pour chaque nouvel identifiant.

## Référence de l'API

| Méthode | Route | Description |
|---|---|---|
| GET | `/` | Interface web |
| GET | `/api/etat` | Horaires de séance (LSX, Euronext), état des tâches scan / picks / IA, taille de l'univers |
| GET | `/api/picks` | Dernier résultat du screener de pics |
| POST | `/api/picks/relancer?seuil=20` | Relance un tour du screener |
| GET | `/api/mouvements` | Chutes et hausses. Paramètres : `periode` (`1j`, `5j`, `1m`), `sens` (`baisse`, `hausse`), `spread_max` (20), `prix_max`, `asie` (false), `trous` (false), `motif` (false), `limite` (200). Sur `1j`, chaque ligne inclut son explication IA. |
| GET | `/api/rebonds?jour=AAAA-MM-JJ` | Rapport des rebonds du jour (ou d'un jour passé) avec explications, et liste des jours disponibles |
| GET | `/api/live` | Dernier scan live |
| GET | `/api/watchlist` | Titres suivis avec prix live et fourchette du mois |
| POST / DELETE | `/api/watchlist/{isin}` | Ajouter ou retirer un titre |
| GET | `/api/action/{isin}?range=1m` | Fiche titre : infos, prix live, bougies (`1d`, `5d`, `1m`, `3m`, `1y`), données du screener |
| GET | `/api/analyse/{isin}?force=false` | Analyse IA détaillée (cache 1 h) |
| POST | `/api/expliquer/{isin}` | Force l'explication IA du mouvement du jour |
| GET | `/api/alertes?depuis=0` | Alertes plus récentes que l'identifiant donné |
| GET | `/api/recherche?q=bio` | Recherche par nom ou ISIN (12 résultats max) |
| GET | `/health` | Healthcheck |

## Paramètres

Toutes les constantes sont en tête de fichier :

| Constante | Fichier | Valeur | Rôle |
|---|---|---|---|
| `INTERVALLE_S` | `scan.py` | 300 s | Intervalle du scan live |
| `SEUIL_PAR_DEFAUT` | `scan.py` | 12 % | Variation live minimale remontée |
| `SPREAD_MAX` | `scan.py` | 20 % | Spread maximal en live |
| `SEUIL_PIC` | `picks.py` | 20 % | Amplitude d'un jour à pic |
| `PLANCHER` | `picks.py` | 0,01 € | Prix minimal fiable |
| `SPREAD_MAX` | `picks.py` | 50 % | Spread maximal au screener |
| `PLATS_MAX` | `picks.py` | 50 % | Part maximale de jours plats |
| `MECANIQUE_MAX` | `picks.py` | 70 % | Seuil de détection des pics mécaniques |
| `SEANCE` | `picks.py` | 9 h – 17 h 30 | Heures de séance prises en compte |
| `LOT` / `PARALLELE` | `picks.py` | 250 / 6 | ISIN par websocket / lots simultanés |
| `HORS_FUSEAU` | `picks.py` | KY, HK, CN, JP… | Pays masqués par défaut |
| `EXCLUS` | `picks.py` | 2 ISIN | Titres aux données cassées |
| `CHUTE` / `REBOND` | `motifs.py` | 15 % / 10 % | Définition du motif chute → rebond |
| `MIN_CHUTES` | `motifs.py` | 2 | Occurrences minimales pour un motif |
| `CHUTE_HIER` / `REBOND_AUJ` | `explications.py` | −10 % / +10 % | Définition d'un rebond du jour |
| `TOP_AUTO` | `explications.py` | 15 | Chutes et hausses expliquées automatiquement |
| `ALERTE_CHUTE` / `ALERTE_WATCH` | `explications.py` | −15 % / −10 % | Seuils d'alerte |
| `CACHE_S` | `analyste.py` | 3600 s | Cache de l'analyse détaillée |

## Fichiers générés

Tous sont créés à l'exécution et **ignorés par git** :

| Fichier | Contenu |
|---|---|
| `picks.json` | Dernier résultat du screener de pics |
| `mouvements.json` | Variations de tout l'univers (sert aussi de repère pour savoir si un tour est nécessaire) |
| `watchlist.json` | ISIN suivis |
| `alertes.json` | Les 300 dernières alertes |
| `explications/<jour>.json` | Explications IA du jour, par ISIN |
| `explications/rebonds_<jour>.json` | Liste des rebonds du jour, pour consulter les jours passés |

## Pièges connus

Toutes ces règles viennent de cas réels observés :

- **Hors séance, les prix LSX sont faux.** Le teneur de marché écarte sa fourchette : Biophytis était à 0,0288 / 0,0706 à 23 h pour environ 0,050 en réalité. Seuls les prix observés entre 9 h et 17 h 30 servent de référence.
- **La bougie 14 h – 18 h est piégée** : sa clôture tombe après la fermeture de Paris ou de Stockholm, au prix de nuit de L&S (KebNi : 0,067 le jour, 0,0256 à 18 h, soit un faux −62 %).
- **Le champ `pre` de Trade Republic n'est pas fiable** : il est parfois périmé, avec de faux +700 % (H2 Core).
- **Spread trop large = cotation fantôme.** Trade Republic a déjà annulé après coup un achat fait sur ce genre de prix (EcoRub).
- **Prints isolés** : Maison Clio Blue 0,645 → 1,94 → 0,65, BayWa 8,36 → 3,41 → 8,44. Ils sont retirés par `despike`.
- **Pics mécaniques** : BATM fait son creux à 14 h tous les jours ; International Metals est à 0,02 le matin et à 0,027 à 14 h chaque jour.
- **Titres asiatiques** : leur bourse est fermée pendant la séance européenne, donc leur cotation LSX ne suit aucun vrai marché (ESPRIT : 0,031 à 10 h puis 0,048 chaque jour). Ils sont masqués par défaut.
- **Historique interrompu** : si le dernier point date de plus de 4 jours, `var_1j` n'est pas calculée.

## Limites

- **Une seule bourse** : sans authentification, seule `LSX` est accessible. Les prix peuvent différer de ceux de la bourse principale (Euronext, Xetra…) et de ceux affichés dans l'app Trade Republic.
- **Univers figé** : il dépend des fichiers `isins_cache_*.json`, qui ne sont pas générés par le projet.
- **État en mémoire** : le scan live repart de zéro à chaque redémarrage, et son premier tour ne produit aucune variation.
- **Pas d'API officielle** : le protocole websocket peut changer sans préavis, comme lors du passage de la version 21 à 31.
- **News limitées aux titres d'articles** : le LLM ne lit pas le contenu des articles.
