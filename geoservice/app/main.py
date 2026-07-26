# -*- coding: utf-8 -*-
"""Cérebro do bot: máquina de estados dos menus + montagem das respostas.

O n8n recebe o webhook da Evolution, repassa pra cá (POST /mensagem) e envia
de volta ao usuário a lista de respostas que este serviço retorna.

Tipos de resposta:
  {"tipo": "texto",     "texto": "..."}
  {"tipo": "imagem",    "url": ".../files/x.png", "legenda": "..."}
  {"tipo": "documento", "url": ".../files/x.pdf", "nome": "x.pdf", "legenda": "..."}
"""

import os
import json

import redis
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, exports, parsing
from .sync import router as sync_router
from .sigef import (sessao, parse_vertices_csv, parse_limites_csv,
                    CaptchaError)

FILES_DIR = os.getenv("FILES_DIR", "/data/files")
BASE_URL = os.getenv("BASE_URL", "http://geoservice:8000")  # como a Evolution enxerga este serviço
TTL_ESTADO = int(os.getenv("TTL_ESTADO", "1800"))  # 30 min
SIGEF_OFICIAL = os.getenv("SIGEF_OFICIAL", "1") == "1"  # Entrega 2 ligada?

r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/3"),
                         decode_responses=True)

app = FastAPI(title="SIGEF Bot Geoservice")
os.makedirs(FILES_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")
app.include_router(sync_router)


class Entrada(BaseModel):
    telefone: str
    texto: str | None = None
    lat: float | None = None
    lon: float | None = None


def T(t): return {"tipo": "texto", "texto": t}


MENU_INICIAL = (
    "👋 Bem-vindo ao *Consulta SIGEF*!\n"
    "Consulto parcelas certificadas no INCRA em segundos.\n\n"
    "Como você quer localizar o imóvel?\n\n"
    "1️⃣ Coordenada (UTM ou geográfica)\n"
    "2️⃣ Código da parcela certificada (SIGEF)\n"
    "3️⃣ Código do imóvel no SNCR / CCIR\n"
    "4️⃣ Código de vértice (ex: FJML-P-0001)\n\n"
    "_Responda com o número da opção._"
)

MENU_DOCS = (
    "O que você deseja receber?\n\n"
    "1️⃣ Planta (A4) + Memorial — PDF oficial SIGEF\n"
    "2️⃣ Planilha de vértices, limites e confrontantes (.ods)\n"
    "3️⃣ KML ou SHP do imóvel\n\n"
    "_Responda com o número, ou *0* para nova consulta._"
)

MENU_ODS = (
    "Em qual sistema de coordenadas?\n\n"
    "1️⃣ UTM (E/N)\n"
    "2️⃣ Geográfico (lat/long em DMS)\n\n"
    "_Responda com o número, ou *0* para nova consulta._"
)

MENU_FORMATO = ("Qual formato?\n\n1️⃣ KML (Google Earth)\n2️⃣ SHP (shapefile)\n\n"
                "_Responda com o número, ou *0* para nova consulta._")


def estado_get(tel):
    raw = r.get(f"sigefbot:{tel}")
    return json.loads(raw) if raw else {"etapa": "INICIO"}


def estado_set(tel, est):
    r.set(f"sigefbot:{tel}", json.dumps(est), ex=TTL_ESTADO)


def ficha(p):
    return (
        f"🏡 *{p.get('nome_area') or 'Imóvel sem denominação'}*\n"
        f"📐 Área: *{p.get('area_ha')} ha*\n"
        f"📄 Matrícula: {p.get('registro_matricula') or '—'} "
        f"(CNS {p.get('registro_cns') or '—'})\n"
        f"✅ Status: {p.get('status') or '—'} | "
        f"Situação: {p.get('situacao') or '—'}\n"
        f"🗓 Certificação: {p.get('data_aprovacao') or '—'}\n"
        f"🔖 Parcela: `{p['parcela_codigo']}`"
    )


def resultado_parcela(tel, p):
    est = {"etapa": "MENU_DOCS", "uuid": p["parcela_codigo"]}
    estado_set(tel, est)
    resp = [T(ficha(p))]
    try:
        nome_png = exports.gerar_png(p)
        resp.append({"tipo": "imagem", "url": f"{BASE_URL}/files/{nome_png}",
                     "legenda": "Limites SIGEF sobre imagem de satélite"})
    except Exception as e:
        resp.append(T(f"(não consegui gerar a imagem de satélite agora: {e})"))
    resp.append(T(MENU_DOCS))
    return resp


def resultado_multiplo(tel, parcelas, contexto=""):
    """Vários imóveis no resultado: gera UM mapa com todas numeradas e lista
    pra pessoa escolher por número. Guarda os UUIDs no estado."""
    # guarda a lista de uuids na ordem; a pessoa responde 1..N
    est = {"etapa": "ESCOLHA_MULTIPLA",
           "opcoes": [p["parcela_codigo"] for p in parcelas]}
    estado_set(tel, est)

    resp = []
    cab = contexto or f"Encontrei *{len(parcelas)} parcelas*."
    resp.append(T(cab + " Veja no mapa e escolha pelo número 👇"))

    try:
        nome_png = exports.gerar_png_multiplas(parcelas)
        resp.append({"tipo": "imagem", "url": f"{BASE_URL}/files/{nome_png}",
                     "legenda": "Cada número é uma parcela certificada"})
    except Exception as e:
        resp.append(T(f"(não consegui gerar o mapa agora: {e})"))

    linhas = "\n".join(
        f"*{i+1}* — {p.get('nome_area') or 'Sem denominação'} "
        f"({p.get('area_ha')} ha)"
        + (f" • Matr. {p.get('registro_matricula')}" if p.get('registro_matricula') else "")
        for i, p in enumerate(parcelas))
    resp.append(T(f"{linhas}\n\n_Responda com o número do imóvel desejado, "
                  f"ou *0* para nova consulta._"))
    return resp


async def entregar_documento(p, opcao):
    """Entrega 2 com fallback local. Retorna lista de respostas."""
    uuid = p["parcela_codigo"]
    resp = []

    if opcao == "1":  # Planta (A4) + Memorial PDF oficial
        if SIGEF_OFICIAL:
            # Planta A4 (escala calculada pela geometria) - nao trava o memorial se falhar
            try:
                corpo_pl, _ = await sessao.baixar_planta(uuid, p.get("geojson"))
                caminho_pl = os.path.join(FILES_DIR, f"planta_{uuid[:8]}.pdf")
                open(caminho_pl, "wb").write(corpo_pl)
                resp.append({"tipo": "documento",
                             "url": f"{BASE_URL}/files/{os.path.basename(caminho_pl)}",
                             "nome": f"Planta_{exports._slug(p.get('nome_area'))}.pdf",
                             "legenda": "Planta oficial (A4) — SIGEF/INCRA"})
            except Exception:
                pass
            try:
                corpo, _ = await sessao.baixar("memorial", uuid)
                caminho = os.path.join(FILES_DIR, f"memorial_{uuid[:8]}.pdf")
                open(caminho, "wb").write(corpo)
                resp.append({"tipo": "documento",
                             "url": f"{BASE_URL}/files/{os.path.basename(caminho)}",
                             "nome": f"Memorial_{exports._slug(p.get('nome_area'))}.pdf",
                             "legenda": "Memorial descritivo oficial (SIGEF/INCRA)"})
            except CaptchaError:
                pass
            except Exception as e:
                resp.append(T(f"⚠️ SIGEF indisponível agora ({e})."))
            if resp:
                return resp
        resp.append(T(
            "📎 Gere a planta e o memorial oficiais direto na página da parcela "
            "(login gov.br):\n"
            f"https://sigef.incra.gov.br/geo/parcela/detalhe/{uuid}/"))
        return resp

    if opcao in ("ods_utm", "ods_geo"):  # Planilha de vértices/limites/confrontantes
        modo = "utm" if opcao == "ods_utm" else "geo"
        if not SIGEF_OFICIAL:
            resp.append(T(
                "🔜 A planilha de vértices depende dos dados oficiais do SIGEF, "
                "que ainda não estão ativos. Por enquanto posso te enviar KML/SHP "
                "do imóvel (opção 3)."))
            return resp
        try:
            cv, _ = await sessao.baixar("vertices_csv", uuid)
            vertices = parse_vertices_csv(cv)
        except CaptchaError:
            resp.append(T(
                "⚠️ O acesso ao SIGEF pediu verificação agora e não consegui "
                "baixar os dados dos vértices. Tente em alguns minutos."))
            return resp
        except Exception as e:
            resp.append(T(f"⚠️ Não consegui obter os vértices no SIGEF agora ({e})."))
            return resp

        limites = []
        try:
            limites = await sessao.limites_detalhe(uuid)
        except Exception:
            limites = []  # segue sem confrontantes se o limite falhar

        if not vertices:
            resp.append(T("Não encontrei vértices para essa parcela no SIGEF."))
            return resp

        nome = exports.gerar_ods_planilha(p, vertices, limites, modo=modo)
        sis = "UTM (E/N)" if modo == "utm" else "Geográfico (DMS)"
        conf = "com confrontantes" if limites else "sem confrontantes (indisponíveis)"
        resp.append({"tipo": "documento", "url": f"{BASE_URL}/files/{nome}",
                     "nome": nome,
                     "legenda": f"Planilha de vértices, limites e confrontantes "
                                f"em {sis} — {conf}. É só selecionar as linhas e "
                                f"colar na sua planilha-modelo do SIGEF."})
        return resp

    if opcao in ("kml", "shp"):
        if SIGEF_OFICIAL:
            try:
                corpo, _ = await sessao.baixar(opcao, uuid)
                ext = "kml" if opcao == "kml" else "zip"
                caminho = os.path.join(FILES_DIR, f"{opcao}_{uuid[:8]}.{ext}")
                open(caminho, "wb").write(corpo)
                resp.append({"tipo": "documento",
                             "url": f"{BASE_URL}/files/{os.path.basename(caminho)}",
                             "nome": f"{exports._slug(p.get('nome_area'))}.{ext}",
                             "legenda": f"{opcao.upper()} oficial (SIGEF/INCRA)"})
                return resp
            except Exception:
                pass
        nome = exports.gerar_kml(p) if opcao == "kml" else exports.gerar_shp(p)
        resp.append({"tipo": "documento", "url": f"{BASE_URL}/files/{nome}",
                     "nome": nome, "legenda": f"{opcao.upper()} (gerado do espelho local)"})
        return resp

    return [T("Opção inválida. " + MENU_DOCS)]


@app.post("/mensagem")
async def mensagem(e: Entrada):
    tel = e.telefone
    texto = (e.texto or "").strip()
    est = estado_get(tel)
    etapa = est.get("etapa", "INICIO")

    # Atalhos globais
    if texto.lower() in ("menu", "0", "voltar", "inicio", "início"):
        estado_set(tel, {"etapa": "AGUARDANDO"})
        return {"respostas": [T(MENU_INICIAL)]}

    # Entradas "diretas" valem em qualquer etapa: localização, coordenada, UUID
    if e.lat is not None and e.lon is not None:
        return {"respostas": await consultar_ponto(tel, e.lat, e.lon)}

    uuid = parsing.detectar_uuid(texto)
    if uuid:
        p = db.por_codigo(uuid)
        if not p:
            return {"respostas": [T(
                "Não encontrei essa parcela na base do RJ. Confira o código "
                "ou, se for de outro estado, me avisa que ainda não cubro lá. "
                "Digite *menu* para recomeçar.")]}
        return {"respostas": resultado_parcela(tel, p)}

    # Código SNCR/CCIR (13 dígitos) — vale em qualquer etapa, exceto submenus
    if etapa not in ("MENU_DOCS", "MENU_FORMATO", "MENU_ODS", "ESCOLHA_MULTIPLA"):
        sncr = parsing.detectar_sncr(texto)
        if sncr:
            achados = db.por_sncr(sncr)
            if not achados:
                return {"respostas": [T(
                    "Não encontrei imóvel com esse código SNCR/CCIR na base do "
                    "RJ. Confira o número (13 dígitos) ou tente pelo código da "
                    "parcela. Digite *menu* para recomeçar.")]}
            if len(achados) > 1:
                return {"respostas": resultado_multiplo(
                    tel, achados,
                    contexto=f"Esse código SNCR/CCIR tem *{len(achados)} "
                             f"parcelas certificadas*.")}
            return {"respostas": resultado_parcela(tel, achados[0])}

    coord = parsing.detectar_coordenada(texto)
    if coord and etapa not in ("MENU_DOCS", "MENU_FORMATO", "MENU_ODS", "ESCOLHA_MULTIPLA"):
        return {"respostas": await consultar_ponto(tel, coord[0], coord[1])}

    # Máquina de estados
    if etapa in ("INICIO",):
        estado_set(tel, {"etapa": "AGUARDANDO"})
        return {"respostas": [T(MENU_INICIAL)]}

    if etapa == "AGUARDANDO":
        if texto == "1":
            estado_set(tel, {"etapa": "AGUARDANDO"})
            return {"respostas": [T(
                "Me envie a coordenada — vale qualquer um:\n"
                "• 📍 localização pelo clipe do WhatsApp\n"
                "• Geográfica decimal: `-22.3683, -44.1328`\n"
                "• DMS: `-22°22'05,7\" -44°07'58,2\"`\n"
                "• UTM: `615000 7524000` (assumo fuso 23S; "
                "se for outro, inclua, ex: `... 24`)")]}
        if texto == "2":
            return {"respostas": [T(
                "Me envie o código da parcela (aquele UUID, ex: "
                "`e7ed2f53-997e-4e75-a575-db8734d026d9`).")]}
        if texto == "3":
            return {"respostas": [T(
                "Me envie o código do imóvel no SNCR/CCIR (13 dígitos, ex: "
                "`9511023531752`).")]}
        if texto == "4":
            estado_set(tel, {"etapa": "AGUARDA_VERTICE"})
            return {"respostas": [T(
                "Me envie o código do vértice (ex: `FJML-P-0001`).")]}
        if coord:
            return {"respostas": await consultar_ponto(tel, coord[0], coord[1])}
        return {"respostas": [T(MENU_INICIAL)]}

    if etapa == "AGUARDA_VERTICE":
        cod = parsing.detectar_vertice(texto)
        if not cod:
            return {"respostas": [T(
                "Não reconheci esse código de vértice. O formato é tipo "
                "`FJML-P-0001`. Tente de novo ou digite *menu*.")]}
        if not SIGEF_OFICIAL:
            return {"respostas": [T(
                "🔜 Busca por vértice chega em breve! Por enquanto, use "
                "coordenada ou código da parcela (*menu*).")]}
        try:
            uuids = await sessao.buscar_por_vertice(cod)
        except Exception:
            uuids = []
        if not uuids:
            return {"respostas": [T(
                "Não consegui localizar esse vértice no SIGEF agora. "
                "Tente por coordenada ou código da parcela (*menu*).")]}
        # busca as parcelas que estão na base local
        parcelas = [p for u in uuids if (p := db.por_codigo(u))]
        if not parcelas:
            return {"respostas": [T(
                f"O vértice `{cod}` existe no SIGEF, mas as parcelas ligadas a "
                "ele não estão na minha base do RJ. Tente por código da parcela.")]}
        if len(parcelas) > 1:
            return {"respostas": resultado_multiplo(
                tel, parcelas,
                contexto=f"O vértice *{cod}* é divisa de *{len(parcelas)} "
                         f"parcelas*.")}
        return {"respostas": resultado_parcela(tel, parcelas[0])}

    if etapa == "ESCOLHA_MULTIPLA":
        opcoes = est.get("opcoes", [])
        if texto.isdigit() and 1 <= int(texto) <= len(opcoes):
            uuid = opcoes[int(texto) - 1]
            p = db.por_codigo(uuid)
            if not p:
                estado_set(tel, {"etapa": "AGUARDANDO"})
                return {"respostas": [T("Essa parcela não está mais disponível. "
                                        + MENU_INICIAL)]}
            return {"respostas": resultado_parcela(tel, p)}
        return {"respostas": [T(
            f"Responda com um número de *1* a *{len(opcoes)}* (o do imóvel no "
            f"mapa), ou *0* para nova consulta.")]}

    if etapa == "MENU_DOCS":
        p = db.por_codigo(est.get("uuid", ""))
        if not p:
            estado_set(tel, {"etapa": "AGUARDANDO"})
            return {"respostas": [T("Sessão expirou. " + MENU_INICIAL)]}
        if texto == "2":  # ODS -> escolher UTM ou Geo
            est["etapa"] = "MENU_ODS"
            estado_set(tel, est)
            return {"respostas": [T(MENU_ODS)]}
        if texto == "3":  # KML/SHP -> escolher formato
            est["etapa"] = "MENU_FORMATO"
            estado_set(tel, est)
            return {"respostas": [T(MENU_FORMATO)]}
        if texto == "1":  # PDF planta + memorial
            resp = await entregar_documento(p, "1")
            resp.append(T("Posso enviar mais algum item? (1, 2, 3 ou *0* "
                          "para nova consulta)"))
            return {"respostas": resp}
        return {"respostas": [T(MENU_DOCS)]}

    if etapa == "MENU_ODS":
        p = db.por_codigo(est.get("uuid", ""))
        if not p:
            estado_set(tel, {"etapa": "AGUARDANDO"})
            return {"respostas": [T("Sessão expirou. " + MENU_INICIAL)]}
        if texto in ("1", "2"):
            opcao = "ods_utm" if texto == "1" else "ods_geo"
            resp = await entregar_documento(p, opcao)
            est["etapa"] = "MENU_DOCS"
            estado_set(tel, est)
            resp.append(T("Posso enviar mais algum item? (1, 2, 3 ou *0* "
                          "para nova consulta)"))
            return {"respostas": resp}
        return {"respostas": [T(MENU_ODS)]}

    if etapa == "MENU_FORMATO":
        p = db.por_codigo(est.get("uuid", ""))
        if not p:
            estado_set(tel, {"etapa": "AGUARDANDO"})
            return {"respostas": [T("Sessão expirou. " + MENU_INICIAL)]}
        if texto in ("1", "2"):
            resp = await entregar_documento(p, "kml" if texto == "1" else "shp")
            est["etapa"] = "MENU_DOCS"
            estado_set(tel, est)
            resp.append(T("Posso enviar mais algum item? (1, 2, 3 ou *0* "
                          "para nova consulta)"))
            return {"respostas": resp}
        return {"respostas": [T(MENU_FORMATO)]}

    estado_set(tel, {"etapa": "AGUARDANDO"})
    return {"respostas": [T(MENU_INICIAL)]}


async def consultar_ponto(tel, lat, lon):
    dentro = db.por_ponto(lat, lon)
    if dentro:
        if len(dentro) > 1:
            return resultado_multiplo(
                tel, dentro,
                contexto=f"Esse ponto cai em *{len(dentro)} parcelas "
                         f"sobrepostas* (divisa).")
        return resultado_parcela(tel, dentro[0])

    perto = db.proximas(lat, lon)
    if perto:
        if len(perto) > 1:
            return resultado_multiplo(
                tel, perto,
                contexto="O ponto não cai dentro de nenhuma parcela, mas "
                         f"encontrei *{len(perto)} próximas* (raio de 500 m).")
        return resultado_parcela(tel, perto[0])

    return [T("Nenhuma parcela certificada nesse ponto nem num raio de 500 m. "
              "Lembrando que minha base cobre o *RJ* (SIGEF particular). "
              "Digite *menu* para nova consulta.")]


@app.get("/health")
def health():
    return {"ok": True, "parcelas": db.total_parcelas()}
