# -*- coding: utf-8 -*-
"""Consultas ao espelho PostGIS."""

import os
import json
import psycopg2
import psycopg2.extras

PG_DSN = os.getenv("PG_DSN", "postgresql://sigef_bot:senha@postgres:5432/sigef")

CAMPOS = """parcela_codigo::text, codigo_imovel, nome_area, rt, art, situacao,
            status, registro_cns, registro_matricula,
            to_char(data_submissao,'DD/MM/YYYY')  AS data_submissao,
            to_char(data_aprovacao,'DD/MM/YYYY')  AS data_aprovacao,
            round(area_ha::numeric, 4)            AS area_ha,
            municipio, uf,
            ST_AsGeoJSON(geom)                    AS geojson"""


def _conn():
    return psycopg2.connect(PG_DSN)


def _rows(sql, params):
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        out = [dict(r) for r in cur.fetchall()]
    for r in out:
        if r.get("geojson"):
            r["geojson"] = json.loads(r["geojson"])
        if r.get("area_ha") is not None:
            r["area_ha"] = float(r["area_ha"])
    return out


def por_codigo(uuid: str):
    r = _rows(f"SELECT {CAMPOS} FROM sigef_parcelas WHERE parcela_codigo = %s",
              (uuid,))
    return r[0] if r else None


def por_sncr(codigo: str):
    """Busca por código do imóvel no SNCR/CCIR (codigo_imovel).
    Aceita com ou sem pontuação — compara só os dígitos."""
    so_digitos = "".join(c for c in codigo if c.isdigit())
    return _rows(
        f"""SELECT {CAMPOS} FROM sigef_parcelas
            WHERE regexp_replace(codigo_imovel, '[^0-9]', '', 'g') = %s
            ORDER BY area_ha LIMIT 10""",
        (so_digitos,),
    )


def por_ponto(lat: float, lon: float):
    return _rows(
        f"""SELECT {CAMPOS} FROM sigef_parcelas
            WHERE ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4674))
            ORDER BY area_ha LIMIT 5""",
        (lon, lat),
    )


def proximas(lat: float, lon: float, raio_m: float = 500):
    """Fallback: parcelas num raio, quando o ponto não cai dentro de nenhuma."""
    return _rows(
        f"""SELECT {CAMPOS},
                   round(ST_Distance(geom::geography,
                         ST_SetSRID(ST_MakePoint(%s, %s),4674)::geography)) AS dist_m
            FROM sigef_parcelas
            WHERE ST_DWithin(geom::geography,
                  ST_SetSRID(ST_MakePoint(%s, %s),4674)::geography, %s)
            ORDER BY dist_m LIMIT 3""",
        (lon, lat, lon, lat, raio_m),
    )


def total_parcelas():
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM sigef_parcelas")
        return cur.fetchone()[0]
