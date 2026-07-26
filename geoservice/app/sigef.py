# -*- coding: utf-8 -*-
"""Sessão SIGEF (cookies) + downloads oficiais — v3.5 (Módulo Sessão v2).

  - Usa a SESSÃO HUMANA (cookies do navegador, via Cookie-Editor) num
    cliente HTTP (httpx). Sem Playwright, sem CPF/senha na VPS.
  - Se a sessão expira, avisa o César no WhatsApp e cai pro fallback local.

v3.1: parser de limites trata '_' e espaço como iguais (conserta DO_VERTICE).
v3.2: novo limites_detalhe(uuid) — raspa a tabela de Limites da página de
      detalhe (?limit=all), que traz o confrontante COM CNS + matrícula
      (o CSV de limites só tem o nome). Devolve o MESMO formato de dict do
      parse_limites_csv, então é drop-in (exports.py não muda).
v3.3: keep-alive — task de fundo (auto-inicia na 1ª sessão válida) que pinga
      o SIGEF a cada 15 min pra manter a sessão viva, loga heartbeat (docker
      logs, pra medir a duração) e avisa no WhatsApp quando a sessão cair.
v3.4: baixar_planta calcula a escala A4 pela geometria (geojson) quando não
      recebe escala explícita — pra ligar a planta na opção 1 do menu.

Interface pública: sessao, SigefSession, CaptchaError, parse_vertices_csv,
parse_limites_csv, e agora sessao.limites_detalhe().
"""

import os
import re
import csv
import io
import json
import asyncio
from datetime import datetime
from html import unescape
from pathlib import Path

import httpx

WEBHOOK_NOTIFY = os.getenv("WEBHOOK_NOTIFY", "")
COOKIES_FILE = os.getenv("COOKIES_FILE", "/data/cookies_sigef.json")
FILES_DIR = os.getenv("FILES_DIR", "/data/files")

SIGEF = "https://sigef.incra.gov.br"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

URLS = {
    "memorial":     SIGEF + "/geo/parcela/memorial/{u}/",
    "kml":          SIGEF + "/geo/exportar/parcela/kml/{u}/",
    "shp":          SIGEF + "/geo/exportar/parcela/shp/{u}/",
    "vertices_csv": SIGEF + "/geo/exportar/vertice/csv/{u}/",
    "limites_csv":  SIGEF + "/geo/exportar/limite/csv/{u}/",
    "detalhe":      SIGEF + "/geo/parcela/detalhe/{u}/",
    "consulta_vertice": SIGEF + "/consultar/parcelas/?vertice={v}",
    "planta":       SIGEF + "/geo/parcela/planta/{u}/{escala}/",
}

UUID_DUMMY = "00000000-0000-0000-0000-000000000000"

_lock = asyncio.Lock()


class CaptchaError(RuntimeError):
    """Sinaliza que a sessão oficial está indisponível (expirada/ausente).
    Nome mantido por compatibilidade com o fallback local."""
    pass


async def _notificar(texto: str):
    if not WEBHOOK_NOTIFY:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(WEBHOOK_NOTIFY, json={"texto": texto})
    except Exception:
        pass


def _ler_cookie_header() -> str:
    """Lê o cookies_sigef.json (Cookie-Editor OU storage_state) e monta o
    header 'Cookie: a=b; c=d' só com os cookies do incra.gov.br. Relê o
    arquivo toda vez -> sobrescrever o arquivo renova a sessão."""
    p = Path(COOKIES_FILE)
    if not p.exists():
        return ""
    try:
        dados = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if isinstance(dados, dict) and "cookies" in dados:
        lista = dados["cookies"]
    elif isinstance(dados, list):
        lista = dados
    else:
        return ""
    pares = [f'{c["name"]}={c["value"]}'
             for c in lista
             if c.get("name") and "incra.gov.br" in c.get("domain", "")]
    return "; ".join(pares)


