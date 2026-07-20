FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Coolify injecte automatiquement SOURCE_COMMIT comme build arg (à activer
# via "Include Source Commit in Build" dans le menu Avancé de chaque
# application Coolify — préprod ET prod). Sans ça, aucun moyen fiable de
# savoir quel code tourne réellement dans un conteneur donné : c'est ce qui
# a produit l'incident du fix modal "vu comme mergé sur GitHub mais pas
# réellement servi en prod" (voir contexte v5, section 3). GIT_COMMIT est
# exposé en clair via /version pour vérification en un clic après chaque
# redeploy, plutôt que de deviner à partir du code source seul.
ARG SOURCE_COMMIT=unknown
ENV GIT_COMMIT=$SOURCE_COMMIT

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["gunicorn", "-b", "0.0.0.0:3000", "--workers", "3", "--timeout", "60", "--preload", "app.main:app"]
