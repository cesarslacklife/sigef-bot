# -*- coding: utf-8 -*-
"""Interpretação das entradas do usuário: UUID, coordenada geográfica
(decimal ou DMS), coordenada UTM e código de vértice."""

import os
import re

UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)

# Ex.: FJML-P-0001 / INXX-M-0195
VERTICE_RE = re.compile(r"\b[A-Z0-9]{2,6}-[MPV]-\d{3,6}\b", re.I)

# DMS: -22°22'05,686"  (aceita º ° ' ’ " ” e vírgula ou ponto decimal)
DMS_RE = re.compile(
    r"(-?\d{1,3})\s*[°º]\s*(\d{1,2})\s*['’]\s*(\d{1,2}(?:[.,]\d+)?)\s*[\"”]?"
)

NUM_RE = re.compile(r"-?\d{1,9}(?:[.,]\d+)?")

FUSO_PADRAO = int(os.getenv("UTM_FUSO_PADRAO", "23"))


def detectar_uuid(texto: str):
    m = UUID_RE.search(texto or "")
    return m.group(0).lower() if m else None


def detectar_vertice(texto: str):
    t = (texto or "").strip()
    if UUID_RE.search(t):
        return None
    m = VERTICE_RE.search(t.upper())
    return m.group(0).upper() if m else None


def detectar_sncr(texto: str):
    """Código do imóvel no SNCR/CCIR: 13 dígitos (com ou sem pontuação).
    Ex.: 9511023531752 ou 951.102.353.175-2. Retorna só os dígitos."""
    t = (texto or "").strip()
    if UUID_RE.search(t):
        return None
    so_digitos = "".join(c for c in t if c.isdigit())
    if len(so_digitos) == 13:
        return so_digitos
    return None


def _num(s: str) -> float:
    return float(s.replace(".", "").replace(",", ".")) if ("," in s and "." in s) \
        else float(s.replace(",", "."))


def dms_para_decimal(g, m, s):
    g, m, s = float(g), float(m), _num(str(s))
    dec = abs(g) + m / 60.0 + s / 3600.0
    return -dec if g < 0 else dec


def detectar_coordenada(texto: str):
    """Retorna (lat, lon) em graus decimais SIRGAS2000/WGS84, ou None.

    Aceita:
      - DMS em par:  -22°22'05,686" -44°07'58,231"
      - Decimal:     -22.3683, -44.1328   (ordem lat, lon)
      - UTM:         615000 7524000  [fuso opcional: '23', '23S', '23K']
    """
    t = (texto or "").strip()
    if not t or UUID_RE.search(t):
        return None

    # 1) DMS (par)
    dms = DMS_RE.findall(t)
    if len(dms) >= 2:
        a = dms_para_decimal(*dms[0])
        b = dms_para_decimal(*dms[1])
        lat, lon = (a, b) if abs(a) <= 90 else (b, a)
        if abs(lat) <= 90 and abs(lon) <= 180:
            return (lat, lon)

    # 2) Números soltos
    nums = [_num(n) for n in NUM_RE.findall(t.replace("°", " "))]
    if len(nums) < 2:
        return None
    a, b = nums[0], nums[1]

    # 2a) Decimal geográfico
    if abs(a) <= 90 and abs(b) <= 180 and (abs(a) > 0.0001 or abs(b) > 0.0001):
        # plausível pro Brasil: lat negativa em geral
        if -35 <= a <= 6 and -75 <= b <= -28:
            return (a, b)
        if -35 <= b <= 6 and -75 <= a <= -28:  # usuário inverteu
            return (b, a)

    # 2b) UTM (E ~ 1e5..9e5 / N ~ 1e6..1.1e7)
    e, n = (a, b) if a < b else (b, a)
    if 100_000 <= e <= 900_000 and 1_000_000 <= n <= 11_000_000:
        fuso = FUSO_PADRAO
        m = re.search(r"\b(1[7-9]|2[0-5])\s*[A-Za-z]?\b", t)
        # só aceita como fuso se for número "pequeno" isolado (não confundir com coord)
        if m and len(m.group(0).strip()) <= 3:
            fuso = int(m.group(1))
        from pyproj import Transformer
        tr = Transformer.from_crs(31960 + fuso, 4674, always_xy=True)
        lon, lat = tr.transform(e, n)
        if -35 <= lat <= 6 and -75 <= lon <= -28:
            return (lat, lon)

    return None


def epsg_utm_sirgas(lon: float) -> int:
    """EPSG SIRGAS 2000 / UTM zona Sul a partir da longitude."""
    fuso = int((lon + 180) // 6) + 1
    return 31960 + fuso
