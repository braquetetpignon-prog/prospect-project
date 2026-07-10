-- ClickProspect — schéma PostgreSQL
-- Reprend le multi-utilisateurs par espace de travail + les tables nécessaires
-- aux Options 2 (recherche IA) et 3 (module Avis Prospect).

-- Espaces de travail (un par client ClickProspect)
CREATE TABLE IF NOT EXISTS workspaces (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Utilisateurs rattachés à un espace de travail
-- (schéma minimal — à affiner quand l'authentification sera spécifiée)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Prospects
CREATE TABLE IF NOT EXISTS prospects (
    id SERIAL PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    nom_entreprise TEXT NOT NULL,
    siren TEXT,
    siret TEXT,
    naf_code TEXT,
    adresse TEXT,
    code_postal TEXT,
    ville TEXT,
    telephone TEXT,
    email TEXT,
    site_web TEXT,
    statut TEXT NOT NULL DEFAULT 'nouveau',   -- nouveau / qualifie / client / recale
    source TEXT,                              -- manuel / sirene / recherche_ia / import_csv
    motif_recalage TEXT,                      -- ex: "liquidation judiciaire (BODACC)"
    recale_at TIMESTAMPTZ,                    -- déclenche le compte à rebours d'1 semaine avant purge
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prospects_workspace ON prospects(workspace_id);
CREATE INDEX IF NOT EXISTS idx_prospects_statut ON prospects(statut);
CREATE INDEX IF NOT EXISTS idx_prospects_siren ON prospects(siren);

-- Historique des lancements de recherche IA (pour appliquer le quota de 3/jour/espace)
CREATE TABLE IF NOT EXISTS ia_search_log (
    id SERIAL PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    prospect_id INTEGER REFERENCES prospects(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ia_search_log_workspace_date ON ia_search_log(workspace_id, created_at);

-- Configuration SMTP par espace de travail (identifiants chiffrés côté application avant écriture)
CREATE TABLE IF NOT EXISTS smtp_configs (
    workspace_id INTEGER PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT NOT NULL,
    password_encrypted TEXT NOT NULL,
    from_email TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fiche Google Business Profile par espace de travail
CREATE TABLE IF NOT EXISTS google_business_profiles (
    workspace_id INTEGER PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    profile_url TEXT NOT NULL
);

-- Campagnes (avis / publicitaire / newsletter) — 10 actives max par espace, contrôlé côté app
CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    type TEXT NOT NULL,                       -- avis / publicitaire / newsletter
    nom TEXT NOT NULL,
    sujet TEXT,
    contenu TEXT,
    quota_par_jour INTEGER DEFAULT 100,
    statut TEXT NOT NULL DEFAULT 'active',     -- active / inactive
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_campaigns_workspace ON campaigns(workspace_id);

-- Envois (email uniquement au lancement — colonne canal prévue pour le SMS futur)
CREATE TABLE IF NOT EXISTS campaign_sends (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    canal TEXT NOT NULL DEFAULT 'email',       -- email / sms (réservé, pas encore actif)
    statut TEXT NOT NULL DEFAULT 'planifie',   -- planifie / envoye / echec
    planifie_pour TIMESTAMPTZ,
    envoye_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_campaign_sends_campaign ON campaign_sends(campaign_id);

-- Consentements RGPD (opt-in / opt-out / intérêt légitime), par prospect et par type de campagne
CREATE TABLE IF NOT EXISTS consents (
    id SERIAL PRIMARY KEY,
    prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    type TEXT NOT NULL,                        -- avis / publicitaire / newsletter
    statut TEXT NOT NULL,                       -- opt_in / opt_out / interet_legitime
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_consents_prospect ON consents(prospect_id);
