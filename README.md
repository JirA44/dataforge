> **Présentation → [docs/PRESENTATION.md](docs/PRESENTATION.md)** — à quoi ça sert, cas d'usages, usages futurs.

# DataForge V1.07

DataForge est un registre de jeux de données auditable. Il enregistre des sources, des
datasets et des versions immuables, calcule leur provenance SHA-256 et exécute des
contrôles qualité conservateurs. Il compare deux versions immuables, versionne des
contrats de données stricts, compare leur compatibilité, relie les versions par un
lignage immuable, ferme la provenance jusqu'aux sources persistées et publie des
rapports hashés. Aucun verdict, diff, parcours ou preuve n'est accepté depuis le client.

## Garanties V1.07

- chaque source et dataset possède une empreinte SHA-256 de sa définition canonique ;
- chaque version contient les données JSON canoniques, leur `content_hash` et un manifeste ;
- le `provenance_hash` est le SHA-256 du manifeste et référence le hash de la version précédente ;
- les versions, évaluations qualité et événements d'audit sont protégés par des triggers SQLite ;
- les contrôles `missing`, `duplicates`, `schema` et `provenance` sont calculés ;
- l'audit est append-only et enregistre l'acteur transmis dans l'en-tête `X-Actor` ;
- une réévaluation ajoute un résultat daté sans modifier la version ni l'ancien résultat.
- un rapport de dérive est calculé uniquement depuis les contenus stockés, puis protégé
  par les mêmes triggers d'immutabilité ;
- la même paire baseline/candidate et la même version de règles retournent exactement
  le même rapport, sans doubler l'audit.
- chaque définition de contrat est immuable, numérotée et chaînée à la précédente ;
- le contrôle d'une version contre un contrat précis est déterministe, idempotent,
  hashé et qualifié `COMPATIBLE`, `VIOLATION` ou `INSUFFICIENT` côté serveur.
- chaque lien de lignage directionnel est vérifié, hashé, immuable et idempotent ;
- l'impact aval est borné à une profondeur de 1 à 10, expose un chemin déterministe
  par version touchée et reprend la conformité contractuelle disponible ;
- les qualifications `ISOLATED`, `CONTAINED`, `PROPAGATED` et `CYCLE_DETECTED` sont
  exclusivement calculées par le serveur.
- la compatibilité entre deux snapshots de contrats est évaluée séparément dans les
  directions backward et forward, justifiée, hashée, immuable et idempotente.
- le dossier de fermeture recharge uniquement les identifiants persistés, recalcule
  tous les hashes et n'invente jamais une source, une version ou un lien manquant ;
- son snapshot est indépendant de l'ordre d'entrée, hashé, immuable, idempotent et
  audité en append-only.
- le dossier d’impact aval vérifie les artefacts atteints et expose chemins,
  profondeur, ampleur, références orphelines, cycles et pire branche.
- le dossier chronologique attribue l’évolution du lignage aval uniquement aux liens
  persistés, sans prétendre établir une causalité externe.

### Règle de verdict

| Situation | Verdict |
|---|---|
| Tous les contrôles obligatoires sont `PASS` | `VERIFIED` |
| Au moins un contrôle est `FAIL` | `REJECTED` |
| Aucun échec, mais au moins un contrôle est inconclusif | `INSUFFICIENT` |

Un dataset vide est `INSUFFICIENT`. Un dataset sans schéma déclaré reste
`INSUFFICIENT`, même si aucune valeur manquante ni aucun doublon n'est observé. Il n'y
a donc pas de faux `PASS` du contrôle de schéma.

## Démarrage Windows / PowerShell

Prérequis : Python 3.10+ accessible par `py -3.10`.

```powershell
Set-Location .\dataforge
.\scripts\Start-DataForge.ps1
```

Le script crée `.venv`, installe le projet puis démarre l'API sur
`http://127.0.0.1:8010`. Swagger UI est disponible sur
`http://127.0.0.1:8010/docs`.

Paramètres facultatifs :

```powershell
.\scripts\Start-DataForge.ps1 `
  -DatabasePath "D:\DataForge\dataforge.sqlite3" `
  -HostAddress "127.0.0.1" `
  -Port 8010
