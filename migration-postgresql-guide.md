# Migration SQLite → PostgreSQL — guide de mise en œuvre

## Constat important

En regardant le dépôt (`braquetetpignon-prog/prospect-project`, public, je l'ai cloné directement), `app/main.py` est encore le scaffold de base : une seule table `visits` qui sert de compteur de démonstration. Il n'y a donc **aucune vraie donnée à migrer**. C'est le bon moment pour basculer, comme prévu.

Conséquence : pas de script de migration de données nécessaire. On bascule le moteur de base et on pose directement le schéma cible en PostgreSQL.

## Ce qui a été fait et testé

J'ai installé PostgreSQL localement, exécuté le schéma, et fait tourner l'app Flask modifiée dessus (route `/` et `/health`) pour vérifier que tout fonctionne avant de te livrer les fichiers — y compris que l'initialisation du schéma est idempotente (rejouable sans erreur), ce qui compte avec plusieurs workers gunicorn qui démarrent en parallèle.

## Fichiers livrés

- **`schema.sql`** — schéma PostgreSQL cible : `workspaces`, `users` (minimal, à affiner quand l'authentification sera spécifiée), `prospects`, `ia_search_log` (quota recherche IA), `smtp_configs`, `google_business_profiles`, `campaigns`, `campaign_sends`, `consents`. Couvre ce qu'on a décidé pour la migration de base, la recherche IA et le module Avis Prospect.
- **`main.py`** — remplace `app/main.py`. Se connecte via `DATABASE_URL` (psycopg2), charge `schema.sql` au démarrage, et le `/health` vérifie maintenant la connexion DB en plus de répondre. La table `visits` de démo a été retirée (elle ne servait qu'au scaffold initial) — dis-moi si tu veux la garder pour une raison précise.
- **`requirements.txt`** — ajout de `psycopg2-binary==2.9.9`.
- **`docker-compose.yml`** — mis à jour pour référence (non utilisé par Coolify) : ajoute un service `db` PostgreSQL local, remplace `DB_PATH` par `DATABASE_URL`.

## À faire de ton côté

Je n'ai pas les accès pour pousser sur ton dépôt GitHub ni pour modifier Coolify — voici les étapes :

**1. Dans le dépôt GitHub**
- Remplacer `app/main.py` par le `main.py` fourni.
- Placer `schema.sql` à côté, dans `app/schema.sql`.
- Remplacer `requirements.txt`.
- Remplacer `docker-compose.yml` (optionnel, gardé en référence uniquement).
- Commit + push sur `main` → le webhook Coolify déclenchera un build automatique.

**2. Dans Coolify**
- Ajouter une nouvelle ressource **PostgreSQL** (Resource → Database → PostgreSQL) sur le même projet, avec un volume dédié.
- Récupérer l'URL de connexion générée par Coolify pour ce service.
- Dans les variables d'environnement de l'app Flask : supprimer `DB_PATH`, ajouter `DATABASE_URL` avec cette URL. Garder `ENV=preproduction` (attention à la casse, comme convenu).
- Le volume `/data` (utilisé pour le fichier SQLite) n'est plus nécessaire — tu peux le retirer une fois la bascule confirmée.
- Redéployer.

**3. Vérification**
- `GET /health` doit répondre `{"status": "healthy", "db": "ok"}`. Si la connexion échoue, il répond `503` avec le détail de l'erreur — utile pour diagnostiquer une mauvaise `DATABASE_URL`.

## Points laissés volontairement de côté à ce stade

- **Authentification** : la table `users` est minimale (email + hash de mot de passe). On n'a pas encore parlé du système de connexion — à spécifier avant de la finaliser.
- **Chiffrement des identifiants SMTP** : le champ `password_encrypted` existe dans le schéma, mais le chiffrement/déchiffrement côté application n'est pas encore implémenté — ça viendra avec la construction du module Avis Prospect (Option 3), pas dans cette étape de migration.