def _redirecionou_login(resp) -> bool:
    loc = str(resp.headers.get("location", ""))
    return resp.status_code in (301, 302, 303) and (
        "acesso.gov.br" in loc or "/login" in loc)


class SigefSession:
    def __init__(self):
        self._cookie = ""
        self._ka_iniciado = False
        self._ka_task = None
        self._ka_viva = True  # estado anterior da sessão (avisa só na virada)

    async def garantir(self):
        async with _lock:
            cookie = _ler_cookie_header()
            if not cookie:
                await _notificar(
                    "⚠️ SIGEF Bot: não achei a sessão (cookies_sigef.json). "
                    "Faça login no navegador, exporte os cookies (Cookie-Editor) "
                    "e suba pra VPS. Documentos oficiais em fallback local.")
                raise CaptchaError("cookies_sigef.json ausente ou vazio")
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
                    r = await c.get(URLS["detalhe"].format(u=UUID_DUMMY),
                                    headers={"User-Agent": UA, "Cookie": cookie})
            except Exception as e:
                raise RuntimeError(f"Falha ao validar sessão SIGEF: {e}")
            if _redirecionou_login(r):
                await _notificar(
                    "⚠️ SIGEF Bot: a sessão do SIGEF expirou. Faça login no "
                    "navegador, exporte os cookies (Cookie-Editor) e suba pra VPS "
                    "(docker cp p/ /data/cookies_sigef.json). Documentos oficiais "
                    "em fallback local até renovar.")
                raise CaptchaError("sessão SIGEF expirada")
            self._cookie = cookie
            self._iniciar_keepalive()

    # --- keep-alive: mantém a sessão viva e avisa quando ela cair ---------
    def _iniciar_keepalive(self):
        """Sobe a task de keep-alive na 1ª sessão válida (uma vez só).
        Se auto-inicia aqui, então não precisa mexer no main.py."""
        if self._ka_iniciado:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._ka_iniciado = True
        self._ka_task = loop.create_task(self._keepalive_loop())
        print("[keepalive] iniciado (ping a cada 15 min)", flush=True)

    async def _keepalive_loop(self, intervalo: int = 900):
        while True:
            await asyncio.sleep(intervalo)
            try:
                await self._keepalive_ping()
            except Exception as e:
                print(f"[keepalive] erro no ping: {e}", flush=True)

    async def _keepalive_ping(self):
        cookie = _ler_cookie_header()  # relê o arquivo (pode ter sido renovado)
        if not cookie:
            return
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
            r = await c.get(SIGEF + "/consultar/parcelas/",
                            headers={"User-Agent": UA, "Cookie": cookie})
        viva = not _redirecionou_login(r)
        agora = datetime.now().strftime("%d/%m %H:%M:%S")
        if viva:
            print(f"[keepalive] {agora} sessao viva", flush=True)
            if not self._ka_viva:
                await _notificar("✅ SIGEF Bot: sessão renovada e ativa de novo.")
            self._ka_viva = True
        else:
            print(f"[keepalive] {agora} sessao MORTA", flush=True)
            if self._ka_viva:  # avisa só na virada viva -> morta
                await _notificar(
                    "⚠️ SIGEF Bot: a sessão do SIGEF expirou (keep-alive detectou). "
                    "Faça login no navegador, exporte os cookies (Cookie-Editor) e "
                    "suba pra VPS (docker cp p/ /data/cookies_sigef.json).")
            self._ka_viva = False

    async def _get(self, url: str, timeout: int = 120):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            r = await c.get(url, headers={"User-Agent": UA, "Cookie": self._cookie})
        if _redirecionou_login(r):
            await _notificar(
                "⚠️ SIGEF Bot: a sessão expirou no meio de um download. "
                "Renove os cookies (Cookie-Editor -> subir pra VPS).")
            raise CaptchaError("sessão expirou durante o download")
        return r

    async def baixar(self, tipo: str, uuid: str) -> tuple[bytes, str]:
        """Baixa um artefato oficial. Retorna (bytes, content_type).
        tipo ∈ memorial, kml, shp, vertices_csv, limites_csv."""
        await self.garantir()
        r = await self._get(URLS[tipo].format(u=uuid))
        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or ("text/html" in ct and tipo != "consulta_vertice"):
            raise RuntimeError(f"SIGEF retornou {r.status_code} ({ct}) para {tipo}")
        return r.content, ct

    async def baixar_planta(self, uuid: str, geojson=None, escala: int = None) -> tuple[bytes, str]:
        """Baixa a planta (PDF). Se 'escala' não vier, calcula pela geometria
        (geojson) pra caber no A4. O SIGEF aceita escala arbitrária."""
        await self.garantir()
        if escala is None:
            escala = _escala_da_geometria(geojson)
        url = URLS["planta"].format(u=uuid, escala=int(escala))
        r = await self._get(url, timeout=180)
        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or "text/html" in ct:
            raise RuntimeError(f"SIGEF retornou {r.status_code} ({ct}) para planta")
        return r.content, ct

    async def limites_detalhe(self, uuid: str):
        """Lê os limites/confrontantes da PÁGINA DE DETALHE (?limit=all).
        Traz nome + CNS + matrícula (o CSV de limites só tem o nome) e o
        azimute em DMS. Devolve o MESMO formato de dict do parse_limites_csv,
        então serve de drop-in no exports.gerar_ods_planilha."""
        await self.garantir()
        r = await self._get(URLS["detalhe"].format(u=uuid) + "?limit=all", timeout=90)
        return _parse_limites_html(r.text)

    async def buscar_por_vertice(self, codigo_vertice: str):
        """Acha a(s) parcela(s) de um vértice via consulta pública (scraping).
        Um vértice de divisa pertence a várias parcelas -> retorna todas."""
        await self.garantir()
        r = await self._get(URLS["consulta_vertice"].format(v=codigo_vertice), timeout=60)
        achados = re.findall(r"/geo/parcela/detalhe/([0-9a-f-]{36})/", r.text)
        vistos, unicos = set(), []
        for u in achados:
            if u not in vistos:
                vistos.add(u)
                unicos.append(u)
        return unicos