```

Pour un déploiement exposé sur un réseau, placer l'application derrière un reverse
proxy avec TLS et authentification. V1.07 n'embarque volontairement aucun mécanisme
d'authentification.

## Premier dataset

```powershell
$Headers = @{ "X-Actor" = "hugo" }

$Source = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8010/v1/sources" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    name = "customers.csv"
    kind = "file"
    uri = "file:///D:/imports/customers.csv"
    metadata = @{ owner = "analytics" }
  } | ConvertTo-Json -Depth 10)

$Dataset = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8010/v1/datasets" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    name = "customers"
    description = "Référentiel clients"
    schema = @{
      allow_extra = $false
      fields = @{
        id = @{ type = "integer"; required = $true }
        name = @{ type = "string"; required = $true }
        score = @{ type = "number"; required = $false }
      }
    }
  } | ConvertTo-Json -Depth 10)

$Version = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8010/v1/datasets/$($Dataset.id)/versions" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    source_id = $Source.id
    records = @(
      @{ id = 1; name = "Ada"; score = 9.5 }
      @{ id = 2; name = "Grace" }
    )
  } | ConvertTo-Json -Depth 10)

$Version.quality
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8010/v1/versions/$($Version.id)/provenance/verify"
```

## Contrôles qualité

### Valeurs manquantes

Avec un schéma déclaré, seuls les champs `required: true` sont obligatoires. Le
contrôle compte les clés absentes et les valeurs `null`, puis publie
`missing_values`, `checked_cells` et `missing_rate`. Sans schéma, il calcule ces
mesures sur l'union observée des champs ; le verdict global reste toutefois
`INSUFFICIENT` car le contrôle de schéma ne peut pas conclure.

### Doublons

Deux lignes sont identiques lorsque leur représentation JSON canonique est identique,
indépendamment de l'ordre initial des clés. Le résultat inclut `duplicate_rows`,
`unique_rows` et `duplicate_rate`.

### Schéma

Types disponibles : `string`, `integer`, `number`, `boolean`, `object`, `array` et
`null`. `allow_extra: false` rejette les champs non déclarés. Les 100 premières
violations sont retournées et `violation_count` conserve le total exact.

### Provenance

La vérification recalcule les hashes du contenu et des manifestes depuis la première
version du dataset, contrôle le chaînage, les numéros, les sources et le dataset.
L'endpoint de réévaluation qualité réexécute d'abord cette vérification.

## Comparaison de dérive V1.01

`POST /v1/versions/{baseline_id}/drift-reports` reçoit uniquement
`candidate_version_id`. DataForge impose que la baseline précède la candidate dans le
même dataset. Il infère ensuite depuis les contenus :

- les champs ajoutés/supprimés et les types JSON observés modifiés ;
- le taux de valeurs absentes ou `null` par champ ;
- le nombre de lignes et sa variation relative ;
- le nombre et le taux de doublons canoniques.

Les seuils sont fixés côté serveur : variation relative des lignes supérieure à 10 %,
variation absolue des valeurs manquantes supérieure à 5 points, ou variation absolue
des doublons supérieure à 5 points. Tout changement structurel déclenche aussi une
dérive. Les verdicts sont `STABLE`, `DRIFTED` ou `INSUFFICIENT` lorsque l'une des
versions est vide ou sans champ observable.

Le `report_hash` SHA-256 lie les identifiants et hashes de contenu des deux versions,
les métriques, le verdict et la version des règles. Le rapport et son événement
`DRIFT_REPORT_CREATED` sont immuables/append-only.

Exemple, après création de deux versions :

```powershell
$Drift = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8010/v1/versions/$($BaselineVersion.id)/drift-reports" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{ candidate_version_id = $CandidateVersion.id } | ConvertTo-Json)
$Drift.verdict
```

## Contrats de données V1.02

`POST /v1/datasets/{dataset_id}/contracts` ajoute une nouvelle version immuable de
contrat. Un contrat fixe côté serveur :

- les champs déclarés et l'autorisation éventuelle de champs supplémentaires ;
- les types JSON observables autorisés (`string`, `integer`, `number`, `boolean`,
  `object`, `array`) ;
- `required`, `nullable`, le taux maximal de valeurs absentes ou `null`, et
  l'unicité éventuelle pour chaque champ ;
- les nombres minimal et maximal de lignes et le taux maximal de lignes dupliquées.

Les taux sont bornés entre 0 et 1, les nombres de lignes entre 0 et 10 000 000, et
`max_rows` ne peut pas être inférieur à `min_rows`. Les champs inconnus sont refusés
par les modèles d'entrée (`extra=forbid`). La liste `types` n'inclut pas `null` : la
présence de `null` est contrôlée séparément par `nullable` et `max_missing_rate`.

Le contrôle reçoit uniquement `version_id` :

```powershell
$Contract = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8010/v1/datasets/$($Dataset.id)/contracts" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    name = "customers-contract"
    fields = @{
      id = @{ types = @("integer"); required = $true; nullable = $false; unique = $true }
      name = @{ types = @("string"); required = $true; nullable = $false; max_missing_rate = 0.01 }
    }
    allow_extra = $false
    min_rows = 1
    max_rows = 1000000
    max_duplicate_rate = 0
  } | ConvertTo-Json -Depth 10)

