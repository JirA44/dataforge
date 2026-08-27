# Exemples d'utilisation de DataForge V1.07

Ce guide présente une installation PowerShell et des appels API complets. Les
identifiants et résultats sont retournés par le serveur ; les verdicts ne sont jamais
envoyés dans les requêtes.

## 1. Installation sous Windows et PowerShell

```powershell
Set-Location .\dataforge
py -3.10 -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
.\scripts\Test-DataForge.ps1
.\scripts\Start-DataForge.ps1
```

L'API écoute par défaut sur `http://127.0.0.1:8010`. Swagger est disponible sur
`http://127.0.0.1:8010/docs`.

```powershell
$BaseUri = "http://127.0.0.1:8010"
$Headers = @{ "X-Actor" = "hugo" }
Invoke-RestMethod -Uri "$BaseUri/health"
```

## 2. Enregistrer une source, un dataset et deux versions

```powershell
$Source = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/sources" -Headers $Headers -ContentType "application/json" `
  -Body (@{
    name = "customers.csv"
    kind = "file"
    uri = "file:///D:/imports/customers.csv"
    metadata = @{ owner = "analytics" }
  } | ConvertTo-Json -Depth 10)

$Dataset = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/datasets" -Headers $Headers -ContentType "application/json" `
  -Body (@{
    name = "customers"
    description = "Référentiel clients"
    schema = @{
      allow_extra = $false
      fields = @{
        id = @{ type = "integer"; required = $true }
        name = @{ type = "string"; required = $true }
      }
    }
  } | ConvertTo-Json -Depth 10)

$Version1 = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/datasets/$($Dataset.id)/versions" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    source_id = $Source.id
    records = @(
      @{ id = 1; name = "Ada" }
      @{ id = 2; name = "Grace" }
    )
  } | ConvertTo-Json -Depth 10)

$Version2 = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/datasets/$($Dataset.id)/versions" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    source_id = $Source.id
    records = @(
      @{ id = 1; name = "Ada" }
      @{ id = 2; name = "Grace Hopper" }
    )
  } | ConvertTo-Json -Depth 10)
```

## 3. Vérifier la provenance et la dérive

```powershell
Invoke-RestMethod `
  -Uri "$BaseUri/v1/versions/$($Version2.id)/provenance/verify"

$Drift = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/versions/$($Version1.id)/drift-reports" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{ candidate_version_id = $Version2.id } | ConvertTo-Json)
$Drift.verdict
```

## 4. Créer et contrôler un contrat de données

```powershell
$ContractV1 = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/datasets/$($Dataset.id)/contracts" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    name = "customers-contract-v1"
    fields = @{
      id = @{
        types = @("integer")
        required = $true
        nullable = $false
        max_missing_rate = 0
        unique = $true
      }
      name = @{
        types = @("string")
        required = $true
        nullable = $false
        max_missing_rate = 0
        unique = $false
      }
    }
    allow_extra = $false
    min_rows = 1
    max_rows = 1000000
    max_duplicate_rate = 0
  } | ConvertTo-Json -Depth 10)

$ContractCheck = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/contracts/$($ContractV1.id)/reports" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{ version_id = $Version2.id } | ConvertTo-Json)
$ContractCheck.verdict
```

## 5. Comparer deux versions de contrat

```powershell
$ContractV2Body = @{
  name = "customers-contract-v2"
  fields = @{
    id = @{ types = @("integer"); required = $true; nullable = $false; max_missing_rate = 0; unique = $true }
    name = @{ types = @("string"); required = $true; nullable = $true; max_missing_rate = 0.05; unique = $false }
  }
  allow_extra = $false
  min_rows = 1
  max_rows = 1000000
  max_duplicate_rate = 0
}
$ContractV2 = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/datasets/$($Dataset.id)/contracts" `
  -Headers $Headers -ContentType "application/json" `
  -Body ($ContractV2Body | ConvertTo-Json -Depth 10)

$Compatibility = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/contract-compatibility-reports" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    baseline_contract_id = $ContractV1.id
    candidate_contract_id = $ContractV2.id
  } | ConvertTo-Json)
$Compatibility.qualification
```

## 6. Déclarer le lignage et calculer l'impact aval

```powershell
$Lineage = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/lineage-links" -Headers $Headers -ContentType "application/json" `
  -Body (@{
    upstream_version_id = $Version1.id
    downstream_version_id = $Version2.id
    relation_type = "TRANSFORMED_FROM"
  } | ConvertTo-Json)

$Impact = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/impact-reports" -Headers $Headers -ContentType "application/json" `
  -Body (@{ changed_version_id = $Version1.id; max_depth = 5 } | ConvertTo-Json)
$Impact.qualification
```

## 7. Construire le dossier de fermeture de provenance V1.05

La requête contient uniquement une liste de 1 à 50 identifiants de versions persistées.
L'ordre ne modifie ni le snapshot ni le rapport.

```powershell
$Closure = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/provenance-closure-reports" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{ version_ids = @($Version2.id) } | ConvertTo-Json -Depth 5)

$Closure.qualification
$Closure.chains
$Closure.integrity
$Closure.missing_references
$Closure.unused_versions

Invoke-RestMethod -Uri "$BaseUri/v1/provenance-closure-reports/$($Closure.id)"
Invoke-RestMethod -Uri "$BaseUri/v1/provenance-closure-reports?limit=20&offset=0"
```

Les qualifications sont `COMPLETE`, `PARTIAL`, `BROKEN`, `INSUFFICIENT` et
`INCOMPATIBLE`. Consultez le README pour les règles fixes. Un dossier `BROKEN` ne doit
jamais être présenté comme une certification valide.

## 8. Mesurer l'impact de provenance aval V1.06

La sélection mélange éventuellement versions et datasets, avec 50 identifiants au
maximum. Le serveur calcule seul les chemins, compteurs, seuils et qualification.

```powershell
$DossierImpact = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/provenance-impact-dossiers" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{
    version_ids = @($Version1.id)
    dataset_ids = @()
  } | ConvertTo-Json -Depth 5)

$DossierImpact.qualification
$DossierImpact.affected
$DossierImpact.worst_branch
$DossierImpact.orphan_references
$DossierImpact.summary

Invoke-RestMethod -Uri "$BaseUri/v1/provenance-impact-dossiers/$($DossierImpact.id)"
Invoke-RestMethod -Uri "$BaseUri/v1/provenance-impact-dossiers?limit=20&offset=0"
```

Ne transmettez jamais `qualification`, `summary`, `affected` ou `worst_branch` : les
modèles stricts refusent ces résultats clients.

## 9. Attribuer l'évolution chronologique du lignage V1.07

```powershell
$Evolution = Invoke-RestMethod -Method Post `
  -Uri "$BaseUri/v1/lineage-evolution-dossiers" `
  -Headers $Headers -ContentType "application/json" `
  -Body (@{ version_ids = @($Version2.id, $Version1.id) } | ConvertTo-Json -Depth 5)

$Evolution.qualification
$Evolution.chronological_version_ids
$Evolution.transitions | ConvertTo-Json -Depth 12
$Evolution.worst_transition | ConvertTo-Json -Depth 12

Invoke-RestMethod -Uri "$BaseUri/v1/lineage-evolution-dossiers/$($Evolution.id)"
Invoke-RestMethod -Uri "$BaseUri/v1/lineage-evolution-dossiers?limit=20&offset=0"
```

N’envoyez jamais de `qualification`, `transitions`, `summary` ou pire branche. Le
serveur attribue uniquement les différences de lignage persistées, sans inférer de
cause externe ni de vérité du contenu.