# --- escala pra planta A4 (SIGEF aceita escala arbitrária) -------------------

def escala_para_a4(largura_m: float, altura_m: float) -> int:
    """Denominador de escala pra caber a parcela num A4, igual ao SIGEF.
    largura_m/altura_m = bbox da parcela em METROS (CRS métrico).

    Moldura calibrada com um caso real: Bela Vista tem 3191,89 m de largura e o
    SIGEF sugeriu escala 22018 -> moldura útil ~0,145 x 0,115 m. Usamos um pouco
    menos (margem de segurança) pra garantir que o SIGEF mantenha A4 e não suba
    pra A3. O SIGEF dimensiona pela MAIOR direção contra a moldura."""
    PAPEL_LARG, PAPEL_ALT = 0.143, 0.113  # metros de papel dentro da moldura A4
    if largura_m <= 0 or altura_m <= 0:
        return 25000
    esc = max(largura_m / PAPEL_LARG, altura_m / PAPEL_ALT)
    return max(500, int(round(esc)))


def _escala_da_geometria(geojson) -> int:
    """Escala A4 a partir do geojson (graus): projeta pra UTM SIRGAS pra medir
    em metros e chama escala_para_a4. Fallback 25000 se algo falhar."""
    try:
        from shapely.geometry import shape
        from pyproj import Transformer
        geom = shape(geojson)
        minx, miny, maxx, maxy = geom.bounds
        zona = int((geom.centroid.x + 180) / 6) + 1
        epsg = 31978 + (zona - 18)  # SIRGAS 2000 / UTM zonas sul (18S=31978 ... 25S=31985)
        tr = Transformer.from_crs(4674, epsg, always_xy=True)
        x0, y0 = tr.transform(minx, miny)
        x1, y1 = tr.transform(maxx, maxy)
        return escala_para_a4(abs(x1 - x0), abs(y1 - y0))
    except Exception:
        return 25000


