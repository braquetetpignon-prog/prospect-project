# Déploiement automatique en pré-production (Coolify + VPS)

## Dépôt Git : GitHub

Recommandé par défaut : GitHub. C'est l'option la plus simple avec Coolify — l'app GitHub officielle configure le webhook automatiquement (pas de token à coller à la main comme avec GitLab auto-hébergé), et c'est le service le plus documenté pour ce cas d'usage. Dépôt privé gratuit, aucune limite pour un usage personnel/pro modeste.

## Stack fournie

- `Dockerfile` — build Python 3.12 + gunicorn
- `app/main.py` — app Flask minimale avec SQLite (adaptable à Django/FastAPI)
- `requirements.txt` — dépendances
- `docker-compose.yml` — service + volume persistant pour le fichier SQLite
- `.env.example` — variables d'environnement à copier en `.env`

Le fichier SQLite est stocké dans un volume Docker nommé (`app_data`), donc il survit aux redéploiements. Pour la prod à plus fort trafic, migrer vers PostgreSQL (Coolify propose PostgreSQL en un clic) — SQLite convient pour une pré-prod ou un usage à faible concurrence.

## Mise en place (une fois)

1. Créer un dépôt GitHub, y pousser ces fichiers.
2. Installer Coolify sur le VPS (script officiel en une commande, cf. coolify.io/docs).
3. Dans Coolify : Sources → connecter GitHub (installation de la GitHub App Coolify sur le dépôt).
4. New Resource → Application → choisir le dépôt et la branche (ex. `preprod`).
5. Coolify détecte le `Dockerfile` automatiquement. Renseigner les variables d'environnement depuis `.env.example`.
6. Ajouter un volume persistant mappé sur `/data` (pour le fichier SQLite) si non repris du `docker-compose.yml`.
7. Domaine + SSL Let's Encrypt : automatique via Coolify.

## Déploiement automatique

Coolify active par défaut un webhook GitHub : chaque `git push` sur la branche configurée déclenche build + déploiement, sans action manuelle. Rien à ajouter côté GitHub Actions — Coolify gère tout le pipeline.

## Séparer pré-prod et prod

Deux approches, au choix :

- **Deux branches** (`develop` → pré-prod, `main` → prod) avec deux "Applications" Coolify distinctes pointant sur le même dépôt, chacune avec ses propres variables d'environnement et son propre domaine (ex. `preprod.mondomaine.com` / `mondomaine.com`).
- **Preview Deployments** de Coolify : déploiement éphémère automatique par Pull Request, utile pour valider avant merge.

Pour une ferme de plusieurs apps, répéter "New Resource → Application" par app sur la même instance Coolify — un seul VPS peut héberger plusieurs apps + bases indépendamment, avec un coût VPS qui reste fixe.
