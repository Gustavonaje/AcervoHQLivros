#!/usr/bin/env python3
"""
Acompanhamento de pré-vendas e lançamentos da Panini (DC Comics e Panini Comics).

Roda uma vez por dia (GitHub Actions) e mantém data/lancamentos.json:
  - lê as categorias ordenadas por "Mais recentes", só até onde há pré-vendas;
  - item novo com a etiqueta "Pré-venda"        -> entra como "pre-venda";
  - item acompanhado que perdeu a etiqueta       -> "a-venda" (lançou), com a data;
  - item acompanhado que sumiu do site (404)     -> "fora-do-site".
Para cada item novo, abre a ficha e guarda referência, coleção, páginas,
volume anterior/próximo etc.

A Panini adia muito: perto do fim do mês, o que estava previsto para aquele
mês passa para o seguinte, na ficha ("Mês") e na faixa "Pré-venda com
previsão de envio para ...". Por isso a ficha das pré-vendas é relida:
  - todo dia, se a previsão é para o mês atual ou já passou;
  - uma vez por semana, nas demais.
Quando a previsão muda, o item guarda a primeira previsão
("previsao_original", aaaamm) e cada mudança em "mudancas_previsao".

O script NÃO conhece o seu acervo: o cruzamento com as suas séries é feito no app.

Uso:
  python panini_lancamentos.py --saida data/lancamentos.json
  python panini_lancamentos.py --saida teste.json --salvar-html debug/   # guarda o HTML lido
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
from urllib.parse import urljoin, urlencode, urlparse, parse_qs, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://panini.com.br"
CATEGORIAS = {
    "DC Comics": f"{BASE}/dc-comics",
    "Panini Comics": f"{BASE}/panini-comics",
}
POR_PAGINA = 36          # maior opção que o site oferece
MAX_PAGINAS = 12         # teto de segurança por categoria (12 x 36 = 432 itens)
PAUSA = 2.0              # segundos entre requisições
DIAS_MANTER_LANCADO = 60 # quanto tempo um lançado continua no arquivo
DIAS_MANTER_FORA = 30
DIAS_RELER = 7           # pré-venda com previsão para depois: ficha relida 1x por semana
MAX_RELEITURAS = 250     # teto de fichas relidas por execução (a 1ª vez relê todas)
USER_AGENT = ("Mozilla/5.0 (compatible; AcervoPessoal/1.0; "
              "acompanhamento pessoal de lancamentos, 1x por dia)")

RE_PREVENDA = re.compile(r"\bpr[eé]-?\s?venda\b(?!s)", re.I)
# "Pré-venda com previsão de envio para 2ª quinzena de Out/26."
RE_ENVIO = re.compile(r"previs[aã]o\s+de\s+envio\s*:?\s*(?:para\s+(?:(?:a|o)\s+)?)?([^.!\n]{3,60})", re.I)
MESES = ["janeiro", "fevereiro", "marco", "abril", "maio", "junho",
         "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


# ----------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------
def hoje():
    return dt.date.today().isoformat()


def agora():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def norm(txt):
    """Normalização usada também no app: minúsculas, sem acento, só letras/números."""
    t = unicodedata.normalize("NFKD", str(txt or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


RE_VARIANTE = re.compile(r"\s*[-–—]\s*(capa\s+variante.*|variante.*|capa\s+alternativa.*)$", re.I)
RE_NUMERO = re.compile(
    r"^(?P<serie>.*?)[\s,:\-–—]*"
    r"(?:\b(?:vol(?:ume)?|n[º°o])\.?\s*)?"
    r"\b(?P<num>\d{1,3})\b(?:\s*/\s*(?P<total>\d{1,4})|\s*\((?P<total2>\d{1,4})\))?"
    r"(?:\s+[-–—]\s+.*)?\s*$",
    re.I,
)


def analisar_titulo(titulo):
    """'Vingadores/LJA 04 - Capa Variante 1' -> serie, numero, total, variante."""
    t = re.sub(r"\s+", " ", titulo or "").strip()
    variante = None
    m = RE_VARIANTE.search(t)
    if m:
        variante = m.group(1).strip()
        t = t[: m.start()].strip()
    serie, numero, total = t, None, None
    m = RE_NUMERO.match(t)
    if m and m.group("serie").strip():
        serie = m.group("serie").strip(" ,:-–—")
        numero = int(m.group("num"))
        t2 = m.group("total") or m.group("total2")
        total = int(t2) if t2 else None
    return {
        "serie": serie,
        "serie_norm": norm(re.sub(r"\b(vol(ume)?|n[º°o])\.?\s*$", "", serie, flags=re.I)),
        "numero": numero,
        "numero_total": total,
        "variante": variante,
    }


def com_parametros(url, **params):
    p = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    q.update({k: str(v) for k, v in params.items() if v is not None})
    return urlunparse(p._replace(query=urlencode(q)))


def slug_de(url):
    return urlparse(url).path.strip("/")


def ler_envio(texto):
    """'... previsão de envio para 2ª quinzena de Out/26.' -> texto, mês, ano, quinzena."""
    m = RE_ENVIO.search(texto or "")
    if not m:
        return {}
    frase = re.sub(r"\s+", " ", m.group(1)).strip(" .,;")
    out = {"envio_txt": frase}
    t = norm(frase)  # "2a quinzena de out 26"
    d = re.search(r"\b\d{1,2}\s+(\d{1,2})\s+(\d{2}|\d{4})\b", t)  # 20/10/2026
    mm = re.search(r"\b(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-z]*\s+(?:de\s+)?(\d{2}|\d{4})\b", t)
    mes = ano = None
    if mm:
        mes = [x[:3] for x in MESES].index(mm.group(1)) + 1
        ano = int(mm.group(2))
    elif d and 1 <= int(d.group(1)) <= 12:
        mes, ano = int(d.group(1)), int(d.group(2))
    if mes and ano:
        out["envio_mes"] = mes
        out["envio_ano"] = ano + 2000 if ano < 100 else ano
        q = re.search(r"\b([12])\s*a?\s+quinzena\b", t)
        if q:
            out["envio_quinzena"] = int(q.group(1))
    return out


def chave_ficha(item):
    """aaaamm pelos campos Ano/Mês da ficha (sem mês: dezembro do ano)."""
    try:
        a = int(str(item.get("ano") or "").strip()[:4])
    except ValueError:
        return None
    m = norm(item.get("mes"))
    return a * 100 + (MESES.index(m) + 1 if m in MESES else 12)


def chave_envio(item):
    a, m = item.get("envio_ano"), item.get("envio_mes")
    return a * 100 + m if a and m else None


def chave_prev(item):
    """A previsão que vale: a faixa de envio, se houver; senão a ficha."""
    return chave_envio(item) or chave_ficha(item)


def rot_chave(c):
    return f"{MESES[c % 100 - 1][:3]}/{str(c // 100)[2:]}" if c else "?"


# ----------------------------------------------------------------------------
# rede
# ----------------------------------------------------------------------------
class Cliente:
    def __init__(self, pasta_html=None):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9"})
        self.pasta_html = pasta_html
        self.n = 0
        self._ultima = 0.0

    def get(self, url):
        espera = PAUSA - (time.time() - self._ultima)
        if espera > 0:
            time.sleep(espera)
        self._ultima = time.time()
        r = self.s.get(url, timeout=30)
        self.n += 1
        if self.pasta_html:
            os.makedirs(self.pasta_html, exist_ok=True)
            nome = re.sub(r"[^a-z0-9]+", "_", url.lower())[-120:] + ".html"
            with open(os.path.join(self.pasta_html, nome), "w", encoding="utf-8") as f:
                f.write(r.text)
        return r


# ----------------------------------------------------------------------------
# leitura da listagem
# ----------------------------------------------------------------------------
def achar_ordem_recentes(soup):
    """Descobre no próprio seletor 'Ordenar por' o código de 'Mais recentes'."""
    for opt in soup.select("select option"):
        if "recente" in norm(opt.get_text()):
            return opt.get("value")
    return None


def total_produtos(soup):
    m = re.search(r"Produtos\s+\d+\s*-\s*\d+\s+de\s+(\d+)", soup.get_text(" "))
    return int(m.group(1)) if m else None


def itens_da_listagem(soup, categoria):
    """Cada produto da grade: título, link, preço, capa, se está em pré-venda."""
    itens, vistos = [], set()
    for link in soup.select("a.product-item-link"):
        url = urljoin(BASE, link.get("href", ""))
        if not url or url in vistos:
            continue
        vistos.add(url)
        caixa = link.find_parent("li") or link.find_parent(class_=re.compile("product-item"))
        if caixa is None:
            continue
        preco = caixa.select_one('[data-price-type="finalPrice"] .price') or caixa.select_one(".price")
        img = caixa.select_one("img.product-image-photo") or caixa.select_one("img")
        itens.append({
            "slug": slug_de(url),
            "url": url,
            "titulo": link.get_text(" ", strip=True),
            "categoria": categoria,
            "preco": preco.get_text(strip=True) if preco else None,
            "capa": (img.get("src") or img.get("data-src")) if img else None,
            "pre_venda": bool(RE_PREVENDA.search(caixa.get_text(" "))),
        })
    return itens


def varrer_categoria(cli, categoria, url_base, acompanhados):
    """Lê as páginas mais recentes até não haver mais pré-vendas nem itens acompanhados."""
    r = cli.get(com_parametros(url_base, product_list_limit=POR_PAGINA))
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    ordem = achar_ordem_recentes(soup)
    if ordem:
        r = cli.get(com_parametros(url_base, product_list_limit=POR_PAGINA,
                                   product_list_order=ordem, product_list_dir="desc"))
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    total = total_produtos(soup)
    todos = []
    pagina = 1
    while True:
        itens = itens_da_listagem(soup, categoria)
        if not itens:
            break
        todos.extend(itens)
        tem_interesse = any(i["pre_venda"] or i["slug"] in acompanhados for i in itens)
        if not tem_interesse or pagina >= MAX_PAGINAS:
            break
        if total and pagina * POR_PAGINA >= total:
            break
        pagina += 1
        r = cli.get(com_parametros(url_base, product_list_limit=POR_PAGINA,
                                   product_list_order=ordem, product_list_dir="desc" if ordem else None,
                                   p=pagina))
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
    info = {"categoria": categoria, "ordem_usada": ordem or "padrão do site",
            "paginas_lidas": pagina, "total_na_categoria": total, "itens_lidos": len(todos)}
    return todos, info


# ----------------------------------------------------------------------------
# leitura da ficha do produto
# ----------------------------------------------------------------------------
CAMPOS_FICHA = {
    "referencia": ("referencia", "sku", "codigo"),
    "autores": ("autor", "autores"),
    "ano": ("ano", "ano de publicacao"),
    "mes": ("mes",),
    "paginas": ("paginas", "numero de paginas", "quantidade de paginas"),
    "encadernacao": ("encadernacao", "tipo de capa"),
    "colecao": ("colecao",),
    "tipo_publicacao": ("tipo de publicacao",),
    "conteudo_original": ("conteudo", "publicacao original", "material original"),
}


def ler_ficha(html):
    soup = BeautifulSoup(html, "html.parser")
    ficha = {}
    sku = soup.select_one('[itemprop="sku"]') or soup.select_one(".product.attribute.sku .value")
    if sku:
        ficha["referencia"] = sku.get_text(strip=True)
    for td in soup.select("#product-attribute-specs-table td, table.additional-attributes td"):
        rotulo = td.get("data-th")
        if not rotulo:
            th = td.find_previous_sibling("th")
            rotulo = th.get_text(strip=True) if th else ""
        r = norm(rotulo)
        for chave, apelidos in CAMPOS_FICHA.items():
            if r in apelidos and chave not in ficha:
                ficha[chave] = td.get_text(" ", strip=True)
    if "paginas" in ficha:
        m = re.search(r"\d+", ficha["paginas"])
        ficha["paginas"] = int(m.group()) if m else None
    for rotulo, chave in (("volume anterior", "volume_anterior"), ("proximo volume", "proximo_volume")):
        no = soup.find(string=lambda s: s and norm(s) == rotulo)
        if no:
            a = no.find_next("a", href=True)
            if a:
                ficha[chave] = slug_de(urljoin(BASE, a["href"]))
    area = soup.select_one(".product-info-main")
    galeria = soup.select_one(".product.media")
    if area is not None or galeria is not None:
        texto = " ".join(x.get_text(" ") for x in (area, galeria) if x is not None)
        ficha["_pre_venda"] = bool(RE_PREVENDA.search(texto))
        if area is not None:
            ficha.update(ler_envio(area.get_text(" ")))
    else:
        ficha["_pre_venda"] = None  # não deu para saber
    return ficha


# ----------------------------------------------------------------------------
# estado
# ----------------------------------------------------------------------------
def carregar(caminho):
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    return {"versao": 1, "itens": []}


CAMPOS_ENVIO = ("envio_txt", "envio_mes", "envio_ano", "envio_quinzena")


def aplicar_previsao(item, ficha):
    """Atualiza ano/mês e a faixa de envio com a ficha relida. Se a previsão
    mudou, guarda a primeira e registra a mudança. Devolve (antes, depois)
    quando mudou, senão None."""
    ant_ficha, ant_envio = chave_ficha(item), chave_envio(item)
    for k in ("ano", "mes"):
        if ficha.get(k):           # campo vazio na releitura não apaga o que havia
            item[k] = ficha[k]
    for k in CAMPOS_ENVIO:         # a faixa some quando a Panini tira: acompanha
        if ficha.get(k) is not None:
            item[k] = ficha[k]
        else:
            item.pop(k, None)
    item["ficha_lida_em"] = hoje()
    # compara a mesma fonte antes e depois (faixa com faixa, ficha com ficha),
    # para não confundir "envio" com "mês de lançamento"
    nov_envio = chave_envio(item)
    antes, depois = (ant_envio, nov_envio) if ant_envio and nov_envio else (ant_ficha, chave_ficha(item))
    if antes and depois and antes != depois:
        item.setdefault("previsao_original", antes)
        item.setdefault("mudancas_previsao", []).append({"de": antes, "para": depois, "em": hoje()})
        return antes, depois
    return None


def precisa_reler(item, chave_hoje):
    if item.get("estado") != "pre-venda":
        return False
    lida = item.get("ficha_lida_em")
    if not lida:
        return True                # registro de antes desta versão
    if lida >= hoje():
        return False               # já relida hoje
    c = chave_prev(item)
    if c is None or c <= chave_hoje:
        return True                # vence este mês ou já passou: é quando a Panini adia
    return (dt.date.today() - dt.date.fromisoformat(lida)).days >= DIAS_RELER


def podar(itens):
    limite_lanc = (dt.date.today() - dt.timedelta(days=DIAS_MANTER_LANCADO)).isoformat()
    limite_fora = (dt.date.today() - dt.timedelta(days=DIAS_MANTER_FORA)).isoformat()
    fica = []
    for i in itens:
        if i["estado"] == "a-venda" and (i.get("lancou_em") or "") < limite_lanc:
            continue
        if i["estado"] == "fora-do-site" and (i.get("saiu_em") or "") < limite_fora:
            continue
        fica.append(i)
    return fica


def executar(saida, pasta_html=None):
    dados = carregar(saida)
    por_slug = {i["slug"]: i for i in dados.get("itens", [])}
    acompanhados = {s for s, i in por_slug.items() if i["estado"] == "pre-venda"}
    cli = Cliente(pasta_html)
    relatorio, vistos = [], {}

    for categoria, url in CATEGORIAS.items():
        try:
            itens, info = varrer_categoria(cli, categoria, url, acompanhados)
        except requests.RequestException as e:
            info = {"categoria": categoria, "erro": str(e)}
            itens = []
        relatorio.append(info)
        for it in itens:
            vistos.setdefault(it["slug"], it)

    novos = lancados = fora = 0
    adiados = []
    for slug, it in vistos.items():
        atual = por_slug.get(slug)
        if atual is None:
            if not it["pre_venda"]:
                continue  # só acompanhamos o que foi visto em pré-venda
            novo = {**it, **analisar_titulo(it["titulo"]),
                    "estado": "pre-venda", "pre_venda_desde": hoje(), "lancou_em": None}
            novo.pop("pre_venda", None)
            try:
                r = cli.get(it["url"])
                if r.status_code == 200:
                    ficha = ler_ficha(r.text)
                    ficha.pop("_pre_venda", None)
                    novo.update(ficha)
                    novo["ficha_lida_em"] = hoje()
            except requests.RequestException:
                pass
            por_slug[slug] = novo
            novos += 1
        else:
            atual.update({"preco": it["preco"] or atual.get("preco"),
                          "capa": it["capa"] or atual.get("capa"),
                          "titulo": it["titulo"]})
            if atual["estado"] == "pre-venda" and not it["pre_venda"]:
                atual["estado"] = "a-venda"
                atual["lancou_em"] = hoje()
                lancados += 1
        por_slug[slug]["visto_em"] = hoje()

    # acompanhados que não apareceram na varredura: confere a ficha
    for slug in acompanhados - set(vistos):
        item = por_slug[slug]
        try:
            r = cli.get(item["url"])
        except requests.RequestException:
            continue
        if r.status_code == 404:
            item["estado"], item["saiu_em"] = "fora-do-site", hoje()
            fora += 1
        elif r.status_code == 200:
            ficha = ler_ficha(r.text)
            pv = ficha.get("_pre_venda")
            if pv is False:
                item["estado"], item["lancou_em"] = "a-venda", hoje()
                lancados += 1
            else:
                mud = aplicar_previsao(item, ficha)
                if mud:
                    adiados.append((item, *mud))
            item["visto_em"] = hoje()

    # releitura das fichas das pré-vendas (a Panini muda a previsão sem aviso)
    chave_hoje = dt.date.today().year * 100 + dt.date.today().month
    fila = [i for i in por_slug.values() if precisa_reler(i, chave_hoje)]
    fila.sort(key=lambda i: (chave_prev(i) or 0, i.get("ficha_lida_em") or ""))  # vencidas primeiro
    relidas = 0
    for item in fila[:MAX_RELEITURAS]:
        try:
            r = cli.get(item["url"])
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue               # 404 e lançamento ficam para a varredura de amanhã
        relidas += 1
        mud = aplicar_previsao(item, ler_ficha(r.text))
        if mud:
            adiados.append((item, *mud))

    itens = podar(list(por_slug.values()))
    itens.sort(key=lambda i: (i["estado"] != "pre-venda", i.get("lancou_em") or "", i.get("pre_venda_desde") or ""),
               reverse=False)
    saida_json = {
        "versao": 1,
        "gerado_em": agora(),
        "fonte": list(CATEGORIAS.values()),
        "execucao": {"requisicoes": cli.n, "novos": novos, "lancados": lancados,
                     "fora_do_site": fora, "fichas_relidas": relidas,
                     "fichas_na_fila": len(fila), "previsoes_mudadas": len(adiados),
                     "categorias": relatorio},
        "itens": itens,
    }
    os.makedirs(os.path.dirname(os.path.abspath(saida)), exist_ok=True)
    with open(saida, "w", encoding="utf-8") as f:
        json.dump(saida_json, f, ensure_ascii=False, indent=1)
    saida_json["_adiados"] = [(i["titulo"], a, d) for i, a, d in adiados]
    return saida_json


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--saida", default="data/lancamentos.json")
    ap.add_argument("--salvar-html", metavar="PASTA", help="guarda o HTML de cada página lida (diagnóstico)")
    a = ap.parse_args()
    res = executar(a.saida, a.salvar_html)
    ex = res["execucao"]
    print(f"{len(res['itens'])} itens no arquivo | novos {ex['novos']} | lançados {ex['lancados']} | "
          f"fora do site {ex['fora_do_site']} | fichas relidas {ex['fichas_relidas']} | "
          f"{ex['requisicoes']} requisições")
    for titulo, a, d in res["_adiados"]:
        print(f"   previsão mudou: {titulo}: {rot_chave(a)} -> {rot_chave(d)}")
    for c in ex["categorias"]:
        print("  ", c)
    em_prevenda = sum(1 for i in res["itens"] if i["estado"] == "pre-venda")
    if em_prevenda == 0:
        print("AVISO: nenhuma pré-venda encontrada — o layout do site pode ter mudado. "
              "Rode com --salvar-html e confira.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
