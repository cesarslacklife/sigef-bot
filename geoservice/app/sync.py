# -*- coding: utf-8 -*-
"""Recebe o ZIP do Acervo Fundiario (enviado pelo PC) e atualiza o espelho.

O PC so faz o download e o upload; toda a leitura do shapefile e a carga
no PostGIS acontecem aqui. Se um dia o Acervo liberar o IP da VPS, basta
chamar processar_zip() a partir de um cron local e aposentar o PC.

A funcao normalizar() e a mesma do sync_sigef.py que rodava no Windows.
"""

import os
import glob
import hmac
import zipfile
import tempfile
import traceback
from datetime import datetime, timezone

import pandas as pd
import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import create_engine, text
from fastapi import APIRouter, UploadFile, File, Header, HTTPException

PG_DSN = os.getenv("PG_DSN", "postgresql://sigef:senha@postgis:5432/sigef")
SYNC_UF = os.getenv("SYNC_UF", "RJ")
MIN_FEATURES = int(os.getenv("SYNC_MIN_FEATURES", "1000"))
MAX_ZIP_MB = int(os.getenv("SYNC_MAX_ZIP_MB", "200"))

router = APIRouter()


def _token_esperado() -> str:
    """Le o token do Docker secret; cai pra variavel de ambiente se nao houver."""
    caminho = os.getenv("SYNC_TOKEN_FILE", "/run/secrets/sync_token")
    if os.path.exists(caminho):
        with open(caminho) as f:
            return f.read().strip()
    return os.getenv("SYNC_TOKEN", "")


def _conferir_token(recebido: str | None) -> None:
    esperado = _token_esperado()
    if not esperado:
        raise HTTPException(500, "SYNC_TOKEN nao configurado no servidor")
    if not recebido or not hmac.compare_digest(recebido, esperado):
        raise HTTPException(401, "token invalido")


# ---------------------------------------------------------------- normalizacao

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


def normalizar(gdf):
    cols_lower = {c.lower(): c for c in gdf.columns}
    out = {}
    for destino, candidatos in COL_MAP_CANDIDATES.items():
        col = next((cols_lower[c] for c in candidatos if c in cols_lower), None)
        out[destino] = gdf[col] if col is not None else None

    novo = gpd.GeoDataFrame(geometry=gdf.geometry)
    for k, v in out.items():
        novo[k] = v if v is not None else None

    # Datas
    for c in ("data_submissao", "data_aprovacao"):
        if novo[c] is not None:
            novo[c] = pd.to_datetime(novo[c], errors="coerce", dayfirst=True).dt.date

    # Area em hectares: usa coluna se existir; senao calcula da geometria
    col_area = next((cols_lower[c] for c in ("area_ha", "area_hecta", "area")
                     if c in cols_lower), None)
    if col_area:
        novo["area_ha"] = pd.to_numeric(gdf[col_area], errors="coerce")
    else:
        novo["area_ha"] = gdf.geometry.to_crs(5880).area / 10_000  # Polyconic BR

    novo["uf"] = SYNC_UF

    # CRS -> SIRGAS 2000 geografico
    if novo.crs is None:
        novo = novo.set_crs(4674)
    else:
        novo = novo.to_crs(4674)

    # Geometria sempre MultiPolygon
    novo["geometry"] = novo.geometry.apply(
        lambda g: MultiPolygon([g]) if isinstance(g, Polygon) else g
    )

    # Remove linhas sem codigo de parcela valido
    novo = novo[novo["parcela_codigo"].notna()].copy()
    novo["parcela_codigo"] = novo["parcela_codigo"].astype(str).str.strip().str.lower()
    novo = novo[novo["parcela_codigo"].str.len() == 36]
    return novo


def _achar_shp(pasta: str) -> str:
    achados = glob.glob(os.path.join(pasta, "**", "*.shp"), recursive=True)
    if not achados:
        raise RuntimeError("nenhum .shp dentro do ZIP")
    return achados[0]


# ---------------------------------------------------------------- carga

