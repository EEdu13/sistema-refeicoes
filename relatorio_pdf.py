# -*- coding: utf-8 -*-
"""Relatório diário de refeições em PDF, para a gerência.

Recebe as linhas cruas de PEDIDOS e devolve os bytes do PDF. Não fala com
banco nem com Telegram de propósito: quem busca os dados é o server, quem
envia é o server. Assim dá para gerar e conferir um relatório sem subir
nada.

O PDF sai em duas partes:
  - página 1: capa com os totais e o consolidado por equipe. É essa que o
    Telegram mostra como miniatura no grupo, então ela carrega o resumo;
  - páginas seguintes: o detalhe por projeto > equipe > refeição, com
    fornecedor, valor unitário e o nome de quem comeu.
"""

from datetime import datetime
from io import BytesIO

import pytz
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether,
                                PageBreak, PageTemplate, Paragraph, Spacer,
                                Table, TableStyle)

# Helvetica é embutida no reportlab: texto vetorial, sem depender de fonte
# instalada no sistema. Sai igual aqui e no Railway. Em compensação não tem
# emoji — por isso os status são etiquetas coloridas, não ícones.
FONTE = 'Helvetica'
FONTE_N = 'Helvetica-Bold'

TINTA = colors.HexColor('#0f172a')       # texto principal
TINTA_FRACA = colors.HexColor('#64748b')  # rótulos e apoio
LINHA = colors.HexColor('#e2e8f0')       # divisórias
FUNDO_SUAVE = colors.HexColor('#f8fafc')
VERDE = colors.HexColor('#15803d')
VERMELHO = colors.HexColor('#b91c1c')
AMBAR = colors.HexColor('#b45309')

# Cada refeição ganha sua cor para bater o olho e achar no meio da página.
CORES_REFEICAO = {
    'CAFÉ': colors.HexColor('#b45309'),
    'ALMOÇO': colors.HexColor('#15803d'),
    'JANTA': colors.HexColor('#4338ca'),
    'OUTRO': colors.HexColor('#475569'),
}
ORDEM_REFEICAO = ['CAFÉ', 'ALMOÇO', 'JANTA', 'OUTRO']

MARGEM = 14 * mm
LARGURA_UTIL = A4[0] - 2 * MARGEM

MESES = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho',
         'agosto', 'setembro', 'outubro', 'novembro', 'dezembro']
DIAS = ['Segunda-feira', 'Terça-feira', 'Quarta-feira', 'Quinta-feira',
        'Sexta-feira', 'Sábado', 'Domingo']