# --- parser da tabela de Limites (HTML da página de detalhe) -----------------

def _texto_td(td_html: str) -> str:
    """Tira tags internas, desescapa entidades (ex.: &#39; -> '), colapsa
    espaços/quebras de linha em um espaço só."""
    sem_tags = re.sub(r"<[^>]+>", " ", td_html)
    return re.sub(r"\s+", " ", unescape(sem_tags)).strip()


def _parse_confrontante(txt: str):
    """'CNS: 09.232-0 - Mat.:4014 - Sítio dos Coqueiros' -> (cns, mat, nome).
    A matrícula pode ter hífen (416-R4); o separador entre campos é ' - '
    (hífen com espaços), então o não-guloso corta certo. Se não bater o
    padrão (ex.: 'Rio Piraí'), devolve ('', '', txt)."""
    m = re.match(r"CNS:\s*(.+?)\s+-\s+Mat\.?:?\s*(.+?)\s+-\s+(.+)$", txt)
    if m:
        return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    return "", "", txt


def _parse_limites_html(html: str):
    """Lê a tabela de Limites da página de detalhe (a que tem a coluna
    'Confrontante') e devolve lista de dicts no formato do parse_limites_csv,
    com cns/matricula preenchidos e azimute em DMS."""
    idx = html.find("Confrontante")
    if idx < 0:
        return []
    ini = html.rfind("<table", 0, idx)
    fim = html.find("</table>", idx)
    if ini < 0 or fim < 0:
        return []
    tabela = html[ini:fim]

    saida = []
    for linha in re.findall(r"<tr[^>]*>(.*?)</tr>", tabela, re.S):
        celulas = re.findall(r"<td[^>]*>(.*?)</td>", linha, re.S)
        if len(celulas) < 7:
            continue  # pula o cabeçalho (usa <th>) e linhas incompletas
        vals = [_texto_td(c) for c in celulas]
        cns, mat, nome = _parse_confrontante(vals[6])
        saida.append({
            "de_vertice": vals[0],
            "ao_vertice": vals[1],
            "tipo": vals[2],
            "lado": vals[3],
            "azimute": vals[4],
            "comprimento": vals[5],
            "confrontante": nome,
            "cns": cns,
            "matricula": mat,
        })
    return saida


# ----------------------- Parsers dos CSVs oficiais ---------------------------