SQL_INSERT = """
    INSERT INTO sigef_parcelas
        (parcela_codigo, codigo_imovel, nome_area, rt, art,
         situacao, status, registro_cns, registro_matricula,
         data_submissao, data_aprovacao, area_ha, municipio, uf, geom)
    SELECT parcela_codigo::uuid, codigo_imovel, nome_area, rt, art,
           situacao, status, registro_cns, registro_matricula,
           data_submissao, data_aprovacao, area_ha, municipio, uf,
           ST_Force2D(ST_Multi(ST_SetSRID(geometry, 4674)))
    FROM sigef_parcelas_staging
    ON CONFLICT (parcela_codigo) DO NOTHING
"""


def processar_zip(zip_path: str) -> dict:
    """Le o ZIP, valida e faz o swap da base. Devolve o resumo do sync."""
    engine = create_engine(PG_DSN)
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)

        gdf = gpd.read_file(_achar_shp(tmp))
        lidas = len(gdf)
        gdf = normalizar(gdf)

        if len(gdf) < MIN_FEATURES:
            raise RuntimeError(
                f"apenas {len(gdf)} parcelas (minimo {MIN_FEATURES}); "
                "abortado para nao corromper a base"
            )

        gdf.to_postgis("sigef_parcelas_staging", engine,
                       if_exists="replace", index=False)

        with engine.begin() as conn:
            antes = conn.execute(
                text("SELECT count(*) FROM sigef_parcelas WHERE uf = :uf"),
                {"uf": SYNC_UF}).scalar() or 0
            conn.execute(text("DELETE FROM sigef_parcelas WHERE uf = :uf"),
                         {"uf": SYNC_UF})
            conn.execute(text(SQL_INSERT))
            depois = conn.execute(
                text("SELECT count(*) FROM sigef_parcelas WHERE uf = :uf"),
                {"uf": SYNC_UF}).scalar()
            novas = max(depois - antes, 0)
            conn.execute(text("""
                INSERT INTO sigef_sync_log (uf, qtd_parcelas, qtd_novas, sucesso, mensagem)
                VALUES (:uf, :qtd, :novas, true, 'sync ok (upload)')
            """), {"uf": SYNC_UF, "qtd": depois, "novas": novas})
            conn.execute(text("DROP TABLE IF EXISTS sigef_parcelas_staging"))

    return {"ok": True, "uf": SYNC_UF, "lidas": lidas, "validas": len(gdf),
            "antes": antes, "depois": depois, "novas": novas,
            "quando": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------- endpoint

@router.post("/sync/upload")
async def sync_upload(arquivo: UploadFile = File(...),
                      x_sync_token: str | None = Header(default=None)):
    _conferir_token(x_sync_token)

    if not (arquivo.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "envie um arquivo .zip")

    limite = MAX_ZIP_MB * 1024 * 1024
    tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        tamanho = 0
        while pedaco := await arquivo.read(1024 * 1024):
            tamanho += len(pedaco)
            if tamanho > limite:
                raise HTTPException(413, f"ZIP acima de {MAX_ZIP_MB} MB")
            tmp_zip.write(pedaco)
        tmp_zip.close()

        if not zipfile.is_zipfile(tmp_zip.name):
            raise HTTPException(400, "arquivo nao e um ZIP valido")

        resultado = processar_zip(tmp_zip.name)
        resultado["recebidos_mb"] = round(tamanho / 1024 / 1024, 1)
        return resultado

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        try:
            with create_engine(PG_DSN).begin() as conn:
                conn.execute(text("""
                    INSERT INTO sigef_sync_log (uf, qtd_parcelas, qtd_novas, sucesso, mensagem)
                    VALUES (:uf, 0, 0, false, :msg)
                """), {"uf": SYNC_UF, "msg": f"falha no upload: {e}"[:500]})
        except Exception:
            pass
        raise HTTPException(500, f"falha ao processar: {e}")
    finally:
        if os.path.exists(tmp_zip.name):
            os.unlink(tmp_zip.name)
