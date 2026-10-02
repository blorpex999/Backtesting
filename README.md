# backtest-lab

Plateforme **locale** de backtest de stratégies, construites par blocs (forex, indices CFD, or,
pétrole), avec une méthode de validation rigoureuse jusqu'à la simulation d'un challenge FTMO.

> Outil de **recherche uniquement** : il ne passe jamais d'ordres réels.
> Principe directeur : **l'outil ne doit pas mentir** (pas de look-ahead, pas de coûts
> sous-estimés, pas de sur-apprentissage caché).

Cahier des charges complet : [`docs/prompt-claude-code-outil-backtest.md`](docs/prompt-claude-code-outil-backtest.md).

## État d'avancement

| Jalon | Contenu | État |
|---|---|---|
| J1 | Squelette, configs, téléchargement Dukascopy, contrôle qualité, métadonnées d'instruments, chargeur avec verrou IS, page « Données » | ✅ terminé |
| J2 | Moteur d'exécution (ordres, SL/TP, coûts, sizing, conversion de devises, profils broker) | à venir |
| J3 → J8 | Schéma de stratégie et interpréteur, backtest et rapports, multi-timeframe et price action, recherche, verrous OOS/HOLDOUT, extras | à venir |

## Installation (Windows, PowerShell)

```powershell
# 1. Outils (une seule fois)
winget install astral-sh.uv
winget install OpenJS.NodeJS.LTS      # requis par dukascopy-node (téléchargement)
# rouvrir le terminal ensuite

# 2. Projet
git clone https://github.com/blorpex999/Backtesting.git
cd Backtesting
uv sync                                # crée .venv et installe les dépendances
uv run btlab data setup                # installe dukascopy-node 1.50.0 dans .tools\
```

## Utilisation

```powershell
uv run btlab data instruments                  # instruments configurés + historique annoncé
uv run btlab data download EURUSD GBPUSD       # téléchargement incrémental + contrôle qualité
uv run btlab data download --all               # tous les instruments (long la première fois)
uv run btlab data update                       # met à jour ce qui est déjà téléchargé
uv run btlab data qc EURUSD                    # contrôle qualité seul (+ reports\qc\EURUSD.html)
uv run btlab data coverage                     # couverture et statut qualité
uv run btlab ui                                # interface web sur http://127.0.0.1:8501
```

Le téléchargement reprend là où il s'est arrêté : relancez simplement la commande après une
coupure. Seuls les jours UTC complets sont téléchargés ; le mois en cours est retéléchargé à la
mise à jour suivante.

**Refus de Dukascopy (HTTP 429).** L'API de Dukascopy refuse parfois des requêtes, pour deux
raisons différentes que l'outil distingue :

- **un jour précis est refusé**, quel que soit le débit (constaté sur des mois de 2009) ;
- **le débit est trop élevé** (limite de requêtes).

Fonctionnement :

1. Chaque mois est d'abord demandé d'un bloc, 4 requêtes à la fois avec 1 s de pause entre
   les lots (`configs/data.yaml`).
2. Si le mois échoue, il est repris **jour par jour**. Les jours déjà obtenus sont relus dans
   le cache, sans nouvelle requête.
3. Chaque refus est vérifié par une **requête témoin** sur un jour déjà servi :
   - si le témoin passe, seul ce jour est refusé : il est noté, et le reste du mois continue.
     Il est redemandé aux lancements suivants. Après 3 lancements, il est déclaré
     **indisponible chez la source** et exclu par le contrôle qualité, avec cette raison ;
   - si le témoin est refusé aussi, c'est la limite de débit : pause de 60 s, puis 120 s,
     240 s… (15 min au plus), débit réduit, nouvel essai. Après 6 pauses, l'outil s'arrête
     proprement et ne traite pas les instruments suivants : relancez plus tard, la reprise
     est automatique.

La fin de la commande résume les jours refusés (à réessayer) et les jours déclarés
indisponibles. L'historique complet représente environ 13 000 requêtes par instrument (une par
jour et par côté) : comptez de l'ordre d'une à deux heures par instrument sans limitation, plus
si Dukascopy impose des pauses. Les mises à jour suivantes sont rapides.

