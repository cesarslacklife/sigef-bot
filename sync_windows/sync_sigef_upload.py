# -*- coding: utf-8 -*-
"""Carteiro: baixa o ZIP do Acervo Fundiario e entrega pro geoservice.

Roda sem janela (pythonw). Nao fala com o banco: so faz download e upload.
Todo o processamento acontece na VPS.
"""

import os
import sys
import tempfile
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

URL_ZIP = os.getenv("SYNC_URL_ZIP", "")
SYNC_API = os.getenv("SYNC_API", "")          # ex.: https://sigef-api.seudominio.com.br/sync/upload
SYNC_TOKEN = os.getenv("SYNC_TOKEN", "")
WEBHOOK_NOTIFY = os.getenv("WEBHOOK_NOTIFY", "")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.log")


def log(msg: str) -> None:
    linha = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(linha + "\n")
    print(linha)


def notificar(msg: str) -> None:
    if not WEBHOOK_NOTIFY:
        return
    try:
        requests.post(WEBHOOK_NOTIFY, json={"texto": msg}, timeout=30)
    except Exception as e:
        log(f"aviso: falha ao notificar ({e})")


def main() -> None:
    for nome, valor in (("SYNC_URL_ZIP", URL_ZIP), ("SYNC_API", SYNC_API),
                        ("SYNC_TOKEN", SYNC_TOKEN)):
        if not valor:
            raise RuntimeError(f"defina {nome} no .env")

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "sigef.zip")

        log(f"Baixando {URL_ZIP}")
        with requests.get(URL_ZIP, stream=True, timeout=600) as resp:
            resp.raise_for_status()
            with open(zip_path, "wb") as f:
                for pedaco in resp.iter_content(1024 * 256):
                    f.write(pedaco)
        mb = os.path.getsize(zip_path) / 1024 / 1024
        log(f"Download concluido: {mb:.1f} MB")

        log(f"Enviando para {SYNC_API}")
        with open(zip_path, "rb") as f:
            r = requests.post(
                SYNC_API,
                headers={"X-Sync-Token": SYNC_TOKEN},
                files={"arquivo": ("sigef.zip", f, "application/zip")},
                timeout=900,
            )
        r.raise_for_status()
        d = r.json()

    msg = (f"Base SIGEF-{d['uf']} atualizada: {d['depois']} parcelas "
           f"({d['novas']:+d} em relacao a anterior).")
    log(msg)
    notificar(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        erro = f"Sync SIGEF FALHOU: {e}"
        log(erro)
        notificar(erro)
        sys.exit(1)
