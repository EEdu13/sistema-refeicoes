# -*- coding: utf-8 -*-
"""Fechamento por fornecedor, pedido em português no Telegram.

A Elaine escreve como falaria — "fechamento 1 a 15 de setembro do
restaurante carreiro" — e recebe de volta a contagem diária para conferir
contra a folha que o fornecedor mantém no balcão.

O formato de saída é uma matriz dia x refeição de propósito: a folha do
fornecedor é uma contagem por dia, então a comparação tem que ser linha a
linha, não uma lista corrida de pedidos.

Não fala com banco nem com Telegram: interpreta texto, formata resultado.
Quem consulta e quem envia é o server.
"""

import re
import unicodedata
from datetime import date, datetime, timedelta

MESES = {
    'janeiro': 1, 'jan': 1, 'fevereiro': 2, 'fev': 2, 'marco': 3, 'mar': 3,
    'abril': 4, 'abr': 4, 'maio': 5, 'mai': 5, 'junho': 6, 'jun': 6,
    'julho': 7, 'jul': 7, 'agosto': 8, 'ago': 8, 'setembro': 9, 'set': 9,
    'outubro': 10, 'out': 10, 'novembro': 11, 'nov': 11,
    'dezembro': 12, 'dez': 12,
}

DIAS_SEMANA = ['seg', 'ter', 'qua', 'qui', 'sex', 'sab', 'dom']

# Colunas fixas na matriz. O resto cai em "outros" — melhor uma coluna
# genérica do que a tabela crescer e quebrar a largura no celular.
FAMILIAS = ['CAFÉ', 'ALMOÇO', 'JANTA']

# Nomes que o app grava quando ninguém escolheu de verdade. Sugerir isso
# como fornecedor é ruído.
LIXO = {'A DEFINIR', 'ESCOLHA O FORNECEDOR', 'OUTRO', 'OUTROS', 'SELVA',
        'VIAGEM', 'N/A', '-', '--'}


def _sem_acento(t):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(t or ''))
                   if not unicodedata.combining(c))


def _chave(t):
    """Normaliza para comparar: sem acento, só letras e números."""
    return ''.join(c for c in _sem_acento(t).upper() if c.isalnum())


def familia_refeicao(tipo):
    t = _sem_acento(tipo).upper()
    for f in FAMILIAS:
        if t.startswith(_sem_acento(f).upper()):
            return f
    return 'OUTROS'


def moeda(v):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f'{v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


# --------------------------------------------------------------------------
# Interpretação do comando
# --------------------------------------------------------------------------
def _ano_provavel(mes, hoje):
    """Mês sem ano: assume o ano que deixa a data mais perto de hoje.

    Em janeiro, "dezembro" quase sempre quer dizer o dezembro que passou,
    não o que vem daqui a onze meses.
    """
    if mes - hoje.month > 6:
        return hoje.year - 1
    if hoje.month - mes > 6:
        return hoje.year + 1
    return hoje.year


def interpretar_periodo(texto, hoje=None):
    """Acha o período no texto. Devolve (de, ate, sobra) — datas ou None."""
    hoje = hoje or date.today()
    t = ' ' + _sem_acento(texto).lower() + ' '

    def limpar(padrao, alvo):
        return re.sub(padrao, ' ', alvo, count=1)

    # 1) dd/mm[/aaaa] a dd/mm[/aaaa]
    m = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*(?:a|ate|-|as)\s*'
                  r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?', t)
    if m:
        d1, m1, a1, d2, m2, a2 = m.groups()
        a1 = int(a1) if a1 else hoje.year
        a2 = int(a2) if a2 else a1
        a1 += 2000 if a1 < 100 else 0
        a2 += 2000 if a2 < 100 else 0
        try:
            return (date(a1, int(m1), int(d1)), date(a2, int(m2), int(d2)),
                    limpar(re.escape(m.group(0)), t))
        except ValueError:
            return None, None, t

    # 2) "1 a 15 de setembro [de 2026]"
    nomes = '|'.join(MESES)
    m = re.search(rf'(\d{{1,2}})\s*(?:a|ao|ate|-)\s*(\d{{1,2}})\s*'
                  rf'(?:de\s+)?({nomes})\b(?:\s+de\s+(\d{{4}}))?', t)
    if m:
        d1, d2, mes_nome, ano = m.groups()
        mes = MESES[mes_nome]
        ano = int(ano) if ano else _ano_provavel(mes, hoje)
        try:
            return (date(ano, mes, int(d1)), date(ano, mes, int(d2)),
                    limpar(re.escape(m.group(0)), t))
        except ValueError:
            return None, None, t

    # 3) "setembro [de 2026]" — mês inteiro
    m = re.search(rf'\b({nomes})\b(?:\s+de\s+(\d{{4}}))?', t)
    if m:
        mes = MESES[m.group(1)]
        ano = int(m.group(2)) if m.group(2) else _ano_provavel(mes, hoje)
        primeiro = date(ano, mes, 1)
        ultimo = (date(ano + (mes == 12), (mes % 12) + 1, 1) - timedelta(days=1))
        return primeiro, ultimo, limpar(re.escape(m.group(0)), t)

    # 4) dia único: dd/mm[/aaaa]
    m = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?', t)
    if m:
        d, mes, ano = m.groups()
        ano = int(ano) if ano else hoje.year
        ano += 2000 if ano < 100 else 0
        try:
            um = date(ano, int(mes), int(d))
            return um, um, limpar(re.escape(m.group(0)), t)
        except ValueError:
            return None, None, t

    # 5) atalhos
    for palavra, (de, ate) in {
        'hoje': (hoje, hoje),
        'ontem': (hoje - timedelta(days=1), hoje - timedelta(days=1)),
        'esta semana': (hoje - timedelta(days=hoje.weekday()), hoje),
        'semana passada': (hoje - timedelta(days=hoje.weekday() + 7),
                           hoje - timedelta(days=hoje.weekday() + 1)),
        'este mes': (hoje.replace(day=1), hoje),
    }.items():
        if palavra in t:
            return de, ate, limpar(re.escape(palavra), t)

    return None, None, t