def _f(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _norm(s):
    """Nome de coluna normalizado: minúsculo, sem pontas, com underscore
    tratado como ESPAÇO (faz 'DO_VERTICE' casar com 'do vertice')."""
    return str(s).lower().strip().replace("_", " ")


def _dms_ou_decimal(v):
    """Aceita -44°07'58,231\" ou decimal."""
    from .parsing import DMS_RE, dms_para_decimal
    s = str(v).strip()
    m = DMS_RE.search(s)
    if m:
        return dms_para_decimal(*m.groups())
    return _f(s)


def _wkt_point_lonlat(wkt):
    """Extrai (lon, lat) de um 'POINT (-44.13 -22.36)'."""
    if not wkt:
        return None, None
    m = re.search(r"POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)", str(wkt), re.I)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def parse_vertices_csv(conteudo: bytes):
    """Lê o CSV oficial de vértices do SIGEF (vert_exportacao)."""
    texto = conteudo.decode("utf-8-sig", errors="replace")
    sep = ";" if texto.count(";") >= texto.count(",") else ","
    leitor = csv.DictReader(io.StringIO(texto), delimiter=sep)

    def acha(campos, *chaves):
        chaves = [_norm(k) for k in chaves]
        for c in campos:
            cl = _norm(c)
            if any(cl == k or k in cl for k in chaves):
                return c
        return None

    campos = leitor.fieldnames or []

    def exato(campos, *nomes):
        nomes = [_norm(n) for n in nomes]
        for c in campos:
            if _norm(c) in nomes:
                return c
        return None

    c_cod = exato(campos, "codigo", "código") or acha(campos, "codigo")
    c_met = exato(campos, "metodo", "método") or acha(campos, "metodo", "método")
    c_x = exato(campos, "x", "e", "este", "leste")
    c_y = exato(campos, "y", "n", "norte")
    c_z = exato(campos, "z", "altitude", "altura") or acha(campos, "altitude", "altura")
    c_sx = exato(campos, "sigma_x", "sigma x") or acha(campos, "sigma long")
    c_sy = exato(campos, "sigma_y", "sigma y") or acha(campos, "sigma lat")
    c_sz = exato(campos, "sigma_z", "sigma z") or acha(campos, "sigma alt")
    c_wkt = acha(campos, "geometria_wkt", "wkt", "geometria")
    c_lon = exato(campos, "longitude", "long") or acha(campos, "longitude")
    c_lat = exato(campos, "latitude", "lat") or acha(campos, "latitude")

    saida = []
    for row in leitor:
        lon = _dms_ou_decimal(row.get(c_lon)) if c_lon else None
        lat = _dms_ou_decimal(row.get(c_lat)) if c_lat else None
        if (lon is None or lat is None) and c_wkt:
            lon, lat = _wkt_point_lonlat(row.get(c_wkt))
        if lon is None or lat is None:
            continue
        saida.append({
            "codigo": (row.get(c_cod) or f"V-{len(saida)+1:04d}").strip(),
            "lon": lon, "lat": lat,
            "x": _f(row.get(c_x)) if c_x else None,
            "y": _f(row.get(c_y)) if c_y else None,
            "sigma_lon": _f(row.get(c_sx)) if c_sx else None,
            "sigma_lat": _f(row.get(c_sy)) if c_sy else None,
            "h": _f(row.get(c_z)) if c_z else None,
            "sigma_h": _f(row.get(c_sz)) if c_sz else None,
            "metodo": (row.get(c_met) or "").strip() if c_met else "",
        })
    return saida


def parse_limites_csv(conteudo: bytes):
    """Lê o CSV oficial de limites do SIGEF (só tem o NOME do confrontante;
    para nome+CNS+matrícula, use sessao.limites_detalhe())."""
    texto = conteudo.decode("utf-8-sig", errors="replace")
    sep = ";" if texto.count(";") >= texto.count(",") else ","
    leitor = csv.DictReader(io.StringIO(texto), delimiter=sep)

    def acha(campos, *chaves):
        chaves = [_norm(k) for k in chaves]
        for c in campos:
            cl = _norm(c)
            if any(cl == k or k in cl for k in chaves):
                return c
        return None

    campos = leitor.fieldnames or []
    c_de = acha(campos, "codigo_do_vertice", "do_vertice", "de_vertice", "do vertice", "codigo")
    c_ao = acha(campos, "ao_vertice", "ao vertice")
    c_tipo = acha(campos, "tipo")
    c_lado = acha(campos, "lado")
    c_conf = acha(campos, "confrontante", "confronta", "descritivo")
    c_cns = acha(campos, "cns")
    c_mat = acha(campos, "matricula", "matrícula")
    c_az = acha(campos, "azimute")
    c_comp = acha(campos, "comprimento", "distancia", "distância")

    saida = []
    for row in leitor:
        de = (row.get(c_de) or "").strip() if c_de else ""
        if not de:
            continue
        saida.append({
            "de_vertice": de,
            "ao_vertice": (row.get(c_ao) or "").strip() if c_ao else "",
            "tipo": (row.get(c_tipo) or "").strip() if c_tipo else "",
            "lado": (row.get(c_lado) or "").strip() if c_lado else "",
            "confrontante": (row.get(c_conf) or "").strip() if c_conf else "",
            "cns": (row.get(c_cns) or "").strip() if c_cns else "",
            "matricula": (row.get(c_mat) or "").strip() if c_mat else "",
            "azimute": (row.get(c_az) or "").strip() if c_az else "",
            "comprimento": (row.get(c_comp) or "").strip() if c_comp else "",
        })
    return saida


sessao = SigefSession()
