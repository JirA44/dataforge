# Changelog DataForge

## V1.07 — 1.0.7

- dossier chronologique sur 2 à 100 versions compatibles d’un même dataset ;
- ordre serveur par numéro de version/date/id, revérification de provenance, hashes,
  références et liens, puis recalcul des ensembles aval par état ;
- attribution des dépendances ajoutées/retirées, ruptures nouvelles/résolues,
  références orphelines/résolues, branches touchées et pire transition ;
- qualifications prudentes `EXPLAINED`, `PARTIAL`, `INSUFFICIENT`, `INCOMPATIBLE` ;
- snapshot immuable/idempotent, audit append-only, API, SQLite/PostgreSQL, OpenAPI,
  PowerShell, documentation et CI alignés sur 1.0.7.

## V1.06 — 1.0.6

- dossiers immuables d’impact aval depuis 1 à 50 identifiants de versions/datasets ;
- parcours déterministe des versions suivantes et lignages avec chemins, profondeur,
  ampleur, pire branche, références orphelines, cycles et vérification des hashes ;
- qualifications serveur `CONTAINED`, `WIDESPREAD`, `INSUFFICIENT`, `INCOMPATIBLE`
  selon des priorités et seuils fixes documentés ;
- snapshot SHA-256 indépendant de l’ordre, idempotence, audit append-only, API
  POST/GET/list, SQLite/PostgreSQL, OpenAPI 3.1, PowerShell et CI mis à jour.

## V1.05 — 1.0.5

- ajout de dossiers immuables de fermeture de provenance fondés uniquement sur 1 à
  50 identifiants de versions persistées ;
- rechargement des versions, datasets, sources et liens de lignage, avec recalcul des
  hashes et reconstruction des chaînes amont sans provenance inventée ;
- détection explicite des références manquantes, orphelins, cycles, ruptures
  d'intégrité et versions inutilisées ;
- qualifications serveur prioritaires `COMPLETE`, `PARTIAL`, `BROKEN`,
  `INSUFFICIENT` et `INCOMPATIBLE` ;
- snapshot indépendant de l'ordre, rapport hashé et idempotent, immutabilité SQLite
  et audit append-only `PROVENANCE_CLOSURE_REPORT_CREATED` ;
- API POST/GET/list, PostgreSQL de référence et OpenAPI 3.1 statique/runtime mis à jour ;
- préparation Git/GitHub avec `.gitignore`, guide de contribution, exemples français
  PowerShell/API et workflow CI Python 3.10/3.12.

## V1.04 — 1.0.4

- ajout de rapports de compatibilité entre deux contrats immuables ;
- directions documentées `baseline_to_candidate` (backward) et
  `candidate_to_baseline` (forward) évaluées séparément ;
- diff calculé pour ajouts/retraits de champs, types, `required`, `nullable`, nouveaux
  champs obligatoires et contraintes resserrées/relâchées ;
- qualifications serveur `FULLY_COMPATIBLE`, `BACKWARD_COMPATIBLE`,
  `FORWARD_COMPATIBLE`, `BREAKING`, `INSUFFICIENT` ;
- snapshots, preuve et rapport hashés, immuables et idempotents ;
- audit append-only `CONTRACT_COMPATIBILITY_REPORT_CREATED` ;
- API POST/GET/list, SQLite, PostgreSQL, OpenAPI statique/runtime et tests ajoutés.

## V1.03 — 1.0.3

- ajout de liens de lignage directionnels immuables entre versions existantes ;
- six types documentaires de relation et rejet explicite des auto-liens ;
- liens hashés, idempotents et audités via `LINEAGE_LINK_CREATED` ;
- rapports d'impact aval bornés par `max_depth` de 1 à 10 ;
- chemins, profondeurs, versions et datasets touchés calculés côté serveur ;
- reprise prudente de la dernière conformité contractuelle disponible ;
- qualifications serveur `ISOLATED`, `CONTAINED`, `PROPAGATED`, `CYCLE_DETECTED`,
  avec seuil fixe de propagation à trois versions aval distinctes ;
- rapports hashés, immuables, idempotents et audités via `IMPACT_REPORT_CREATED` ;
- API, OpenAPI statique/runtime, SQLite, PostgreSQL et tests de non-régression mis à jour.

## V1.02 — 1.0.2

- ajout de contrats de données immuables, numérotés et chaînés par hash SHA-256 ;
- règles strictes pour champs requis, types observables, nullabilité, taux de valeurs
  manquantes, unicité, volumes et taux de doublons ;
- contrôle déterministe d'une version immuable contre une version précise de contrat ;
- verdict serveur `COMPATIBLE`, `VIOLATION` ou `INSUFFICIENT`, jamais fourni par le client ;
- rapports hashés, immuables et idempotents par contrat/version/version de règles ;
- audit append-only `DATA_CONTRACT_CREATED` et `CONTRACT_REPORT_CREATED` ;
- API, contrats OpenAPI statique/runtime, schémas SQLite/PostgreSQL et tests complets.

## V1.01 — 1.0.1

- ajout des comparaisons déterministes entre une version baseline et une candidate ;
- inférence des champs et types observés depuis les contenus immuables ;
- comparaison des taux de valeurs manquantes, nombres de lignes et taux de doublons ;
- verdict serveur `STABLE`, `DRIFTED` ou `INSUFFICIENT`, jamais fourni par le client ;
- rapport SHA-256 immuable et création idempotente par paire/version de règles ;
- audit append-only `DRIFT_REPORT_CREATED` ;
- endpoints de création, lecture et liste, avec contrats OpenAPI statique et runtime ;
- tables, index et triggers ajoutés aux schémas SQLite et PostgreSQL.

## V1.00 — 1.0.0

- registre initial de sources, datasets et versions immuables ;
- provenance chaînée SHA-256, contrôles qualité calculés et audit append-only.
