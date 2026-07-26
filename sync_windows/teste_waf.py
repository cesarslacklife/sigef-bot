# -*- coding: utf-8 -*-
"""Teste: navegador real (Playwright headful) consegue baixar do SIGEF pos-WAF?

Como funciona:
  1. Abre um Chromium VISIVEL com perfil persistente em ./perfil_sigef
  2. Voce loga no gov.br e resolve o que o WAF pedir (é você mesmo, no navegador)
  3. Aperta Enter no console
  4. O script tenta baixar o CSV de vertices usando o proprio contexto do
     navegador (nao exporta cookie nenhum pra fora)

O que estamos testando: se o WAF libera quando quem pede é um navegador de
verdade com sessao legitima. Se liberar, a via (a) esta provada.
"""

import os
from playwright.sync_api import sync_playwright

UUID = "a76543f4-e2cf-4141-8596-6bcbf7e072cf"
PERFIL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "perfil_sigef")
SAIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "teste_saida")

URL_LOGIN = "https://sigef.incra.gov.br/consultar/parcelas/"
URL_CSV = f"https://sigef.incra.gov.br/geo/exportar/vertice/csv/{UUID}/"
URL_MEMORIAL = f"https://sigef.incra.gov.br/geo/parcela/memorial/{UUID}/"

os.makedirs(SAIDA, exist_ok=True)


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PERFIL,
            headless=False,
            accept_downloads=True,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print("\n" + "=" * 70)
        print("Abrindo o SIGEF. Faca o login normalmente e navegue ate")
        print("conseguir consultar uma parcela (resolva o que o WAF pedir).")
        print("Quando estiver logado, volte aqui e aperte ENTER.")
        print("=" * 70 + "\n")

        page.goto(URL_LOGIN, wait_until="domcontentloaded")
        input(">>> Logado? Aperte ENTER para tentar o download... ")

        # --- Teste 1: CSV de vertices via requisicao do proprio navegador ---
        print("\n[1] Tentando o CSV de vertices (via contexto do navegador)...")
        try:
            resp = ctx.request.get(URL_CSV)
            print(f"    status: {resp.status}")
            print(f"    content-type: {resp.headers.get('content-type')}")
            corpo = resp.body()
            print(f"    tamanho: {len(corpo)} bytes")

            texto = corpo[:300].decode("utf-8", errors="replace")
            if "QRCODE" in texto or ";" in texto[:120]:
                caminho = os.path.join(SAIDA, f"vertices_{UUID[:8]}.csv")
                with open(caminho, "wb") as f:
                    f.write(corpo)
                print(f"    >>> PASSOU! CSV salvo em {caminho}")
                print(f"    primeiras linhas:\n{texto[:200]}")
            else:
                print("    >>> BLOQUEADO (veio HTML, nao CSV). Inicio do corpo:")
                print(f"    {texto[:200]}")
        except Exception as e:
            print(f"    erro: {e}")

        # --- Teste 2: memorial PDF via navegacao real com download ---
        print("\n[2] Tentando o memorial PDF (via navegacao real)...")
        try:
            with page.expect_download(timeout=30000) as dl_info:
                page.goto(URL_MEMORIAL)
            download = dl_info.value
            caminho = os.path.join(SAIDA, f"memorial_{UUID[:8]}.pdf")
            download.save_as(caminho)
            tam = os.path.getsize(caminho)
            print(f"    >>> PASSOU! PDF salvo ({tam} bytes) em {caminho}")
        except Exception as e:
            print(f"    nao baixou como download: {e}")
            print(f"    URL atual: {page.url}")
            if "consultar/parcelas" in page.url:
                print("    >>> BLOQUEADO (redirecionou pro /consultar/parcelas)")

        print("\n" + "=" * 70)
        print("Teste concluido. O navegador segue aberto pra voce inspecionar.")
        print("Aperte ENTER para fechar.")
        print("=" * 70)
        input()
        ctx.close()


if __name__ == "__main__":
    main()