$Report = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8010/v1/contracts/$($Contract.id)/reports" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{ version_id = $Version.id } | ConvertTo-Json)
$Report.verdict
```

`COMPATIBLE` signifie que toutes les règles ont réussi sur les lignes stockées.
`VIOLATION` contient les violations calculées (schéma, type, valeurs manquantes,
nullabilité, unicité, volume ou doublons). Une version vide est `INSUFFICIENT`, car
elle ne fournit aucune observation permettant de conclure. Le même couple
contrat/version et la même version de règles retourne le même rapport sans nouvel
événement d'audit.

## Compatibilité des contrats V1.04

La requête `POST /v1/contract-compatibility-reports` accepte exclusivement
`baseline_contract_id` et `candidate_contract_id`. DataForge recharge les deux
snapshots immuables, vérifie leurs hashes puis calcule deux inclusions d'ensembles :

- **backward** (`baseline_to_candidate`) : toute donnée acceptée par la baseline doit
  encore être acceptée par le candidat ; autrement dit, le nouveau contrat accepte
  les anciennes données conformes ;
- **forward** (`candidate_to_baseline`) : toute donnée acceptée par le candidat doit
  aussi être acceptée par la baseline ; autrement dit, les nouvelles données
  conformes restent acceptables pour l'ancien contrat.

Cette convention est propre au validateur de données DataForge et figure aussi dans
OpenAPI. Elle évite l'ambiguïté fréquente entre compatibilité de lecteurs et de
producteurs.

```powershell
$Compatibility = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8010/v1/contract-compatibility-reports" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    baseline_contract_id = $ContractV1.id
    candidate_contract_id = $ContractV2.id
  } | ConvertTo-Json)
```

Le rapport sépare les champs ajoutés/retirés, changements de types, transitions de
`required` et `nullable`, nouveaux champs obligatoires, contraintes resserrées et
contraintes relâchées. Les règles couvertes comprennent aussi `allow_extra`,
`max_missing_rate`, `unique`, `min_rows`, `max_rows` et `max_duplicate_rate`. Chaque
direction incompatible contient ses raisons calculées.

| Résultat directionnel | Qualification |
|---|---|
| Backward et forward vraies | `FULLY_COMPATIBLE` |
| Backward vraie uniquement | `BACKWARD_COMPATIBLE` |
| Forward vraie uniquement | `FORWARD_COMPATIBLE` |
| Les deux fausses | `BREAKING` |
| Contrats de datasets différents | `INSUFFICIENT` |

La comparaison de contrats appartenant à deux datasets différents est volontairement
`INSUFFICIENT` : DataForge fournit le diff structurel mais ne prétend pas établir une
substituabilité métier entre datasets sans base commune. Les deux snapshots, les
preuves et le rapport ont chacun un SHA-256. Une paire inchangée retourne exactement
le même rapport sans nouvel événement d'audit.

## Lignage et impact aval V1.03

`POST /v1/lineage-links` relie deux identifiants de versions déjà stockées. Le sens est
toujours `upstream_version_id` vers `downstream_version_id`. Les types documentaires
autorisés sont :

- `DERIVED_FROM` : dérivation générale ;
- `TRANSFORMED_FROM` : transformation de valeurs ou de structure ;
- `FILTERED_FROM` : sélection d'un sous-ensemble ;
- `AGGREGATED_FROM` : agrégation ;
- `JOINED_FROM` : participation à une jointure ;
- `COPIED_FROM` : copie sans transformation déclarée.

DataForge vérifie l'existence des deux versions et interdit l'auto-lien. Un même triplet
upstream/downstream/relation retourne le même lien sans ajouter d'audit. Les cycles
entre plusieurs versions restent enregistrables afin que le rapport puisse les
signaler explicitement.

```powershell
$Link = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8010/v1/lineage-links" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    upstream_version_id = $RawVersion.id
    downstream_version_id = $PreparedVersion.id
    relation_type = "TRANSFORMED_FROM"
  } | ConvertTo-Json)

