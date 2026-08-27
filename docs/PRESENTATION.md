# Dataforge — Présentation complète

## Présentation
dataforge est un registre immuable, hashé (SHA-256), auditable et rejouable.

## À quoi ça sert ? (problèmes réglés)
- **Dataset modifié sans version** → résolu par un dossier déterministe, ordre-indépendant
- **Source non tracée -> RGPD impossible** → résolu par un dossier déterministe, ordre-indépendant
- **Deux versions incompatibles utilisées comme si compatibles** → résolu par un dossier déterministe, ordre-indépendant

## Cas d'utilisation concrets
- Pipeline data: prouver le lineage source -> dataset -> version
- Audit RGPD: fermer la provenance jusqu à la source consentie
- ML: comparer deux contrats de données avant entraînement

## Exemples d'utilisation (API)
```bash
curl -X POST http://localhost:8000/v1/lineage-evolution-dossiers -d '{"dataset_ids": [...] }'
# → { "qualification": "COMPLETE|GAPPED|INSUFFICIENT|INCOMPATIBLE", "coverage_ratio": 0.94, ... }
```

## À quoi ça pourrait servir (futur / possibilités)
- Data mesh certifié
- Traçabilité alimentaire / pharma
- Open data avec preuve SHA-256

## Pour qui ?
Devs, auditeurs, ops, chercheurs — qui ont besoin d'une preuve opposable, pas d'un verdict déclaratif.