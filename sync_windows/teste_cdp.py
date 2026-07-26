# -*- coding: utf-8 -*-
"""Conecta no Chrome REAL (aberto com --remote-debugging-port=9222) e testa
os downloads do SIGEF usando a sessao que o Cesar logou com as proprias maos.

Pre-requisito: o Chrome precisa estar aberto assim, e ja logado no SIGEF:
  chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\Users\\Cesar\\chrome-sigef

Diferenca pro teste anterior: aqui o Playwright NAO abre navegador nenhum.
Ele se anexa a um Chrome comum, com perfil comum, ja autenticado por uma
pessoa. Nao ha mascaramento de fingerprint nem resolucao automatica de
challenge - so o reaproveitamento de uma sessao legitima.
"""

import os
from playwright.sync_api import sync_playwright

UUID = "a76543f4-e2cf-4141-8596-6bcbf7e072cf"
SAIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "teste_saida")

BASE = "https://sigef.incra.gov.br"
URL_CSV_VERT = f"{BASE}/geo/exportar/vertice/csv/{UUID}/"
URL_CSV_LIM = f"{BASE}/geo/exportar/limite/csv/{UUID}/"
URL_MEMORIAL = f"{BASE}/geo/parcela/memorial/{UUID}/"
URL_DETALHE = f"{BASE}/geo/parcela/detalhe/{UUID}/?limit=all"

os.makedirs(SAIDA, exist_ok=True)


def tentar(ctx, nome, url, extensao):
    """Baixa via requisicao no contexto do navegador e diz se passou."""
    print(f"\n[{nome}]")
    print(f"  {url}")
    try:
        resp = ctx.request.get(url)
        ct = resp.headers.get("content-type", "")
        corpo = resp.body()
        print(f"  status {resp.status} | {ct} | {len(corpo)} bytes")

        eh_html = "text/html" in ct
        eh_waf = b"bobcmn" in corpo[:5000] or b"TSPD" in corpo[:5000]

        if eh_waf:
            print("  >>> BLOQUEADO PELO WAF (script bobcmn/TSPD no corpo)")
            return False
        if eh_html and extensao != ".html":
            print("  >>> BLOQUEADO (veio HTML no lugar do arquivo)")
            print(f"  inicio: {corpo[:150].decode('utf-8', errors='replace')}")
            return False

        caminho = os.path.join(SAIDA, f"{nome}_{UUID[:8]}{extensao}")
        with open(caminho, "wb") as f:
            f.write(corpo)
        print(f"  >>> PASSOU! salvo em {caminho}")
        if extensao == ".csv":
            print(f"  1a linha: {corpo[:160].decode('utf-8', errors='replace')}")
        return True
    except Exception as e:
        print(f"  erro: {e}")
        return False


def main():
    with sync_playwright() as p:
        print("Conectando no Chrome da porta 9222...")
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        print(f"Conectado. {len(ctx.pages)} aba(s) aberta(s).")

        # Confere se a sessao esta viva antes de tentar os downloads
        print("\n[sessao] conferindo se esta logado...")
        r = ctx.request.get(f"{BASE}/consultar/parcelas/")
        logado = b"sair" in r.body().lower() or b"logout" in r.body().lower()
        print(f"  status {r.status} | parece logado: {logado}")

        resultados = {
            "vertices_csv": tentar(ctx, "vertices", URL_CSV_VERT, ".csv"),
            "limites_csv": tentar(ctx, "limites", URL_CSV_LIM, ".csv"),
            "memorial_pdf": tentar(ctx, "memorial", URL_MEMORIAL, ".pdf"),
            "detalhe_html": tentar(ctx, "detalhe", URL_DETALHE, ".html"),
        }

        print("\n" + "=" * 60)
        print("RESUMO")
        for k, v in resultados.items():
            print(f"  {k:16} {'PASSOU' if v else 'bloqueado'}")
        print("=" * 60)

        browser.close()  # so desconecta; nao fecha o Chrome do Cesar


if __name__ == "__main__":
    main()