Dans le code, les prix se lisent **uniquement** via le chargeur :

```python
from btlab.data.loader import load_m1
df = load_m1("EURUSD", "2015-01-01", "2016-01-01")   # index ts_utc, colonnes bid_* / ask_*
```

## Données

**Source : Dukascopy**, bougies M1 BID et ASK séparées, en UTC, via
[`dukascopy-node`](https://github.com/Leo4815162342/dukascopy-node) en version **épinglée (1.50.0)**.
Pourquoi : Dukascopy a changé de flux de données en 2026 (dukascopy-node lit une API JSON depuis la
1.49 au lieu des anciens fichiers `.bi5`) ; un client maintenu suit ces changements, et l'épinglage
empêche un changement de format silencieux. Deux précautions intégrées :

- `--volumes` est toujours activé : sans lui, dukascopy-node ne filtre pas les bougies « plates »
  de remplissage et les trous deviendraient invisibles ;
- au moins une reprise (`retries ≥ 1`) est imposée : avec 0 reprise, dukascopy-node masque les
  erreurs réseau (fichier vide, code de sortie 0), soit une perte de données silencieuse.

| Nom interne | Symbole Dukascopy | 1re bougie M1 annoncée |
|---|---|---|
| EURUSD, GBPUSD, USDJPY, USDCHF | EUR/USD, GBP/USD, USD/JPY, USD/CHF | 2003-05-04 |
| AUDUSD, USDCAD, EURGBP | AUD/USD, USD/CAD, EUR/GBP | 2003-08-03 |
| XAUUSD | XAU/USD | 2003-05-05 |
| US100 | USATECH.IDX/USD | 2011-09-18 |
| US500 | USA500.IDX/USD | 2011-09-18 |
| US30 | USA30.IDX/USD | 2013-09-30 |
| GER40 | DEU.IDX/EUR | 2013-09-30 |
| USOIL (WTI) | LIGHT.CMD/USD | 2011-09-23 |
| UKOIL (Brent) | BRENT.CMD/USD | 2010-12-02 |

Dates issues des métadonnées de dukascopy-node ; le rapport qualité donne la date réellement
exploitable après téléchargement. Le téléchargement démarre au 2009-01-01 (chauffe des
indicateurs) ou au début de l'historique.

⚠️ **Les prix Dukascopy peuvent différer légèrement de ceux de FTMO**, surtout sur les indices
CFD (sous-jacent, horaires, spread).

Stockage :

```
data/raw/{SYMBOL}/{bid|ask}/{AAAA}-{MM}.csv   fichiers bruts de dukascopy-node, un par mois et par côté
data/raw/{SYMBOL}/manifest.json               ce qui a été téléchargé, quand, avec quelle version
data/parquet/{SYMBOL}/{AAAA}.parquet          ts_utc, bid_o/h/l/c, ask_o/h/l/c, bid_v, ask_v
data/quality/{SYMBOL}/                        résultats du contrôle qualité
reports/qc/{SYMBOL}.html                      rapport qualité autonome
```

### Contrôle qualité

**Rien n'est réparé silencieusement.** Chaque jour (calendaire, en heure de Paris par défaut) reçoit
un statut et, s'il est exclu, sa raison. Les jours exclus sont stockés comme intervalles UTC, donc
valables pour une stratégie dans n'importe quel fuseau.

| Contrôle | Seuil par défaut (`configs/data.yaml`) | Effet |
|---|---|---|
| Trou de cotation pendant les heures attendues | > 15 min signalé, ≥ 60 min | jour invalide |
| Couverture (minutes observées / attendues) | < 90 % | jour invalide |
| Jour sans aucune donnée | — | jour invalide |
| Bougie incohérente (OHLC, prix ≤ 0) | > 0 | jour invalide |
| ask < bid | > 0 minute | jour invalide |
| Spread aberrant (> 10 × médiane du jour) | > 2 % des minutes | jour invalide (sinon signalé) |
| Minute avec un seul côté (BID sans ASK…) | > 5 minutes | jour invalide (sinon minute retirée et comptée) |
| Doublon contradictoire | > 0 | retiré à la construction, jour invalide |
| Doublon identique | — | retiré à la construction et compté |
| 24, 25 et 31 décembre, 1er janvier | configurable | jour exclu (faible liquidité) |
| Jour férié du sous-jacent (NYSE, Xetra) | — | signalé, couverture non exigée |
| Jour coupé par le début ou la fin des données | — | jour exclu (incomplet) |
| Cotation hors des horaires configurés | — | signalé (sert à vérifier les horaires) |

La carte de couverture « heure de la semaine » (observée / attendue) permet de vérifier les
horaires de séance configurés, qui sont **provisoires** (voir plus bas).

### Horaires, changements d'heure

Les séances sont définies dans l'heure locale du marché de référence (New York pour le forex,
l'or, les indices US et le WTI ; Berlin pour le GER40 ; Londres pour le Brent) et converties en
UTC minute par minute avec `zoneinfo`. Les semaines où l'Europe et les États-Unis n'ont pas
encore tous deux changé d'heure sont donc justes par construction (tests dédiés).

## Périodes et verrou

| Période | Dates (UTC, fin exclue) | Accès |
|---|---|---|
| Chauffe | 2009-01-01 → 2010-01-01 | lecture pour initialiser les indicateurs, jamais de trade |
| IS | 2010-01-01 → 2020-01-01 | autorisé |
| OOS | 2020-01-01 → 2024-01-01 | **verrouillé** |
| HOLDOUT | 2024-01-01 → fin des données | **verrouillé** |

Le verrou est appliqué par **le chargeur lui-même** (`btlab.data.loader.load_m1`) : toute plage qui
touche l'OOS ou le HOLDOUT est refusée (`PeriodLockedError`), et chaque tentative est journalisée
dans `registry/runs.db`. Un accès n'est possible qu'avec un jeton enregistré comme accordé dans ce
registre ; la procédure d'octroi (stratégie figée, confirmation explicite, contamination) arrive
au jalon 7. Les fichiers des années protégées ne sont même pas ouverts. Le chargeur refuse aussi
des données dont le contrôle qualité n'a pas été lancé ou est périmé.

Limite assumée : c'est un garde-fou contre l'erreur, pas une barrière contre soi-même (les
fichiers Parquet restent lisibles à la main).

`registry/runs.db` est **versionné avec git** : commitez-le, pour que le nombre d'essais (qui
alimentera le Deflated Sharpe Ratio) ne se perde jamais.