PALAVRAS_SOLTAS = {'fechamento', 'relatorio', 'historico', 'do', 'da', 'de',
                   'dos', 'das', 'no', 'na', 'em', 'para', 'pra', 'o', 'a',
                   'os', 'as', 'com', 'fornecedor', 'periodo', 'entre', 'e'}


def interpretar(texto, hoje=None):
    """Devolve (busca_fornecedor, de, ate). Datas podem vir None."""
    limpo = re.sub(r'^/\w+(@\S+)?', ' ', str(texto or '').strip())
    de, ate, sobra = interpretar_periodo(limpo, hoje)

    palavras = [p for p in re.split(r'\s+', sobra.strip()) if p]
    busca = ' '.join(p for p in palavras if p not in PALAVRAS_SOLTAS).strip()

    if de and ate and de > ate:
        de, ate = ate, de
    return busca, de, ate


# --------------------------------------------------------------------------
# Casamento do fornecedor
# --------------------------------------------------------------------------
def casar_fornecedor(busca, candidatos):
    """Ordena os fornecedores que combinam com o que foi digitado.

    Nome de fornecedor no banco é texto livre — vem com caixa trocada e
    grafia variando. Casar por pedaço, e não exato, é o que faz "carreiro"
    achar "RESTAURANTE CARREIRO".
    """
    alvo = _chave(busca)
    if not alvo:
        return []

    termos = [_chave(p) for p in busca.split() if len(_chave(p)) >= 3]
    achados = []

    for nome, quantos in candidatos:
        if _sem_acento(nome).strip().upper() in LIXO:
            continue
        chave = _chave(nome)
        if chave == alvo:
            peso = 0
        elif alvo and alvo in chave:
            peso = 1
        elif chave and chave in alvo and len(chave) >= 0.6 * len(alvo):
            # O nome cadastrado cabe dentro do que foi digitado. Só vale se
            # cobrir a maior parte: senão "pizzaria do joao" casa com um
            # fornecedor chamado "Joao" e a palavra que importava some.
            peso = 2
        elif termos and all(t in chave for t in termos):
            peso = 3
        elif termos and any(t in chave for t in termos):
            peso = 4
        else:
            continue
        achados.append((peso, -quantos, nome))

    achados.sort()
    return [(peso, nome) for peso, _, nome in achados]


# Até aqui o casamento é confiável: nome igual, um contendo o outro, ou
# todos os termos presentes. Acima disso é só "alguma palavra bateu" — e
# foi assim que "pizzaria do joao" achou um fornecedor chamado "Joao".
# Numa conferência de dinheiro, palpite fraco vira pergunta, nunca resposta.
PESO_CONFIAVEL = 3