$Impact = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8010/v1/impact-reports" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    changed_version_id = $RawVersion.id
    max_depth = 5
  } | ConvertTo-Json)
```

La requête d'impact contient uniquement `changed_version_id` et `max_depth`, borné de
1 à 10. Le serveur traverse les liens en direction aval et retourne, pour chaque
version distincte atteinte, un plus court chemin déterministe, sa profondeur, son
dataset et son dernier rapport de conformité contractuelle disponible. À défaut,
`contract_compliance.status` vaut `NOT_AVAILABLE` : aucune conformité n'est inventée.

### Qualification fixe

| Situation calculée dans la profondeur demandée | Qualification |
|---|---|
| Aucune version aval distincte | `ISOLATED` |
| Une ou deux versions aval distinctes | `CONTAINED` |
| Au moins trois versions aval distinctes | `PROPAGATED` |
| Cycle détecté dans le sous-graphe atteint | `CYCLE_DETECTED` |

`CYCLE_DETECTED` est prioritaire sur les autres qualifications. Le seuil de propagation
est fixe à **3 versions aval distinctes**. Le `evidence_hash` lie l'état du graphe et
les conformités disponibles ; le `report_hash` lie ensuite toutes les entrées et tous
les résultats calculés. Tant que ces preuves ne changent pas, la même demande retourne
exactement le même rapport et ne double pas l'audit.

## Dossier de fermeture de provenance V1.05

`POST /v1/provenance-closure-reports` reçoit uniquement `version_ids`, une liste de
1 à 50 identifiants uniques déjà persistés. Le serveur trie la sélection afin que le
snapshot soit indépendant de l'ordre, puis recharge les versions, datasets, sources
et liens de lignage depuis SQLite.

Il recalcule les hashes de source, dataset, contenu, manifeste et lien de lignage,
contrôle le nombre de lignes et les références du manifeste, suit les versions
précédentes et les dépendances amont jusqu'aux sources, sans jamais compléter une
référence absente. Le rapport expose les chaînes, références manquantes, orphelins,
cycles, ruptures d'intégrité et versions stockées non utilisées par la fermeture.

```powershell
$Closure = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/provenance-closure-reports" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{ version_ids = @($Version2.id) } | ConvertTo-Json -Depth 5)
$Closure.qualification
```

### Règles fixes de qualification

| Priorité | Situation | Qualification |
|---:|---|---|
| 1 | Hash invalide, référence manquante, cycle ou rupture de chaîne | `BROKEN` |
| 2 | Plusieurs versions demandées réparties en composants sans lien | `INCOMPATIBLE` |
| 3 | Une seule première version sans ascendance persistée | `INSUFFICIENT` |
| 4 | Intégrité valide mais versions des datasets concernés hors fermeture | `PARTIAL` |
| 5 | Chaînes, références et hashes complets, sans version inutilisée | `COMPLETE` |

`BROKEN` est prioritaire : un dossier cassé ne devient jamais complet grâce à un
autre signal favorable. `INSUFFICIENT` ne signifie pas faux ; il signifie que la seule
version fournie ne contient pas assez d'ascendance persistée pour fermer une chaîne.
Le `snapshot_hash` lie les artefacts rechargés dans un ordre canonique et le
`report_hash` lie tout le résultat. Une même sélection et un même snapshot retournent
le même rapport sans doubler l'audit.

## Impact de provenance en aval V1.06

`POST /v1/provenance-impact-dossiers` accepte uniquement `version_ids` et
`dataset_ids`, pour un total de 1 à 50 identifiants uniques déjà persistés. Un dataset
sélectionné est développé vers toutes ses versions. Le serveur suit ensuite les
versions suivantes et les liens de lignage upstream vers downstream.

Les hashes des sources, datasets, versions et liens atteints sont recalculés. Le
dossier expose les éléments affectés et leur plus court chemin déterministe, les
références orphelines, cycles, ruptures, profondeur maximale, ampleur et pire branche.
Aucune métrique, branche ou qualification n’est acceptée du client.

| Priorité | Règle fixe | Qualification |
|---:|---|---|
| 1 | hash invalide, référence orpheline ou cycle | `INCOMPATIBLE` |
| 2 | dataset sans version ou aucun aval observé | `INSUFFICIENT` |
| 3 | au moins 5 versions, 3 datasets ou profondeur 4 | `WIDESPREAD` |
| 4 | impact présent sous tous ces seuils | `CONTAINED` |

Le snapshot est indépendant de l’ordre et le dossier est hashé, immuable, idempotent
et audité. Il décrit uniquement les dépendances persistées, sans prédire d’effet
métier ni inventer de référence absente.

## Évolution chronologique du lignage V1.07

`POST /v1/lineage-evolution-dossiers` reçoit uniquement `version_ids`, une sélection
de 2 à 100 versions uniques. Le serveur impose l’ordre
`(version_number, created_at, id)`, vérifie que les versions appartiennent au même
dataset, recalcule leurs preuves de provenance et reconstruit pour chaque état le
graphe aval déclaré.

Chaque transition expose dépendances ajoutées/retirées, ruptures nouvelles/résolues,
références devenues orphelines ou résolues, branches touchées et score de sévérité.
La pire transition maximise, dans cet ordre pondéré, ruptures nouvelles, orphelins,
retraits puis ajouts ; la plus récente départage les égalités.

| Priorité | Règle fixe | Qualification |
|---:|---|---|
| 1 | versions de datasets différents | `INCOMPATIBLE` |
| 2 | preuve de provenance racine manquante/invalide ou aucun lignage aval | `INSUFFICIENT` |
| 3 | rupture, orphelin ou cycle dans un état aval | `PARTIAL` |
| 4 | transitions entièrement attribuées aux liens vérifiés | `EXPLAINED` |

Le dossier explique uniquement les changements observables dans le registre. Il
n’infère aucune causalité externe et ne certifie pas la vérité du contenu.
Un identifiant qui n’existe pas est rejeté en `404`; une version persistée dont la
preuve de provenance est absente ou invalide produit prudemment `INSUFFICIENT`.

## API

| Méthode | Route | Usage |
|---|---|---|
| `GET` | `/health` | Santé et version |
| `GET` | `/info` | Version, édition et capacités actives |
| `POST`, `GET` | `/v1/sources` | Créer/lister les sources |
| `GET` | `/v1/sources/{id}` | Lire une source |
| `POST`, `GET` | `/v1/datasets` | Créer/lister les datasets |
| `GET` | `/v1/datasets/{id}` | Lire un dataset |
| `POST`, `GET` | `/v1/datasets/{id}/versions` | Créer/lister les versions |
| `GET` | `/v1/versions/{id}` | Lire une version immuable |
| `GET` | `/v1/versions/{id}/provenance/verify` | Recalculer l'intégrité |
| `GET` | `/v1/versions/{id}/quality` | Dernière évaluation |
| `POST` | `/v1/versions/{id}/quality-checks` | Ajouter une réévaluation |
| `POST` | `/v1/versions/{baseline_id}/drift-reports` | Calculer/relire un rapport idempotent |
| `GET` | `/v1/drift-reports/{id}` | Lire un rapport de dérive immuable |
| `GET` | `/v1/datasets/{id}/drift-reports` | Lister les rapports d'un dataset |
| `POST`, `GET` | `/v1/datasets/{id}/contracts` | Créer/lister les versions de contrats |
| `GET` | `/v1/contracts/{id}` | Lire un contrat immuable |
| `POST` | `/v1/contracts/{id}/reports` | Contrôler une version contre ce contrat |
| `GET` | `/v1/contract-reports/{id}` | Lire un rapport de contrat immuable |
| `GET` | `/v1/datasets/{id}/contract-reports` | Lister les rapports de contrat |
| `POST` | `/v1/contract-compatibility-reports` | Comparer deux contrats immuables |
| `GET` | `/v1/contract-compatibility-reports/{id}` | Lire une compatibilité immuable |
| `GET` | `/v1/contracts/{id}/compatibility-reports` | Lister les comparaisons d'une baseline |
| `POST`, `GET` | `/v1/lineage-links` | Créer/lister les liens de lignage |
| `GET` | `/v1/lineage-links/{id}` | Lire un lien immuable |
| `POST` | `/v1/impact-reports` | Calculer/relire un impact aval borné |
| `GET` | `/v1/impact-reports/{id}` | Lire un rapport d'impact immuable |
| `GET` | `/v1/versions/{id}/impact-reports` | Lister les rapports d'une version changée |
| `POST`, `GET` | `/v1/provenance-closure-reports` | Créer/lister les dossiers de fermeture |
| `GET` | `/v1/provenance-closure-reports/{id}` | Lire un dossier immuable |
| `POST`, `GET` | `/v1/provenance-impact-dossiers` | Créer/lister les impacts aval |
| `GET` | `/v1/provenance-impact-dossiers/{id}` | Lire un impact aval immuable |
| `POST`, `GET` | `/v1/lineage-evolution-dossiers` | Créer/lister les évolutions attribuées |
| `GET` | `/v1/lineage-evolution-dossiers/{id}` | Lire un dossier chronologique immuable |
| `GET` | `/v1/audit` | Lire/filtrer l'audit |

Le contrat statique est dans [`docs/openapi.yaml`](docs/openapi.yaml). Lorsque le
serveur tourne, `/openapi.json` est la représentation générée par FastAPI.

Pour des parcours API complets en français, voir
[`docs/USAGE_EXAMPLES.md`](docs/USAGE_EXAMPLES.md). Les règles de contribution et de
validation sont détaillées dans [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Tests

Tests complets avec les dépendances de développement :

```powershell
.\scripts\Test-DataForge.ps1
```

Tests du cœur SQLite/qualité sans FastAPI, HTTPX ni pytest :

```powershell
.\scripts\Test-Core-NoDeps.ps1
```

Équivalent multiplateforme :

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Stockage

SQLite est utilisé par défaut dans `data/dataforge.sqlite3`. La variable
`DATAFORGE_DB_PATH` permet de changer ce chemin. Le schéma PostgreSQL 14+ de référence
est fourni dans [`docs/postgresql-schema.sql`](docs/postgresql-schema.sql) ; il reprend
les contraintes et triggers d'immutabilité, mais V1.07 n'inclut pas encore
l'adaptateur d'exécution PostgreSQL.

## Limites explicites de V1.07

- ingestion JSON en mémoire, sans import CSV/Parquet ni stockage objet ;
- aucune mise à jour/suppression applicative : on crée une nouvelle version ;
- aucune prétention statistique au-delà des contrôles documentés ;
- aucune certification externe : `VERIFIED` signifie uniquement que les règles
  qualité DataForge ont toutes réussi sur la version hashée ;
- `STABLE` signifie uniquement qu'aucune dérive couverte par les règles V1.01 n'a
  dépassé les seuils documentés ; ce n'est pas une preuve d'identité des données.
- `COMPATIBLE` signifie uniquement que la version respecte le contrat DataForge
  sélectionné ; ce n'est pas une certification de la véracité métier des données.
- un rapport d'impact décrit uniquement le lignage déclaré et atteint dans
  `max_depth` ; il ne prétend pas découvrir des dépendances jamais enregistrées.
- la compatibilité compare les ensembles acceptés par les règles DataForge couvertes ;
  elle ne remplace pas une validation sémantique du métier ou des consommateurs réels.
- un dossier de fermeture décrit uniquement les objets persistés et les liens déclarés ;
  une dépendance externe non enregistrée demeure inconnue et n'est jamais inventée.

