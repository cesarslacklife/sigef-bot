#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_sigef.py - Sincronização semanal da base SIGEF (roda na SUA MÁQUINA local)
================================================================================
Por que local? O Acervo Fundiário bloqueia o IP da VPS (Hetzner/Alemanha).
Sua internet residencial brasileira acessa normalmente.

O que faz:
  1. Baixa o ZIP do shapefile "Sigef Privado" da UF configurada
  2. Lê o shapefile e normaliza os atributos
  3. Carrega numa tabela staging do Postgres da VPS
  4. Troca staging -> tabela oficial em transação única (bot nunca vê base vazia)
  5. Notifica seu WhatsApp via webhook do n8n (sucesso ou falha)

Instalação (uma vez, no Windows):
  py -m pip install geopandas pyogrio sqlalchemy psycopg2-binary requests python-dotenv

Agendamento: Agendador de Tarefas do Windows -> ver README seção 5.
"""

import io
import os
import sys
import zipfile
import tempfile
import traceback
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ----------------------------- Configuração ---------------------------------
UF = os.getenv("SYNC_UF", "RJ").upper()

# URL do ZIP no Acervo/Certificação. Confirme o link atual na página
# "Download de Shapefiles" do Acervo Fundiário e ajuste no .env se mudar.
URL_ZIP = os.getenv(
    "SYNC_URL_ZIP",
    f"https://certificacao.incra.gov.br/csv_shp/zip/Sigef%20Privado_{UF}.zip",
)

PG_DSN = os.getenv("PG_DSN")  # ex: postgresql://user:senha@IP_DA_VPS:5432/sigef
WEBHOOK_NOTIFY = os.getenv("WEBHOOK_NOTIFY", "")  # webhook n8n p/ aviso no WhatsApp
MIN_FEATURES = int(os.getenv("SYNC_MIN_FEATURES", "1000"))  # trava de segurança

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Mapeia nomes de colunas do shapefile -> colunas da tabela.
# Os shapefiles do INCRA costumam usar estes nomes (limite de 10 chars do DBF).
COL_MAP_CANDIDATES = {
    "parcela_codigo":     ["parcela_co", "parcela_cod", "codigo_par", "parcela"],
    "codigo_imovel":      ["codigo_imo", "cod_imovel"],
    "nome_area":          ["nome_area", "nome", "imovel"],
    "rt":                 ["rt"],
    "art":                ["art"],
    "situacao":           ["situacao_i", "situacao"],
    "status":             ["status"],
    "registro_cns":       ["registro_c", "cns"],
    "registro_matricula": ["registro_m", "matricula"],
    "data_submissao":     ["data_submi"],
    "data_aprovacao":     ["data_aprov"],
    "municipio":          ["municipio_", "municipio", "nome_munic"],
}


def log(msg: str):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notificar(texto: str):
    if not WEBHOOK_NOTIFY:
        return
    try:
        requests.post(WEBHOOK_NOTIFY, json={"texto": texto}, timeout=15)
    except Exception as e:
        log(f"Falha ao notificar webhook: {e}")


def baixar_zip(url: str, destino: str) -> str:
    log(f"Baixando {url}")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=900, stream=True)
    r.raise_for_status()
    total = 0
    with open(destino, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            f.write(chunk)
            total += len(chunk)
    log(f"Download concluído: {total/1e6:.1f} MB")
    if total < 50_000:
        raise RuntimeError("Arquivo suspeito de erro (muito pequeno). Verifique a URL.")
    return destino


def achar_shp(pasta: str) -> str:
    for raiz, _, arquivos in os.walk(pasta):
        for a in arquivos:
            if a.lower().endswith(".shp"):
                return os.path.join(raiz, a)
    raise FileNotFoundError("Nenhum .shp encontrado dentro do ZIP.")


def normalizar(gdf):
    import pandas as pd

    cols_lower = {c.lower(): c for c in gdf.columns}
    out = {}
    for destino, candidatos in COL_MAP_CANDIDATES.items():
        col = next((cols_lower[c] for c in candidatos if c in cols_lower), None)
        out[destino] = gdf[col] if col is not None else None

    import geopandas as gpd
    novo = gpd.GeoDataFrame(geometry=gdf.geometry)
    for k, v in out.items():
        novo[k] = v if v is not None else None

    # Datas
    for c in ("data_submissao", "data_aprovacao"):
        if novo[c] is not None:
            novo[c] = pd.to_datetime(novo[c], errors="coerce", dayfirst=True).dt.date

    # Área em hectares: usa coluna se existir; senão calcula da geometria
    col_area = next((cols_lower[c] for c in ("area_ha", "area_hecta", "area")
                     if c in cols_lower), None)
    if col_area:
        novo["area_ha"] = pd.to_numeric(gdf[col_area], errors="coerce")
    else:
        novo["area_ha"] = gdf.geometry.to_crs(5880).area / 10_000  # Polyconic BR

    novo["uf"] = UF

    # CRS -> SIRGAS 2000 geográfico
    if novo.crs is None:
        novo = novo.set_crs(4674)
    else:
        novo = novo.to_crs(4674)

    # Geometria sempre MultiPolygon
    from shapely.geometry import MultiPolygon, Polygon
    novo["geometry"] = novo.geometry.apply(
        lambda g: MultiPolygon([g]) if isinstance(g, Polygon) else g
    )

    # Remove linhas sem código de parcela válido
    novo = novo[novo["parcela_codigo"].notna()].copy()
    novo["parcela_codigo"] = novo["parcela_codigo"].astype(str).str.strip().str.lower()
    novo = novo[novo["parcela_codigo"].str.len() == 36]
    return novo


def main():
    import geopandas as gpd
    from sqlalchemy import create_engine, text

    if not PG_DSN:
        raise RuntimeError("Defina PG_DSN no arquivo .env")

    engine = create_engine(PG_DSN)

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "sigef.zip")
        baixar_zip(URL_ZIP, zip_path)

        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        shp = achar_shp(tmp)
        log(f"Lendo {os.path.basename(shp)}")

        gdf = gpd.read_file(shp)
        log(f"{len(gdf)} feições lidas. Colunas: {list(gdf.columns)}")
        gdf = normalizar(gdf)
        log(f"{len(gdf)} parcelas válidas após normalização")

        if len(gdf) < MIN_FEATURES:
            raise RuntimeError(
                f"Apenas {len(gdf)} parcelas (mínimo {MIN_FEATURES}). "
                "Abortando para não corromper a base."
            )

        log("Carregando staging no Postgres da VPS...")
        gdf.to_postgis("sigef_parcelas_staging", engine,
                       if_exists="replace", index=False)

        with engine.begin() as conn:
            antes = conn.execute(
                text("SELECT count(*) FROM sigef_parcelas WHERE uf = :uf"),
                {"uf": UF},
            ).scalar() or 0

            conn.execute(text("""
                DELETE FROM sigef_parcelas WHERE uf = :uf
            """), {"uf": UF})

            conn.execute(text("""
                INSERT INTO sigef_parcelas
                    (parcela_codigo, codigo_imovel, nome_area, rt, art,
                     situacao, status, registro_cns, registro_matricula,
                     data_submissao, data_aprovacao, area_ha, municipio, uf, geom)
                SELECT parcela_codigo::uuid, codigo_imovel, nome_area, rt, art,
                       situacao, status, registro_cns, registro_matricula,
                       data_submissao, data_aprovacao, area_ha, municipio, uf,
                       ST_Multi(ST_SetSRID(geometry, 4674))
                FROM sigef_parcelas_staging
                ON CONFLICT (parcela_codigo) DO NOTHING
            """))

            depois = conn.execute(
                text("SELECT count(*) FROM sigef_parcelas WHERE uf = :uf"),
                {"uf": UF},
            ).scalar()

            conn.execute(text("""
                INSERT INTO sigef_sync_log (uf, qtd_parcelas, qtd_novas, sucesso, mensagem)
                VALUES (:uf, :qtd, :novas, true, 'sync ok')
            """), {"uf": UF, "qtd": depois, "novas": max(depois - antes, 0)})

            conn.execute(text("DROP TABLE IF EXISTS sigef_parcelas_staging"))

        msg = (f"✅ Base SIGEF-{UF} atualizada: {depois} parcelas "
               f"({max(depois - antes, 0):+d} em relação à anterior).")
        log(msg)
        notificar(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        erro = f"❌ Sync SIGEF falhou: {e}"
        log(erro)
        log(traceback.format_exc())
        notificar(erro + "\n(A base anterior continua no ar, o bot não foi afetado.)")
        sys.exit(1)
