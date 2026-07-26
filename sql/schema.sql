-- ============================================================
-- SIGEF Bot - Espelho local de parcelas certificadas (INCRA)
-- Rodar uma única vez no Postgres da VPS:
--   psql -U postgres -d sigef -f schema.sql
-- (crie o banco antes: CREATE DATABASE sigef;)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- Tabela oficial consultada pelo bot
CREATE TABLE IF NOT EXISTS sigef_parcelas (
    id                 SERIAL PRIMARY KEY,
    parcela_codigo     UUID UNIQUE,
    codigo_imovel      TEXT,
    nome_area          TEXT,
    rt                 TEXT,
    art                TEXT,
    situacao           TEXT,
    status             TEXT,
    registro_cns       TEXT,
    registro_matricula TEXT,
    data_submissao     DATE,
    data_aprovacao     DATE,
    area_ha            NUMERIC,
    municipio          TEXT,
    uf                 CHAR(2) DEFAULT 'RJ',
    atualizado_em      TIMESTAMPTZ DEFAULT now(),
    geom               geometry(MultiPolygon, 4674)   -- SIRGAS 2000
);

CREATE INDEX IF NOT EXISTS idx_sigef_geom   ON sigef_parcelas USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_sigef_codigo ON sigef_parcelas (parcela_codigo);
CREATE INDEX IF NOT EXISTS idx_sigef_nome   ON sigef_parcelas (lower(nome_area));
CREATE INDEX IF NOT EXISTS idx_sigef_matric ON sigef_parcelas (registro_matricula);

-- Tabela de staging usada pelo sync semanal (o script recria à vontade)
-- A troca staging -> oficial é feita pelo sync em transação única.

-- Log de sincronizações (auditoria simples)
CREATE TABLE IF NOT EXISTS sigef_sync_log (
    id           SERIAL PRIMARY KEY,
    executado_em TIMESTAMPTZ DEFAULT now(),
    uf           CHAR(2),
    qtd_parcelas INTEGER,
    qtd_novas    INTEGER,
    sucesso      BOOLEAN,
    mensagem     TEXT
);

-- Usuário dedicado pro bot (ajuste a senha!)
-- CREATE USER sigef_bot WITH PASSWORD 'TROQUE_ESTA_SENHA';
-- GRANT SELECT ON sigef_parcelas TO sigef_bot;
-- GRANT SELECT, INSERT ON sigef_sync_log TO sigef_bot;