# --------------------------------------------------------------------------
# Formatação
# --------------------------------------------------------------------------
def moeda(v):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0.0
    return 'R$ ' + f'{v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def esc(v):
    """Paragraph do reportlab lê marcação tipo XML, então escapa."""
    return (str(v if v is not None else '')
            .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _titulo_pessoa(nome):
    """MARIA DA SILVA -> Maria da Silva. Bloco de nome maiúsculo cansa."""
    miudas = {'da', 'de', 'do', 'das', 'dos', 'e'}
    partes = []
    for i, p in enumerate(str(nome or '').strip().split()):
        baixo = p.lower()
        partes.append(baixo if (i and baixo in miudas) else baixo.capitalize())
    return ' '.join(partes)


def _familia_refeicao(tipo):
    """'ALMOÇO MARMITEX' e 'ALMOÇO LOCAL' contam como a mesma família."""
    t = str(tipo or '').upper()
    for f in ('CAFÉ', 'CAFE', 'ALMOÇO', 'ALMOCO', 'JANTA'):
        if t.startswith(f):
            return {'CAFE': 'CAFÉ', 'ALMOCO': 'ALMOÇO'}.get(f, f)
    return 'OUTRO'


def _data_extenso(data_iso):
    try:
        d = datetime.strptime(data_iso, '%Y-%m-%d')
        return f'{DIAS[d.weekday()]}, {d.day} de {MESES[d.month - 1]} de {d.year}'
    except (ValueError, TypeError):
        return data_iso


def _data_br(data_iso):
    try:
        return '/'.join(reversed(str(data_iso).split('-')))
    except Exception:
        return str(data_iso)


# --------------------------------------------------------------------------
# Estilos
# --------------------------------------------------------------------------
def _estilos():
    base = dict(fontName=FONTE, textColor=TINTA, leading=11, fontSize=8.5)
    return {
        'capa_titulo': ParagraphStyle('ct', fontName=FONTE_N, fontSize=26,
                                      leading=30, textColor=TINTA),
        'capa_sub': ParagraphStyle('cs', fontName=FONTE, fontSize=11,
                                   leading=15, textColor=TINTA_FRACA),
        'kpi_rot': ParagraphStyle('kr', fontName=FONTE_N, fontSize=7.5,
                                  leading=10, textColor=TINTA_FRACA,
                                  alignment=TA_CENTER),
        'kpi_val': ParagraphStyle('kv', fontName=FONTE_N, fontSize=19,
                                  leading=23, textColor=TINTA,
                                  alignment=TA_CENTER),
        'secao': ParagraphStyle('sc', fontName=FONTE_N, fontSize=11,
                                leading=14, textColor=TINTA),
        'th': ParagraphStyle('th', fontName=FONTE_N, fontSize=7.5, leading=10,
                             textColor=TINTA_FRACA),
        'th_d': ParagraphStyle('thd', fontName=FONTE_N, fontSize=7.5,
                               leading=10, textColor=TINTA_FRACA,
                               alignment=TA_RIGHT),
        'td': ParagraphStyle('td', **base),
        'td_d': ParagraphStyle('tdd', alignment=TA_RIGHT, **base),
        'projeto': ParagraphStyle('pj', fontName=FONTE_N, fontSize=13,
                                  leading=16, textColor=colors.white),
        'projeto_v': ParagraphStyle('pv', fontName=FONTE_N, fontSize=13,
                                    leading=16, textColor=colors.white,
                                    alignment=TA_RIGHT),
        'equipe': ParagraphStyle('eq', fontName=FONTE_N, fontSize=10.5,
                                 leading=14, textColor=TINTA),
        'equipe_v': ParagraphStyle('ev', fontName=FONTE_N, fontSize=10.5,
                                   leading=14, textColor=TINTA,
                                   alignment=TA_RIGHT),
        'refeicao': ParagraphStyle('rf', fontName=FONTE_N, fontSize=8.5,
                                   leading=11, textColor=colors.white),
        'forn': ParagraphStyle('fn', fontName=FONTE_N, fontSize=9,
                               leading=12, textColor=TINTA),
        'num': ParagraphStyle('nm', fontName=FONTE, fontSize=9, leading=12,
                              textColor=TINTA_FRACA, alignment=TA_RIGHT),
        'sub': ParagraphStyle('sb', fontName=FONTE_N, fontSize=9.5, leading=12,
                              textColor=TINTA, alignment=TA_RIGHT),
        'nomes': ParagraphStyle('nomes', fontName=FONTE, fontSize=7.8,
                                leading=10.5, textColor=TINTA_FRACA),
    }


def _etiqueta(texto, cor):
    """Chip colorido — faz o papel do emoji, que Helvetica não tem."""
    st = ParagraphStyle('et', fontName=FONTE_N, fontSize=7, leading=9,
                        textColor=cor, alignment=TA_CENTER)
    t = Table([[Paragraph(esc(texto), st)]], colWidths=[22 * mm], rowHeights=[6 * mm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.Color(cor.red, cor.green, cor.blue, 0.10)),
        ('BOX', (0, 0), (-1, -1), 0.5, cor),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    return t


# --------------------------------------------------------------------------
# Cabeçalho e rodapé de cada página
# --------------------------------------------------------------------------
def _moldura(fluxo, data_iso, gerado_em):
    def desenhar(canvas, doc):
        canvas.saveState()
        # Faixa escura no topo
        canvas.setFillColor(TINTA)
        canvas.rect(0, A4[1] - 16 * mm, A4[0], 16 * mm, stroke=0, fill=1)

        canvas.setFillColor(colors.white)
        canvas.setFont(FONTE_N, 9.5)
        canvas.drawString(MARGEM, A4[1] - 10.5 * mm, 'RELATÓRIO DE REFEIÇÕES')

        canvas.setFont(FONTE_N, 9.5)
        canvas.drawRightString(A4[0] - MARGEM, A4[1] - 10.5 * mm,
                               f'{fluxo}   ·   {_data_br(data_iso)}')

        # Rodapé
        canvas.setFillColor(TINTA_FRACA)
        canvas.setFont(FONTE, 7.5)
        canvas.drawString(MARGEM, 10 * mm,
                          f'Sistema de Refeições Larsil  ·  gerado em {gerado_em}')
        canvas.drawRightString(A4[0] - MARGEM, 10 * mm, f'Página {doc.page}')
        canvas.setStrokeColor(LINHA)
        canvas.setLineWidth(0.5)
        canvas.line(MARGEM, 13.5 * mm, A4[0] - MARGEM, 13.5 * mm)
        canvas.restoreState()

    return desenhar


# --------------------------------------------------------------------------
# Agrupamento
# --------------------------------------------------------------------------
def _agrupar(linhas):
    """Achata PEDIDOS em projeto > equipe > família de refeição."""
    projetos = {}
    for p in linhas:
        proj = str(p.get('PROJETO') or '—')
        eq = str(p.get('LIDER') or '—')
        fam = _familia_refeicao(p.get('TIPO_REFEICAO'))

        bloco = projetos.setdefault(proj, {'projeto': proj, 'total': 0.0,
                                           'equipes': {}})
        equipe = bloco['equipes'].setdefault(eq, {
            'equipe': eq, 'nome': p.get('NOME_LIDER') or '', 'total': 0.0,
            'pessoas': 0, 'refeicoes': {}, 'situacoes': set(),
        })

        try:
            valor = float(p.get('TOTAL_PAGAR') or 0)
        except (TypeError, ValueError):
            valor = 0.0
        try:
            qtd = int(p.get('TOTAL_COLABORADORES') or 0)
        except (TypeError, ValueError):
            qtd = 0

        bloco['total'] += valor
        equipe['total'] += valor
        equipe['pessoas'] += qtd
        equipe['situacoes'].add((str(p.get('APROVADO') or '').strip().upper()
                                 or 'AGUARDANDO'))

        nomes = [_titulo_pessoa(n) for n in
                 str(p.get('COLABORADORES') or '').split(',') if n.strip()]

        equipe['refeicoes'].setdefault(fam, []).append({
            'tipo': str(p.get('TIPO_REFEICAO') or ''),
            'fornecedor': str(p.get('FORNECEDOR') or '—'),
            'unitario': p.get('VALOR_PAGO'),
            'qtd': qtd,
            'subtotal': valor,
            'nomes': nomes,
        })

    return sorted(projetos.values(), key=lambda b: b['projeto'])


def _situacao(equipe):
    s = equipe['situacoes']
    if s == {'APROVADO'}:
        return 'APROVADO', VERDE
    if s == {'REPROVADO'}:
        return 'REPROVADO', VERMELHO
    if 'AGUARDANDO' in s or '' in s:
        return ('PARCIAL', AMBAR) if len(s) > 1 else ('PENDENTE', AMBAR)
    return 'PARCIAL', AMBAR


# --------------------------------------------------------------------------
# Capa
# --------------------------------------------------------------------------
def _capa(est, fluxo, data_iso, projetos):
    total = sum(b['total'] for b in projetos)
    equipes = [e for b in projetos for e in b['equipes'].values()]
    pessoas = sum(e['pessoas'] for e in equipes)
    refeicoes = sum(len(l) for e in equipes for l in e['refeicoes'].values())

    itens = [
        Spacer(1, 6 * mm),
        Paragraph(f'Pedidos de Refeição — {esc(fluxo)}', est['capa_titulo']),
        Spacer(1, 2 * mm),
        Paragraph(_data_extenso(data_iso), est['capa_sub']),
        Spacer(1, 8 * mm),
    ]

    # Quatro números grandes: é o que a gerência olha primeiro.
    cartoes = [('EQUIPES', str(len(equipes))), ('REFEIÇÕES', str(refeicoes)),
               ('PESSOAS', str(pessoas)), ('TOTAL DO DIA', moeda(total))]
    linha_rot = [Paragraph(r, est['kpi_rot']) for r, _ in cartoes]
    linha_val = [Paragraph(v, est['kpi_val']) for _, v in cartoes]

    larg = LARGURA_UTIL / 4.0
    kpi = Table([linha_rot, linha_val], colWidths=[larg] * 4)
    kpi.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), FUNDO_SUAVE),
        ('BOX', (0, 0), (-1, -1), 0.7, LINHA),
        ('INNERGRID', (0, 0), (-1, -1), 0.7, LINHA),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, 0), 7),
        ('BOTTOMPADDING', (0, 1), (-1, 1), 9),
    ]))
    itens += [kpi, Spacer(1, 10 * mm),
              Paragraph('Consolidado por equipe', est['secao']),
              Spacer(1, 3 * mm)]

    # Tabela do consolidado
    cab = [Paragraph('PROJETO', est['th']), Paragraph('EQUIPE', est['th']),
           Paragraph('LÍDER', est['th']), Paragraph('SITUAÇÃO', est['th']),
           Paragraph('REF.', est['th_d']), Paragraph('PESSOAS', est['th_d']),
           Paragraph('TOTAL', est['th_d'])]
    dados, estilo_extra = [cab], []

    for bloco in projetos:
        for eq in sorted(bloco['equipes'].values(), key=lambda e: e['equipe']):
            rotulo, cor = _situacao(eq)
            n_ref = sum(len(l) for l in eq['refeicoes'].values())
            dados.append([
                Paragraph(esc(bloco['projeto']), est['td']),
                Paragraph(f"<b>{esc(eq['equipe'])}</b>", est['td']),
                Paragraph(esc(_titulo_pessoa(eq['nome'])), est['td']),
                Paragraph(f'<font color="#{cor.hexval()[2:]}"><b>{rotulo}</b></font>',
                          est['td']),
                Paragraph(str(n_ref), est['td_d']),
                Paragraph(str(eq['pessoas']), est['td_d']),
                Paragraph(f"<b>{moeda(eq['total'])}</b>", est['td_d']),
            ])

    dados.append([Paragraph('<b>TOTAL GERAL</b>', est['td']), '', '', '', '', '',
                  Paragraph(f'<b>{moeda(total)}</b>', est['td_d'])])

    larguras = [22 * mm, 22 * mm, 52 * mm, 24 * mm, 14 * mm, 18 * mm, 30 * mm]
    tab = Table(dados, colWidths=larguras, repeatRows=1)
    estilo = [
        ('LINEBELOW', (0, 0), (-1, 0), 0.8, TINTA_FRACA),
        ('LINEBELOW', (0, 1), (-1, -2), 0.4, LINHA),
        ('LINEABOVE', (0, -1), (-1, -1), 0.8, TINTA),
        ('SPAN', (0, -1), (5, -1)),
        ('BACKGROUND', (0, -1), (-1, -1), FUNDO_SUAVE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]
    tab.setStyle(TableStyle(estilo + estilo_extra))
    itens.append(tab)
    return itens


# --------------------------------------------------------------------------
# Detalhe
# --------------------------------------------------------------------------
def _detalhe(est, projetos):
    itens = []

    for bloco in projetos:
        faixa = Table([[Paragraph(f"PROJETO {esc(bloco['projeto'])}", est['projeto']),
                        Paragraph(moeda(bloco['total']), est['projeto_v'])]],
                      colWidths=[LARGURA_UTIL * 0.6, LARGURA_UTIL * 0.4])
        faixa.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), TINTA),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (0, 0), 8),
            ('RIGHTPADDING', (-1, 0), (-1, 0), 8),
        ]))
        itens += [faixa, Spacer(1, 4 * mm)]

        for eq in sorted(bloco['equipes'].values(), key=lambda e: e['equipe']):
            rotulo, cor = _situacao(eq)
            cab = Table([[
                Paragraph(f"{esc(eq['equipe'])} &nbsp;·&nbsp; "
                          f"{esc(_titulo_pessoa(eq['nome']))}", est['equipe']),
                _etiqueta(rotulo, cor),
                Paragraph(moeda(eq['total']), est['equipe_v']),
            ]], colWidths=[LARGURA_UTIL - 55 * mm, 24 * mm, 31 * mm])
            cab.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LINEBELOW', (0, 0), (-1, -1), 0.8, LINHA),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ]))
            itens += [cab, Spacer(1, 3 * mm)]

            familias = sorted(eq['refeicoes'].items(),
                              key=lambda kv: ORDEM_REFEICAO.index(kv[0])
                              if kv[0] in ORDEM_REFEICAO else 99)

            for fam, linhas in familias:
                cor_ref = CORES_REFEICAO.get(fam, CORES_REFEICAO['OUTRO'])
                tag = Table([[Paragraph(esc(fam), est['refeicao'])]],
                            colWidths=[26 * mm])
                tag.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, -1), cor_ref),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('TOPPADDING', (0, 0), (-1, -1), 3),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                    ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ]))
                # Tabela mais estreita que o quadro vem centralizada por
                # padrão no reportlab; a tarja tem que encostar na esquerda.
                tag.hAlign = 'LEFT'
                grupo = [tag, Spacer(1, 2 * mm)]

                for l in linhas:
                    # Quando a equipe se divide em frentes, o mesmo tipo de
                    # refeição vem duas vezes com fornecedores diferentes —
                    # cada linha carrega a sua gente, e isso fica explícito.
                    detalhe_tipo = ''
                    if l['tipo'] and l['tipo'].upper() != fam:
                        resto = l['tipo'].upper().replace(fam, '').strip()
                        if resto:
                            detalhe_tipo = (f'  <font size="7" color="#94a3b8">'
                                            f'{esc(resto)}</font>')

                    cabecalho = Table([[
                        Paragraph(esc(l['fornecedor']) + detalhe_tipo, est['forn']),
                        Paragraph(moeda(l['unitario']), est['num']),
                        Paragraph(f"× {l['qtd']}", est['num']),
                        Paragraph(moeda(l['subtotal']), est['sub']),
                    ]], colWidths=[LARGURA_UTIL - 84 * mm, 28 * mm, 18 * mm, 38 * mm])
                    cabecalho.setStyle(TableStyle([
                        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('TOPPADDING', (0, 0), (-1, -1), 2),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                        ('LEFTPADDING', (0, 0), (0, 0), 6),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
                    ]))

                    # Nem todo pedido gravou a lista de gente (uns 11% do
                    # histórico). Melhor dizer isso na cara do que mostrar
                    # um traço e deixar a gerência achando que faltou gente.
                    if l['nomes']:
                        nomes = ' · '.join(esc(n) for n in l['nomes'])
                    else:
                        nomes = (f"<i>{l['qtd']} pessoa(s) — nomes não "
                                 f"informados no pedido</i>")
                    corpo = Table([[Paragraph(nomes, est['nomes'])]],
                                  colWidths=[LARGURA_UTIL - 6 * mm])
                    corpo.setStyle(TableStyle([
                        ('LEFTPADDING', (0, 0), (-1, -1), 6),
                        ('TOPPADDING', (0, 0), (-1, -1), 0),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                    ]))
                    corpo.hAlign = 'LEFT'

                    # KeepTogether por linha: o nome nunca desgruda do
                    # fornecedor ao virar a página.
                    grupo.append(KeepTogether([cabecalho, corpo]))

                itens.append(KeepTogether(grupo[:2]))
                itens += grupo[2:]
                itens.append(Spacer(1, 2 * mm))

            itens.append(Spacer(1, 4 * mm))

        itens.append(Spacer(1, 4 * mm))

    return itens