# --------------------------------------------------------------------------
# Montagem do resultado
# --------------------------------------------------------------------------
def montar(fornecedor, de, ate, linhas):
    """Texto pronto para o Telegram (HTML). `linhas` = pedidos do período."""
    if not linhas:
        return (f'📋 <b>{_esc(fornecedor)}</b>\n'
                f'{de.strftime("%d/%m/%Y")} a {ate.strftime("%d/%m/%Y")}\n\n'
                f'Nenhum pedido neste fornecedor no período.')

    dias = {}
    unitarios = {}
    equipes = set()
    reprovados = 0
    total_geral = 0.0

    for p in linhas:
        situacao = str(p.get('APROVADO') or '').strip().upper()
        if situacao == 'REPROVADO':
            reprovados += 1
            continue

        d = p.get('DATA_RETIRADA')
        if hasattr(d, 'date'):
            d = d.date()
        fam = familia_refeicao(p.get('TIPO_REFEICAO'))

        try:
            qtd = int(p.get('TOTAL_COLABORADORES') or 0)
        except (TypeError, ValueError):
            qtd = 0
        try:
            valor = float(p.get('TOTAL_PAGAR') or 0)
        except (TypeError, ValueError):
            valor = 0.0

        linha = dias.setdefault(d, {'total': 0.0})
        linha[fam] = linha.get(fam, 0) + qtd
        linha['total'] += valor
        total_geral += valor

        if p.get('LIDER'):
            equipes.add(str(p['LIDER']))
        unitarios.setdefault(str(p.get('TIPO_REFEICAO') or fam), set()).add(
            round(float(p.get('VALOR_PAGO') or 0), 2))

    if not dias:
        return (f'📋 <b>{_esc(fornecedor)}</b>\n'
                f'{de.strftime("%d/%m/%Y")} a {ate.strftime("%d/%m/%Y")}\n\n'
                f'Os {reprovados} pedido(s) do período foram todos reprovados.')

    # Só mostra colunas que têm movimento — evita coluna vazia ocupando
    # largura preciosa na tela do celular.
    colunas = [f for f in FAMILIAS + ['OUTROS']
               if any(l.get(f) for l in dias.values())]

    cab = 'DIA        ' + ''.join(f'{c[:6]:>7}' for c in colunas) + f'{"TOTAL":>11}'
    corpo = [cab, '─' * len(cab)]

    somas = {c: 0 for c in colunas}
    for d in sorted(dias):
        l = dias[d]
        marca = f'{d.strftime("%d/%m")} {DIAS_SEMANA[d.weekday()]}'
        celulas = ''
        for c in colunas:
            n = l.get(c, 0)
            somas[c] += n
            celulas += f'{n if n else "-":>7}'
        corpo.append(f'{marca:<11}{celulas}{moeda(l["total"]):>11}')

    corpo.append('─' * len(cab))
    corpo.append(f'{"TOTAL":<11}'
                 + ''.join(f'{somas[c]:>7}' for c in colunas)
                 + f'{moeda(total_geral):>11}')

    partes = [
        f'📋 <b>FECHAMENTO — {_esc(fornecedor)}</b>',
        f'{de.strftime("%d/%m/%Y")} a {ate.strftime("%d/%m/%Y")} · '
        f'{len(dias)} dia(s) com pedido',
        '',
        '<pre>' + '\n'.join(corpo) + '</pre>',
    ]

    # Preço unitário: se o mesmo item saiu por valores diferentes no
    # período, isso aparece — é justamente o tipo de divergência que a
    # conferência com a folha do fornecedor precisa pegar.
    precos = []
    for tipo in sorted(unitarios):
        vals = sorted(v for v in unitarios[tipo] if v)
        if not vals:
            continue
        marca = ' / '.join('R$ ' + moeda(v) for v in vals)
        precos.append(f'{tipo[:22]:<22} {marca}'
                      + ('   ⚠️ variou' if len(vals) > 1 else ''))
    if precos:
        partes += ['', '<b>VALORES UNITÁRIOS</b>', '<pre>' + '\n'.join(precos) + '</pre>']

    total_refeicoes = sum(somas.values())
    partes.append(f'💰 <b>{total_refeicoes} refeições · R$ {moeda(total_geral)}</b>')

    if equipes:
        partes.append(f'👥 Equipes: {", ".join(sorted(equipes))}')
    if reprovados:
        partes.append(f'\n❌ {reprovados} pedido(s) reprovado(s) '
                      f'não entraram na conta.')

    return '\n'.join(partes)


def _esc(v):
    return (str(v if v is not None else '')
            .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def partir(texto, limite=3900):
    """Telegram corta em 4096. Quebra em pedaços fechando o <pre>."""
    if len(texto) <= limite:
        return [texto]

    pedacos, atual, dentro_pre = [], [], False
    for linha in texto.split('\n'):
        if len('\n'.join(atual)) + len(linha) + 1 > limite and atual:
            if dentro_pre:
                atual.append('</pre>')
            pedacos.append('\n'.join(atual))
            atual = ['<pre>'] if dentro_pre else []
        if linha.startswith('<pre>'):
            dentro_pre = True
        if linha.endswith('</pre>'):
            dentro_pre = False
        atual.append(linha)

    if atual:
        pedacos.append('\n'.join(atual))
    return pedacos
