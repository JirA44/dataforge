# Contribuer à DataForge

Merci de contribuer à DataForge. Le projet privilégie les résultats calculés,
reproductibles et auditables. Aucun endpoint ne doit accepter un verdict, un diff ou
une preuve déjà calculée par le client.

## Préparer l'environnement sous Windows

Prérequis : Git, PowerShell 7 et Python 3.10 ou plus récent.

```powershell
git clone <URL_DU_DEPOT>
Set-Location .\dataforge
py -3.10 -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Le dépôt n'est pas initialisé ni publié automatiquement par le starter. Remplacez
`<URL_DU_DEPOT>` uniquement après avoir créé votre propre dépôt GitHub.

## Exécuter les contrôles

```powershell
.\scripts\Test-DataForge.ps1
python -m compileall -q src tests
```

Les tests du cœur sans FastAPI sont également disponibles :

```powershell
.\scripts\Test-Core-NoDeps.ps1
```

Avant une pull request, vérifiez que :

- tous les tests historiques et nouveaux réussissent ;
- `VERSION`, `pyproject.toml`, `dataforge.__version__`, `/health` et OpenAPI portent
  exactement la même version ;
- les entrées Pydantic sont strictes et `extra="forbid"` ;
- les décisions restent exclusivement calculées côté serveur ;
- les nouveaux artefacts persistés ont un hash canonique, des triggers d'immutabilité
  et un événement d'audit append-only ;
- `docs/openapi.yaml`, le schéma PostgreSQL et la documentation française sont à jour.

## Style des changements

- Python 3.10+ sans syntaxe réservée à une version plus récente.
- Fonctions déterministes séparées du stockage lorsque possible.
- Listes et objets triés avant hash afin de garantir la reproductibilité.
- Erreurs métier explicites (`ValidationError`, `NotFoundError`, `ConflictError`).
- Aucun faux `PASS`, aucune provenance inventée et aucune certification implicite.

## Ajouter une migration cumulative

Le schéma SQLite utilise `CREATE TABLE IF NOT EXISTS` afin qu'une base d'une version
antérieure puisse être ouverte cumulativement. Ajoutez la table, ses index, ses clés
étrangères et ses triggers sans supprimer les capacités précédentes. Reproduisez les
invariants dans `docs/postgresql-schema.sql`.

## Sécurité

N'incluez jamais de secret, token, fichier `.env`, base locale ou donnée personnelle
dans un commit. Pour signaler une vulnérabilité, contactez le mainteneur en privé au
lieu de publier immédiatement les détails exploitables dans une issue.
