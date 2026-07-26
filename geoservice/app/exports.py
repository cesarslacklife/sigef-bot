# -*- coding: utf-8 -*-
"""Geração local de arquivos a partir da geometria do espelho PostGIS:
PNG sobre satélite, KML, SHP, GeoJSON e TXT de vértices em UTM."""

import os
import io
import json
import zipfile
import unicodedata

from shapely.geometry import shape, mapping
from pyproj import Transformer

from .parsing import epsg_utm_sirgas

FILES_DIR = os.getenv("FILES_DIR", "/data/files")
os.makedirs(FILES_DIR, exist_ok=True)


def _slug(nome: str) -> str:
    s = unicodedata.normalize("NFKD", nome or "parcela")
    s = s.encode("ascii", "ignore").decode()
    s = "".join(c if c.isalnum() else "_" for c in s).strip("_")
    return (s or "parcela")[:40]


def _caminho(parcela, ext):
    nome = f"{_slug(parcela.get('nome_area'))}_{parcela['parcela_codigo'][:8]}.{ext}"
    return os.path.join(FILES_DIR, nome), nome


# ----------------------------- PNG sobre satélite ----------------------------

def gerar_png(parcela) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import geopandas as gpd
    import contextily as cx

    geom = shape(parcela["geojson"])
    gdf = gpd.GeoDataFrame(geometry=[geom], crs=4674).to_crs(3857)

    fig, ax = plt.subplots(figsize=(8, 8), dpi=130)
    gdf.boundary.plot(ax=ax, color="#FFD500", linewidth=2.2)
    gdf.plot(ax=ax, facecolor="#FFD500", alpha=0.18, edgecolor="none")

    # enquadramento QUADRADO centrado no polígono (mapa preenche o quadro)
    minx, miny, maxx, maxy = gdf.total_bounds
    ccx, ccy = (minx + maxx) / 2, (miny + maxy) / 2
    half = max(maxx - minx, maxy - miny) * 0.675 or 200
    ax.set_xlim(ccx - half, ccx + half)
    ax.set_ylim(ccy - half, ccy + half)

    cx.add_basemap(ax, source=cx.providers.Esri.WorldImagery, attribution=False)
    ax.set_axis_off()
    titulo = f"{parcela.get('nome_area') or 'Parcela'} — {parcela.get('area_ha')} ha"
    ax.set_title(titulo, fontsize=11)
    ax.text(0.99, 0.015, "Imagem: Esri World Imagery • Limites: SIGEF/INCRA",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=6,
            color="white", bbox=dict(facecolor="black", alpha=0.5, pad=2))

    caminho, nome = _caminho(parcela, "png")
    fig.savefig(caminho, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    return nome


def gerar_png_multiplas(parcelas) -> str:
    """Desenha várias parcelas num único mapa de satélite, numeradas de 1 a N,
    pra pessoa escolher visualmente qual quer. Retorna o nome do PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import geopandas as gpd
    import contextily as cx

    geoms = [shape(p["geojson"]) for p in parcelas]
    gdf = gpd.GeoDataFrame(geometry=geoms, crs=4674).to_crs(3857)

    fig, ax = plt.subplots(figsize=(8, 8), dpi=130)
    cores = ["#FFD500", "#FF5A5A", "#3CA0C8", "#28A078", "#C828C8",
             "#F0A028", "#8050F0", "#50C878"]
    for i, (_, row) in enumerate(gdf.iterrows()):
        cor = cores[i % len(cores)]
        sub = gpd.GeoDataFrame(geometry=[row.geometry], crs=3857)
        sub.boundary.plot(ax=ax, color=cor, linewidth=2.4)
        sub.plot(ax=ax, facecolor=cor, alpha=0.20, edgecolor="none")
        # número no centro da parcela
        c = row.geometry.centroid
        ax.annotate(str(i + 1), (c.x, c.y), color="white", fontsize=14,
                    fontweight="bold", ha="center", va="center",
                    bbox=dict(boxstyle="circle", facecolor=cor, edgecolor="white"))

    minx, miny, maxx, maxy = gdf.total_bounds
    ccx, ccy = (minx + maxx) / 2, (miny + maxy) / 2
    half = max(maxx - minx, maxy - miny) * 0.60 or 200
    ax.set_xlim(ccx - half, ccx + half)
    ax.set_ylim(ccy - half, ccy + half)

    cx.add_basemap(ax, source=cx.providers.Esri.WorldImagery, attribution=False)
    ax.set_axis_off()
    ax.set_title(f"{len(parcelas)} parcelas encontradas — escolha pelo número",
                 fontsize=11)
    ax.text(0.99, 0.015, "Imagem: Esri World Imagery • Limites: SIGEF/INCRA",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=6,
            color="white", bbox=dict(facecolor="black", alpha=0.5, pad=2))

    nome = f"multi_{parcelas[0]['parcela_codigo'][:8]}_{len(parcelas)}.png"
    fig.savefig(os.path.join(FILES_DIR, nome), bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    return nome

def gerar_geojson(parcela) -> str:
    caminho, nome = _caminho(parcela, "geojson")
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {k: parcela.get(k) for k in
                       ("parcela_codigo", "nome_area", "area_ha", "status",
                        "registro_matricula", "municipio", "uf")},
        "geometry": parcela["geojson"],
    }]}
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)
    return nome


def gerar_kml(parcela) -> str:
    geom = shape(parcela["geojson"])
    caminho, nome = _caminho(parcela, "kml")

    def anel(coords):
        return " ".join(f"{x:.8f},{y:.8f},0" for x, y in coords)

    poligonos = []
    geoms = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    for g in geoms:
        interiores = "".join(
            f"<innerBoundaryIs><LinearRing><coordinates>{anel(r.coords)}"
            f"</coordinates></LinearRing></innerBoundaryIs>"
            for r in g.interiors
        )
        poligonos.append(
            f"<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            f"{anel(g.exterior.coords)}</coordinates></LinearRing>"
            f"</outerBoundaryIs>{interiores}</Polygon>"
        )
    corpo = (f"<MultiGeometry>{''.join(poligonos)}</MultiGeometry>"
             if len(poligonos) > 1 else poligonos[0])

    nome_area = (parcela.get("nome_area") or "Parcela").replace("&", "e")
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<name>{nome_area}</name>
<Style id="p"><LineStyle><color>ff00d5ff</color><width>2.5</width></LineStyle>
<PolyStyle><color>3300d5ff</color></PolyStyle></Style>
<Placemark><name>{nome_area} - {parcela.get('area_ha')} ha</name>
<description>Parcela SIGEF {parcela['parcela_codigo']}
Matricula: {parcela.get('registro_matricula') or '-'}
Status: {parcela.get('status') or '-'}</description>
<styleUrl>#p</styleUrl>{corpo}</Placemark>
</Document></kml>"""
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(kml)
    return nome


def gerar_shp(parcela) -> str:
    import geopandas as gpd
    geom = shape(parcela["geojson"])
    gdf = gpd.GeoDataFrame(
        {
            "parcela": [parcela["parcela_codigo"]],
            "nome_area": [(parcela.get("nome_area") or "")[:80]],
            "area_ha": [parcela.get("area_ha")],
            "matricula": [(parcela.get("registro_matricula") or "")[:30]],
            "status": [(parcela.get("status") or "")[:20]],
        },
        geometry=[geom], crs=4674,
    )
    caminho, nome = _caminho(parcela, "shp.zip")
    base = caminho[:-8]  # remove .shp.zip
    gdf.to_file(base + ".shp", encoding="utf-8")
    with zipfile.ZipFile(caminho, "w", zipfile.ZIP_DEFLATED) as z:
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            p = base + ext
            if os.path.exists(p):
                z.write(p, os.path.basename(p))
                os.remove(p)
    return nome


# ----------------------------- TXT de vértices (UTM) -------------------------

def _vertices(geom):
    geoms = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    for g in geoms:
        # exterior sem repetir o último ponto (igual ao primeiro)
        yield from list(g.exterior.coords)[:-1]
        for r in g.interiors:
            yield from list(r.coords)[:-1]


def gerar_txt_utm(parcela, linhas_oficiais=None) -> str:
    """TXT tabulado: Codigo  X  SigmaX  Y  SigmaY  h  Sigmah
    - linhas_oficiais: lista de dicts vindos do CSV oficial do SIGEF
      (codigo, lon, lat, sigma_lon, sigma_lat, h, sigma_h). Se None,
      usa a geometria local (códigos sequenciais, sigmas '—')."""
    geom = shape(parcela["geojson"])
    lon_c = geom.centroid.x
    epsg = epsg_utm_sirgas(lon_c)
    tr = Transformer.from_crs(4674, epsg, always_xy=True)

    caminho, nome = _caminho(parcela, "txt")
    fuso = epsg - 31960

    cab = (f"# Parcela SIGEF: {parcela['parcela_codigo']}\n"
           f"# Imovel: {parcela.get('nome_area') or '-'} | "
           f"Area: {parcela.get('area_ha')} ha | "
           f"Matricula: {parcela.get('registro_matricula') or '-'}\n"
           f"# Sistema: SIRGAS 2000 / UTM fuso {fuso}S (EPSG:{epsg})\n"
           f"Codigo\tX(E)\tSigmaX(m)\tY(N)\tSigmaY(m)\tAltitude(m)\tSigmaAlt(m)\n")

    linhas = []
    if linhas_oficiais:
        for v in linhas_oficiais:
            x, y = tr.transform(v["lon"], v["lat"])
            linhas.append(
                f"{v['codigo']}\t{x:.3f}\t{v.get('sigma_lon') or '—'}\t"
                f"{y:.3f}\t{v.get('sigma_lat') or '—'}\t"
                f"{v.get('h') or '—'}\t{v.get('sigma_h') or '—'}"
            )
        origem = "# Fonte: CSV oficial de vertices do SIGEF/INCRA\n"
    else:
        for i, (lon, lat) in enumerate(_vertices(geom), start=1):
            x, y = tr.transform(lon, lat)
            linhas.append(f"V-{i:04d}\t{x:.3f}\t—\t{y:.3f}\t—\t—\t—")
        origem = ("# Fonte: geometria do espelho local (shapefile INCRA). "
                  "Codigos INXX, sigmas e altitudes: disponiveis na versao oficial.\n")

    with open(caminho, "w", encoding="utf-8") as f:
        f.write(cab + origem + "\n".join(linhas) + "\n")
    return nome


# ============================================================================
# PLANILHA ODS — vértices + limites + confrontantes no layout do modelo SIGEF
# ============================================================================
# Cruza os 3 CSVs oficiais do SIGEF (vértice, limite, polígono) e monta uma
# planilha limpa, com as colunas na MESMA ordem do modelo, pra o profissional
# selecionar as linhas e colar direto na planilha-modelo dele.
#
# Ordem das colunas (igual ao print do modelo):
#   Vértice | E/Long | Sigma long | N/Lat | Sigma lat | h | Sigma h |
#   Método Posicionamento | Tipo Limite | CNS | Matrícula | Descritivo(Confrontante)

def _dec_para_dms(dec, is_lat):
    """Converte grau decimal para string DMS com símbolos: -44°07'58,231"."""
    if dec is None:
        return ""
    sinal = "-" if dec < 0 else ""
    dec = abs(dec)
    g = int(dec)
    m_float = (dec - g) * 60
    m = int(m_float)
    s = (m_float - m) * 60
    # vírgula decimal (padrão BR), 3 casas nos segundos
    s_str = f"{s:.3f}".replace(".", ",")
    return f"{sinal}{g}°{m:02d}'{s_str}\""


def _vert_index(linhas_vertice):
    """Indexa os vértices por código, guardando UTM, geo e sigmas."""
    idx = {}
    for v in linhas_vertice:
        idx[v["codigo"]] = v
    return idx


def gerar_ods_planilha(parcela, vertices, limites, modo="utm"):
    """Gera a planilha ODS no layout do modelo SIGEF.

    vertices: lista de dicts (do parse_vertices_csv) com codigo, lon, lat,
              sigma_lon, sigma_lat, h, sigma_h, metodo, e (opcional) x, y.
    limites:  lista de dicts com de_vertice, ao_vertice, tipo, lado,
              confrontante, cns, matricula.
    modo:     'utm' -> colunas E/N projetadas | 'geo' -> DMS com símbolos.
    """
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableRow, TableCell
    from odf.text import P
    from odf.style import Style, TableColumnProperties

    doc = OpenDocumentSpreadsheet()
    tabela = Table(name="perimetro")

    def linha(valores):
        tr = TableRow()
        for val in valores:
            tc = TableCell()
            tc.addElement(P(text="" if val is None else str(val)))
            tr.addElement(tc)
        tabela.addElement(tr)

    # Cabeçalho do bloco de identificação (compacto, pro profissional saber o que é)
    linha([f"Denominação: {parcela.get('nome_area') or '-'}"])
    linha([f"Parcela SIGEF: {parcela['parcela_codigo']}"])
    linha([f"Matrícula: {parcela.get('registro_matricula') or '-'}  "
           f"CNS: {parcela.get('registro_cns') or '-'}"])
    sist = "UTM (E/N)" if modo == "utm" else "Geográfico (DMS)"
    linha([f"Sistema: SIRGAS 2000 / {sist}"])
    linha([])  # linha em branco

    # Cabeçalho das colunas (ordem do modelo)
    col_coord1 = "E" if modo == "utm" else "Longitude"
    col_coord2 = "N" if modo == "utm" else "Latitude"
    linha(["Vértice", col_coord1, "Sigma long", col_coord2, "Sigma lat",
           "h", "Sigma h", "Método Posicionamento",
           "Tipo Limite", "CNS", "Matrícula", "Descritivo (Confrontante)"])

    # Indexa limites pelo vértice de origem (o "de_vertice" define o segmento vante)
    lim_por_de = {}
    for l in (limites or []):
        lim_por_de[l.get("de_vertice")] = l

    # Transformer pra UTM, se necessário
    tr_utm = None
    if modo == "utm":
        lon_c = parcela["geojson"]
        from shapely.geometry import shape as _shape
        lon_c = _shape(parcela["geojson"]).centroid.x
        epsg = epsg_utm_sirgas(lon_c)
        tr_utm = Transformer.from_crs(4674, epsg, always_xy=True)

    for v in vertices:
        cod = v["codigo"]
        if modo == "utm":
            if v.get("x") is not None and v.get("y") is not None:
                c1, c2 = f"{v['x']:.2f}".replace(".", ","), f"{v['y']:.2f}".replace(".", ",")
            else:
                x, y = tr_utm.transform(v["lon"], v["lat"])
                c1, c2 = f"{x:.2f}".replace(".", ","), f"{y:.2f}".replace(".", ",")
        else:
            c1 = _dec_para_dms(v["lon"], is_lat=False)
            c2 = _dec_para_dms(v["lat"], is_lat=True)

        def fmt(x):
            return "" if x is None else f"{x}".replace(".", ",")

        lim = lim_por_de.get(cod, {})
        linha([
            cod, c1, fmt(v.get("sigma_lon")), c2, fmt(v.get("sigma_lat")),
            fmt(v.get("h")), fmt(v.get("sigma_h")), v.get("metodo") or "",
            lim.get("tipo") or "", lim.get("cns") or "",
            lim.get("matricula") or "", lim.get("confrontante") or "",
        ])

    doc.spreadsheet.addElement(tabela)

    sufixo = "UTM" if modo == "utm" else "Geo"
    caminho, nome = _caminho(parcela, f"perimetro_{sufixo}.ods")
    doc.save(caminho)
    return nome
