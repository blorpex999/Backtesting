# Plateforme de backtest de stratégies — constructeur par blocs

## Rôle et objectif

Tu es mon binôme de développement en recherche quantitative. Construis une **plateforme locale de backtest générique** :

1. je décris une stratégie en remplissant des formulaires dans une **interface web locale**, en assemblant des blocs (sessions, niveaux, indicateurs, price action, multi-timeframe, entrées, sorties, risque) ;
2. l'outil transforme ces paramètres en règles exécutables, me montre ce qu'il a compris, puis la teste ;
3. l'outil m'accompagne dans une méthode de validation rigoureuse jusqu'à la simulation d'un challenge FTMO.

L'objectif principal : **l'outil ne doit pas me mentir.** Il doit rendre difficiles le sur-apprentissage, le look-ahead et les coûts sous-estimés. C'est un outil de recherche uniquement : il ne passe jamais d'ordres réels.

## Contexte

- Je suis à l'aise en Python (profil dev / cybersécurité). Je suis sous **Windows** : commandes compatibles PowerShell, chemins via `pathlib`.
- Langue : interface, explications, comptes rendus et rapports **en français** ; code, identifiants et commentaires en anglais.
- Marchés : **forex, indices CFD (US100, US500, US30, GER40…), or et matières premières**.
- Cible : challenge **FTMO 2-Step, compte Standard 100 000 $**. Risque habituel : 0,5 % par trade.
- Fuseau horaire de référence pour définir les stratégies : **Europe/Paris** (configurable par stratégie).
- Ma première stratégie à tester est un « range asiatique » (cassure ou fausse sortie à l'ouverture de Londres). Elle sert de modèle de validation (section 5.4), mais **l'outil ne doit contenir aucune logique propre à une stratégie** en dehors des blocs génériques.

## Règles de travail (à respecter en permanence)

1. Avant d'écrire du code, pose-moi tes questions si quelque chose est ambigu, puis propose l'architecture et le plan du jalon 1, et **attends ma validation**.
2. Avance par **jalons** (section 13). À la fin de chaque jalon : tests verts, compte rendu court (fait / hypothèses / limites / suite), puis **STOP** jusqu'à mon feu vert.
3. Ne change jamais une stratégie, des paramètres, des périodes ou des coûts de ta propre initiative pour « améliorer les résultats ». Si tu as une idée, propose-la.
4. Ne lance **jamais** un calcul sur les périodes OOS ou HOLDOUT sans instruction explicite de ma part (section 9.1).
5. Si un résultat semble trop beau (profit factor > 2, espérance > 0,5 R, taux de réussite > 70 %), **cherche d'abord un bug** (look-ahead, alignement multi-timeframe, coûts, ambiguïté intrabar, fuseau horaire) avant de me le présenter.
6. Aucun résultat n'est présenté sans les coûts (spread, commission, slippage) inclus.

## 1. Principe d'architecture

- **Stratégie = définition déclarative, pas du code généré.** Le formulaire produit une définition de stratégie (JSON/YAML) validée par un schéma `pydantic`. Un **interpréteur** unique, testé une fois pour toutes, l'exécute. Pas de génération de code Python par stratégie ni d'appel à une IA : chaque bloc est testé une fois et réutilisé partout.
- La définition est sauvegardée dans une **bibliothèque de stratégies** (fichiers versionnés avec git). Je peux la charger, la dupliquer, la modifier et la comparer.
- **Porte de sortie** : un bloc « Python personnalisé » (section 5.3) pour les règles impossibles à exprimer avec les blocs, soumis aux mêmes protections anti-look-ahead.
- Couches séparées : données → indicateurs et niveaux → conditions et signaux → moteur d'exécution → recherche (optimisation, robustesse, Monte Carlo, prop firm) → interface et rapports.

## 2. Stack technique

- Python 3.11+ ; environnement géré avec `uv`.
- `pandas` ou `polars`, `numpy`, `numba` pour la boucle d'exécution, `pyarrow` (Parquet), `pydantic` + YAML, `plotly` (graphiques, bougies), `pytest`, `ruff`.
- Interface : **Streamlit**, en local uniquement (écoute sur `127.0.0.1`).
- CLI en plus de l'interface (`typer`), pour lancer les mêmes opérations en script.
- Performance visée : un backtest de 10 ans en M1 sur un instrument en **moins d'une minute** ; grilles exécutées en parallèle (multiprocessing). Signaux calculés de façon vectorisée ; gestion des trades dans une boucle compilée `numba`.
- Reproductibilité : seeds fixées, hash de la définition de stratégie, commit git et versions des librairies enregistrés avec chaque run.
- `git init` dès le départ ; `data/` et `reports/` dans `.gitignore`.

## 3. Données

### 3.1 Prix
- Source : **Dukascopy**, bougies **M1 BID et ASK séparées**, en UTC. Outil suggéré : la CLI `dukascopy-node` (via `npx`) ou une librairie Python équivalente — choisis la plus fiable et explique pourquoi.
- Univers de départ (extensible depuis l'interface) :
  - forex : EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, EURGBP ;
  - indices : équivalents Dukascopy de US100, US500, US30, GER40 ;
  - matières premières : XAUUSD, pétrole WTI / Brent.

  Trouve les noms exacts des symboles Dukascopy et **indique la profondeur d'historique réelle de chaque instrument** (elle varie).
- Période : du 2010-01-01 (ou le début disponible) à la dernière date disponible.
- Stockage : Parquet partitionné par symbole et par année. Colonnes : `ts_utc`, `bid_o/h/l/c`, `ask_o/h/l/c`, volume si disponible.
- Page « Données » dans l'interface : téléchargement, mise à jour incrémentale, couverture par instrument, statut qualité.

### 3.2 Métadonnées d'instruments
Fichier de configuration par instrument :

- classe d'actif, taille du pip ou du point, devise de cotation ;
- horaires de cotation et pauses quotidiennes (les indices CFD ont des pauses) ;
- jours fériés principaux.

La **taille de contrat, la valeur du point, le pas de lot et la commission** dépendent du broker : ils vont dans des **profils broker** (section 6.3).

### 3.3 Contrôle qualité (commande + rapport)
- Trous de plus de N minutes pendant les heures de cotation, doublons, bougies incohérentes, ask < bid, spreads aberrants, jours manquants.
- **Ne rien réparer silencieusement** : signaler, et exclure les jours invalides avec la raison (liste consultable).
- Jours de faible liquidité excluables (24, 25 et 31 décembre, 1er janvier), configurable.

### 3.4 Fuseaux horaires et changements d'heure
- Conversion UTC → fuseau de la stratégie avec `zoneinfo`. L'Europe et les États-Unis ne changent pas d'heure le même week-end : les heures d'ouverture US et des news US en heure de Paris varient de quelques semaines par an. Les événements sont stockés en UTC.

### 3.5 Calendrier économique
- Module qui lit un CSV `data/calendar/events.csv` (colonnes : `ts_utc, currency, impact, event`). L'outil fonctionne sans ce fichier (bloc news désactivé, avertissement).
- Propose-moi des sources possibles pour l'historique depuis 2010, en respectant leurs conditions d'utilisation. Ne scrape rien sans mon accord.

## 4. Règle d'or anti-look-ahead (multi-timeframe)

- Chaque série (bougie agrégée, indicateur, niveau, condition) porte un **instant de disponibilité**. Une bougie H1 de 09:00–10:00 n'est utilisable qu'à partir de 10:00. Une valeur calculée sur la clôture d'une bougie n'est connue qu'à la clôture.
- Le moteur ne peut lire que des valeurs dont l'instant de disponibilité est ≤ à l'instant courant. C'est imposé par l'architecture, pas laissé à chaque bloc.
- Un niveau de range horaire (ex. 00:00–08:00) n'existe qu'à partir de la fin du range.
- Un swing high à N bougies n'est confirmé qu'après N bougies supplémentaires.
- **Test de troncature obligatoire pour chaque bloc** (section 11).

## 5. Constructeur de stratégies

### 5.1 Blocs disponibles
Chaque paramètre numérique peut être déclaré **fixe** ou **optimisable** (liste courte de valeurs).

**Univers et temps**
- Instruments ; timeframe(s) des signaux (M1, M5, M15, M30, H1, H4, D1) ; l'exécution est toujours simulée en M1.
- Fenêtres de trading (heure début / fin), jours de la semaine autorisés, dates exclues.
- Sortie forcée à heure fixe ; sortie avant la pause de cotation ; sortie avant le week-end (option).

**Niveaux**
- Range horaire (début / fin) → haut, bas, milieu, taille.
- Range d'ouverture (N minutes après une heure donnée).
- Plus haut / bas / clôture de la veille, de la semaine précédente ; ouverture du jour.
- Plus haut / bas des N dernières bougies (Donchian).
- Niveaux ronds (option).

**Indicateurs** (sur n'importe quel timeframe)
- SMA, EMA, RSI, MACD, Bollinger, ATR, Stochastique, ADX, VWAP ancré sur la session.
- Architecture extensible pour en ajouter facilement.

**Price action / structure**
- Swing highs / lows (fractales de N bougies), cassure de structure (BOS), changement de structure (CHoCH).
- **Sweep** d'un niveau : dépassement d'au moins X puis clôture de retour de l'autre côté.
- Fair Value Gaps (FVG), inside bar, engulfing, pin bar.
- Distance du prix à un niveau, en pips, en points ou en multiple d'ATR.

**Conditions et logique**
- Comparaisons : A > B, A < B, A croise au-dessus / en dessous de B, A à moins de X de B, A entre B et C.
- ET / OU / NON.
- **Séquences** : « événement A puis événement B dans les N minutes / N bougies » (indispensable pour les sweeps et confirmations).
- **Biais multi-timeframe** : condition évaluée sur un timeframe supérieur qui filtre les entrées (ex. clôture D1 au-dessus de l'EMA 200 → longs seulement).
- Filtres de régime : taille du range ou volatilité comparée à l'ATR, avec seuils ou terciles calculés **sur l'IS uniquement**.
- Filtre news : sauter les jours ou les fenêtres autour d'annonces à fort impact (devises, types d'événements, minutes avant / après).

**Entrée**
- Sens : long, short, les deux.
- Type d'ordre : au marché (ouverture de la bougie suivante), stop à un niveau ± décalage, limite à un niveau ± décalage.
- Ordres OCO ; expiration des ordres (heure ou N bougies).
- Maximum de trades par jour, par instrument et au total ; une seule position à la fois par instrument (option).

**Stop loss**
- Fixe (pips / points), multiple d'ATR, à un niveau ± décalage, extrême depuis un événement (ex. plus haut du sweep) ± décalage.
- Distance maximale ou minimale : si le stop sort de ces bornes, pas de trade.

**Take profit**
- Multiple de R, fixe, multiple d'ATR, à un niveau, aucun.

**Gestion de position**
- Break-even à X R, stop suiveur (fixe, ATR, dernier swing), clôture partielle à X R, sortie sur signal opposé, sortie à heure fixe.

**Risque**
- % du capital par trade.
- Risque max ouvert simultanément, risque max par jour.
- **Groupes de corrélation** (ex. EURUSD + GBPUSD) avec risque cumulé plafonné.
- Politique de dépassement : sauter le trade ou réduire la taille.

### 5.2 Ce que l'interface doit faire
- Formulaires par bloc, avec valeurs par défaut sensées et validation immédiate (références manquantes, timeframe incohérent, paramètres impossibles).
- **Résumé en français** généré automatiquement : « Voici ce que l'outil a compris : chaque jour, entre 08:00 et 10:30… ». C'est mon contrôle principal.
- Affichage du **nombre de combinaisons** des paramètres optimisables, avec un avertissement au-delà d'un seuil (ex. 50).
- **Vérification visuelle** : graphique en bougies d'un jour choisi ou tiré au hasard, avec niveaux, indicateurs, signaux, entrées, sorties, SL et TP dessinés. Avec un bouton « jour suivant au hasard » pour vérifier vite sur 20 jours que l'outil fait ce que je veux.
- Sauvegarder, charger, dupliquer et comparer deux stratégies (diff lisible).
- Export / import de la définition en YAML.

### 5.3 Bloc Python personnalisé (porte de sortie)
- Interface de plugin : une fonction qui reçoit **uniquement** les données disponibles à l'instant courant (vue tronquée fournie par le moteur) et retourne un signal ou un niveau.
- Même test de troncature automatique que les autres blocs.
- Clairement signalé dans les rapports comme « code personnalisé ».

### 5.4 Modèles de validation
Ces stratégies doivent être **exprimables uniquement avec les blocs**. Elles servent de tests d'acceptation du constructeur et sont livrées comme modèles :

1. **Range asiatique — cassure** (EURUSD, GBPUSD) :
   - range 00:00–08:00 sur le prix mid ;
   - ordres stop OCO à haut + 0,5 pip et bas − 0,5 pip, placés à 08:00, expirés à 10:30 ;
   - SL au milieu ou à l'opposé du range ; TP à 1R, 2R ou aucun ;
   - sortie forcée à 11:00 ou 17:00 ; 1 trade par paire et par jour.
2. **Range asiatique — fausse sortie** :
   - sweep du haut ou du bas du range (≥ 1 pip, bougies M5) entre 08:00 et 10:30 ;
   - puis clôture M5 de retour dans le range dans les 30 ou 60 minutes ;
   - entrée au marché dans le sens du retour ;
   - SL au-delà de l'extrême du sweep + 0,5 pip, trade sauté si le SL est plus grand que la taille du range ;
   - TP au milieu ou à l'opposé du range.
3. **Opening Range Breakout US100** : range des 15 premières minutes après l'ouverture cash US (heure de New York), cassure, SL de l'autre côté, sortie avant la clôture.
4. **Croisement EMA 20/50 en H1** avec biais D1 (clôture au-dessus / en dessous de l'EMA 200), SL à 1,5 × ATR, TP 2R.
5. **Retour à la moyenne RSI + Bollinger** en M15 sur XAUUSD, dans une fenêtre horaire.
6. **BOS + FVG en M5** avec biais H1, sur GER40.

Pour chacune, vérifie à la main sur quelques jours que les trades générés correspondent aux règles (captures du graphique de vérification dans le compte rendu).

## 6. Moteur d'exécution, coûts, sizing

### 6.1 Exécution
- Achat à l'ASK, vente au BID. Un ordre d'achat stop au niveau X se déclenche quand l'ASK atteint X ; un ordre de vente stop quand le BID atteint X. Un SL d'achat est touché quand le BID atteint le niveau ; un SL de vente quand l'ASK l'atteint.
- **Ambiguïté intrabar (règles conservatrices)** :
  - SL et TP touchés dans la même bougie M1 → SL.
  - Entrée et SL dans la même bougie → perte.
  - Stop suiveur et break-even mis à jour uniquement à la clôture d'une bougie.
  - Gap au-delà du stop → exécution au prix d'ouverture de la bougie, pas au niveau du stop.
- Pas d'ordre pendant les pauses de cotation ; gestion des ouvertures de marché (gaps du lundi, reprise après la pause des indices).

### 6.2 Coûts
- Spread réel via BID/ASK.
- Commission par lot et par instrument selon le profil broker.
- Slippage configurable (défaut : défavorable sur les entrées stop et les SL, nul sur les TP limites).
- Swap optionnel pour les positions gardées la nuit.

### 6.3 Profils broker / prop firm
- Fichier par profil (défaut : **FTMO**) : taille de contrat, valeur du point, pas de lot, commission par instrument, levier max.
- Les valeurs FTMO sont à remplir depuis les caractéristiques des instruments dans MT5. En attendant, mets des valeurs provisoires clairement marquées **« hypothèse à remplacer »** dans chaque rapport.
- Préviens-moi que les prix Dukascopy peuvent différer légèrement de ceux du broker, surtout sur les indices CFD.

### 6.4 Sizing et devises
- `taille = equity × risk_pct / (distance_SL × valeur_du_point_par_lot)`, arrondie à l'inférieur au pas de lot. Compte en USD.
- **Conversion de devises** : P/L des instruments cotés hors USD (GER40 en EUR, USDJPY en JPY…) converti en USD au cours de la paire de conversion au moment de la sortie (données téléchargées automatiquement si besoin).
- Deux modes de résultats :
  - (a) **en R**, indépendants du capital (recherche) ;
  - (b) **compte simulé en $** avec sizing et plafonds de risque (prop firm).

## 7. Métriques (pour chaque run)

- Nombre de trades, trades par an, taux de réussite, gain moyen et perte moyenne (R).
- **Espérance (R/trade) avec intervalle de confiance à 95 %** (bootstrap).
- Profit factor, max drawdown (R et %), plus longue série de pertes, Sharpe journalier annualisé.
- % d'années positives ; résultats par année, par instrument, par jour de semaine, par heure d'entrée.
- Coûts totaux payés et part du résultat brut mangée par les coûts.
- **Deflated Sharpe Ratio** (Bailey & López de Prado), avec le nombre d'essais issu du registre (section 8).
- Courbe d'équité en R et en $, drawdown sous l'eau, distribution des résultats en R, durée des trades.

## 8. Registre des essais

- Chaque backtest (quelle que soit la période) est enregistré automatiquement dans SQLite (`runs.db`) : date, commit git, hash de la définition, hash de la structure (sans les valeurs des paramètres optimisables), paramètres, instruments, période, métriques.
- Page « Registre » : liste, filtres, comparaison de runs.
- Chaque rapport affiche le nombre d'essais réalisés sur cette **famille de stratégie** (même structure) et au total. Ce nombre alimente le Deflated Sharpe Ratio.

## 9. Méthodologie encodée dans l'outil

Périodes par défaut (configurables par instrument selon la profondeur d'historique) :

- **IS** (construction) : 2010-01-01 → 2019-12-31
- **OOS** (validation) : 2020-01-01 → 2023-12-31
- **HOLDOUT** (test final) : 2024-01-01 → fin des données

### 9.1 Verrou des périodes (dès le jalon 1)
- Le **chargeur de données lui-même** n'autorise que l'IS par défaut (pas seulement l'interface).
- **Figer** une stratégie : enregistre la définition complète, paramètres fixés, avec son hash.
- **Validation OOS** : exige une stratégie figée et une confirmation explicite dans l'interface (case à cocher + saisie du nom de la stratégie). Chaque accès est journalisé.
- Un second passage OOS de la même famille avec une définition différente est **bloqué** avec une explication. Seule une option « forcer et contaminer » le permet, et l'OOS de cette famille est alors marqué « contaminé » définitivement dans le registre et dans tous les rapports.
- Compteur global affiché : « X stratégies validées sur l'OOS » (tester beaucoup de familles sur l'OOS et garder la meilleure, c'est aussi du sur-apprentissage).
- HOLDOUT : même logique en plus strict (une seule exécution par stratégie figée, définitivement).

### 9.2 Pages / commandes de recherche
1. **Étude d'événement** (descriptive, sans trades) : pour une condition donnée, ce qui se passe ensuite. Mouvement max favorable / défavorable sur N minutes, % de retour sous un niveau, etc., par instrument et par année.
2. **Backtest** d'une stratégie sur l'IS.
3. **Optimisation** : grille des paramètres déclarés optimisables. Sorties : tableau + heatmaps 2D. **Score de robustesse** = médiane de l'espérance de la config et de ses voisines directes. Recommander le centre d'un plateau, **pas le maximum**.
4. **Walk-forward** dans l'IS : fenêtres entraînement / test configurables (défaut : 3 ans / 1 an, glissante). Sorties : paramètres choisis par fenêtre, stabilité, résultats de test concaténés.
5. **Figer** puis **valider sur l'OOS** (uniquement sur mon instruction).
6. **Robustesse** (sur la stratégie figée) :
   - **Entrées aléatoires** : 1 000 simulations avec le même nombre de trades, dans les mêmes fenêtres horaires, sens et moment tirés au hasard, mêmes règles de sortie et mêmes coûts. Afficher le percentile de la stratégie.
   - **Autres instruments** de la même classe, sans réoptimisation.
   - **Coûts dégradés** : spread × 1,5, slippage × 2, commission × 1,5.
   - **Perturbation des paramètres** : ±20 % sur les paramètres continus, et configs voisines.
   - **Retrait aléatoire de 10 % des trades** (1 000 fois).
7. **Monte Carlo** : bootstrap par **blocs de journées entières** (les trades d'un même jour sur plusieurs instruments restent ensemble), 10 000 tirages. Distribution du max drawdown et de la plus longue série de pertes (médiane, 95e percentile), pour plusieurs niveaux de risque par trade.
8. **Simulation prop firm** — profil **FTMO 2-Step Standard** par défaut. Règles dans un fichier de configuration (elles changent ; valeurs de septembre 2026) :
   - phase 1 : +10 % ; phase 2 : +5 % ;
   - perte max journalière : l'equity (P/L flottant et commissions inclus) ne doit jamais passer sous le solde de 00:00 (heure de Prague = Paris) − 5 % du capital initial ;
   - perte max totale : equity ≥ 90 % du capital initial (statique) ;
   - au moins 4 jours de trading par phase ; pas de limite de temps.

   Deux méthodes : (a) **historique** (un challenge démarré chaque mois, séquence réelle rejouée) ; (b) **Monte Carlo** par blocs de journées. Sorties par niveau de risque : P(phase 1), P(phases 1 + 2), nombre médian de jours, causes d'échec.

   **Contrôle compte financé Standard** : signaler les trades dont l'entrée, la sortie, le SL ou le TP tomberait entre −2 min et +2 min d'une annonce à fort impact sur un instrument concerné. Signaler aussi les positions gardées le week-end.

   Architecture prévue pour ajouter d'autres profils de prop firms plus tard.
9. **Test final HOLDOUT** — une seule fois, sur mon instruction.

## 10. Rapports et critères d'acceptation

- Rapport HTML autonome (un seul fichier) par run, en français : définition et résumé de la stratégie, métriques, graphiques, période, hash, nombre d'essais, statut de contamination, hypothèses de coûts et de profil broker.
- Critères affichés en ✅ / ❌ (seuils par défaut, configurables) :
  - ≥ 200 trades sur l'IS ;
  - espérance ≥ +0,10 R après coûts, et borne basse de l'IC 95 % > 0 ;
  - profit factor ≥ 1,2 ;
  - ≥ 60 % d'années positives, et les 2 meilleures années font moins de 50 % du profit total ;
  - plateau : score de robustesse > 0 et configs voisines positives ;
  - OOS : espérance > 0 et ≥ 50 % de celle de l'IS ;
  - meilleure que 95 % des simulations à entrées aléatoires ;
  - reste positive en coûts dégradés ;
  - Deflated Sharpe Ratio > 0,95 ;
  - prop firm : probabilités affichées pour information, sans seuil.

## 11. Tests pytest obligatoires

- **Troncature (look-ahead), pour chaque bloc et chaque modèle** : exécuter sur les données coupées à l'instant T et sur les données complètes. Toutes les décisions prises avant T doivent être identiques.
- **Multi-timeframe** : une valeur H1 / H4 / D1 n'est jamais utilisée avant la clôture de sa bougie.
- **Scénarios synthétiques** au résultat connu : ordre stop déclenché, OCO, SL et TP dans la même bougie → SL, gap au-delà du SL, expiration d'ordre, séquence dans les N minutes (validée / expirée), SL hors bornes → pas de trade, sortie forcée, break-even, stop suiveur, clôture partielle, pause de cotation.
- **Changements d'heure** : fenêtres et ranges corrects les jours de changement d'heure européens et américains, et pendant les semaines de décalage.
- **Sizing, valeur du point et conversion de devises** pour chaque classe d'actif (forex, JPY, indices, or).
- **Règles prop firm** : limite journalière basée sur le solde de 00:00 et l'equity flottante, limite totale statique, minimum de jours.
- **Verrous** OOS / HOLDOUT.
- **Schéma** : définitions invalides refusées avec un message clair.
- **Modèles de la section 5.4** : chacun s'exécute et produit des trades conformes sur des jours vérifiés à la main.

## 12. Structure de projet suggérée

```
backtest-lab/
  pyproject.toml
  README.md
  configs/            # instruments/, brokers/, propfirms/, periods.yaml, acceptance.yaml
  strategies/         # bibliothèque de définitions (YAML) + templates/
  frozen/             # stratégies figées + hash
  data/               # raw/, parquet/, calendar/   (gitignored)
  reports/            # rapports HTML                (gitignored)
  src/btlab/
    data/             # download, quality, loader (verrou des périodes), calendar, instruments
    schema/           # modèles pydantic de la définition de stratégie
    blocks/           # levels, indicators, price_action, conditions, filters, custom
    engine/           # timeframes (disponibilité), signals, execution (numba), costs, sizing, fx
    research/         # event_study, metrics, optimize, walkforward, robustness,
                      # montecarlo, propfirm, dsr
    registry.py
    report/
    ui/               # app Streamlit (pages)
    cli.py
  tests/
```

## 13. Jalons

- **J1** — Squelette, configs, téléchargement et contrôle qualité (forex, indices, or, pétrole), métadonnées d'instruments, **chargeur avec verrou IS**, page « Données » de l'interface. STOP.
- **J2** — Moteur d'exécution (ordres, SL/TP, gestion, coûts, sizing, conversion de devises, profils broker) + tests synthétiques. STOP.
- **J3** — Schéma de stratégie v1 et interpréteur : blocs temps / niveaux / indicateurs / conditions / entrées / sorties / risque ; page constructeur ; résumé en français ; vérification visuelle ; modèles 1, 3, 4, 5 ; tests de troncature. STOP.
- **J4** — Page backtest, métriques, rapport HTML, registre des essais, comparaison de runs. STOP.
- **J5** — Multi-timeframe complet, price action (swings, BOS / CHoCH, sweeps, FVG, patterns), séquences ; modèles 2 et 6. STOP.
- **J6** — Étude d'événement, optimisation (plateau), walk-forward, robustesse, Monte Carlo, simulation prop firm. STOP.
- **J7** — Figer, validation OOS, HOLDOUT et leurs verrous dans l'interface : codés et testés sur données synthétiques, **pas exécutés sur les vraies données sans mon feu vert**. STOP.
- **J8** (optionnel) — Filtre news et calendrier, bloc Python personnalisé, export d'une stratégie figée en pseudo-code et en squelette MQL5 pour un futur EA.

À chaque jalon : code, tests verts, README mis à jour, et compte rendu court en français.

---

**Pour commencer :** pose-moi tes questions éventuelles, puis propose l'architecture et le plan du jalon 1.