# --------------------------------------------------------------------------
# Montagem
# --------------------------------------------------------------------------
def gerar_pdf(fluxo, data_iso, linhas):
    """Devolve os bytes do PDF, ou None se não houver pedido no dia."""
    if not linhas:
        return None

    projetos = _agrupar(linhas)
    if not projetos:
        return None

    est = _estilos()
    gerado_em = datetime.now(pytz.timezone('America/Sao_Paulo')).strftime(
        '%d/%m/%Y às %H:%M')

    buffer = BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=A4,
        leftMargin=MARGEM, rightMargin=MARGEM,
        topMargin=22 * mm, bottomMargin=18 * mm,
        title=f'Relatório de Refeições {_data_br(data_iso)} — {fluxo}',
        author='Sistema de Refeições Larsil',
    )
    quadro = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                   id='corpo', leftPadding=0, rightPadding=0,
                   topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='padrao', frames=[quadro],
                                       onPage=_moldura(fluxo, data_iso, gerado_em))])

    historia = _capa(est, fluxo, data_iso, projetos)
    historia.append(PageBreak())
    historia += _detalhe(est, projetos)

    doc.build(historia)
    return buffer.getvalue()


def nome_arquivo(fluxo, data_iso):
    return f'refeicoes-{data_iso}-{str(fluxo).lower()}.pdf'