## Hypothèses à remplacer

- **Horaires de séance** de tous les instruments (`sessions.verified: false`) : valeurs
  provisoires, à confirmer avec les caractéristiques des symboles dans MT5 (FTMO) et la carte de
  couverture du contrôle qualité.
- **Jours fériés** de l'or et du WTI : calendrier NYSE utilisé comme approximation du CME.
- **Seuils du contrôle qualité** : valeurs par défaut raisonnables, à ajuster après le premier
  contrôle sur les vraies données.
- Taille de contrat, valeur du point, pas de lot, commission : profils broker au jalon 2.

## Structure

```
configs/            instruments/*.yaml, periods.yaml, data.yaml
docs/               cahier des charges
registry/runs.db    registre des essais et journal des accès (versionné)
strategies/         bibliothèque de stratégies (jalon 3)
frozen/             stratégies figées (jalon 7)
data/  reports/     données et rapports (non versionnés)
src/btlab/
  paths.py config.py context.py periods.py registry.py cli.py
  data/             instruments, sessions, settings, download, store, quality, loader,
                    synthetic, service
  report/           rapport qualité HTML, graphiques
  ui/               application Streamlit (app.py, views/)
tests/
```

## Développement

```powershell
uv run pytest            # tests (aucun accès réseau : sources Dukascopy simulées)
uv run ruff check src tests
uv run ruff format src tests
```

Les tests travaillent dans une copie temporaire du projet (`BTLAB_ROOT`) : ils ne touchent ni
`data/`, ni `reports/`, ni `registry/runs.db`.
