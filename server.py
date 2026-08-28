#!/usr/bin/env python3
import http.server
import socketserver
import socket
import json
import urllib.parse
import base64
import hmac
import re
import threading
import queue
import time
from datetime import datetime
import pytz
import pymssql
import decimal
import os
import requests
from dotenv import load_dotenv

# Carregar variáveis de ambiente
load_dotenv()


# ==========================================================================
# REDE DE SAÍDA — IPv6 quebrado trava tudo
#
# Nesta rede o IPv6 resolve mas não conecta: api.telegram.org via IPv6 dá
# timeout, via IPv4 responde em 0,35s. Como o Python tenta IPv6 primeiro
# (é o que o getaddrinfo devolve na frente), CADA chamada ao Telegram, à
# Z-API e à IAM pagava ~20s esperando o timeout antes de cair no IPv4 —
# era isso que fazia o botão de aprovar ficar "girando" sem responder.
#
# A checagem é feita no boot e vale para o processo todo: se o IPv6 estiver
# ruim, o getaddrinfo passa a devolver só IPv4. Onde o IPv6 funciona (ex.:
# Railway), nada muda.
# ==========================================================================
_getaddrinfo_original = socket.getaddrinfo


def _ipv6_utilizavel(timeout=2.5):
    """IPv6 realmente CONECTA? Resolver não basta — aqui ele resolve e trava."""
    if os.getenv('FORCAR_IPV4', '').strip() == '1':
        return False
    try:
        alvo = socket.getaddrinfo('api.telegram.org', 443,
                                  socket.AF_INET6, socket.SOCK_STREAM)[0][4]
    except OSError:
        return False

    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(alvo)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _preferir_ipv4():
    """Faz todo o processo (requests, urllib, pymssql) enxergar só IPv4."""
    def apenas_ipv4(host, porta, familia=0, tipo=0, proto=0, flags=0):
        if familia == socket.AF_UNSPEC:
            familia = socket.AF_INET
        return _getaddrinfo_original(host, porta, familia, tipo, proto, flags)
    socket.getaddrinfo = apenas_ipv4

# Função para serializar Decimal e datetime em JSON
def decimal_default(obj):
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    elif isinstance(obj, datetime):
        # Converter para horário de Brasília
        brasilia_tz = pytz.timezone('America/Sao_Paulo')
        if obj.tzinfo is None:
            # Se datetime não tem timezone, assumir que já está em UTC (Railway usa UTC)
            # e converter para Brasília
            utc_tz = pytz.UTC
            obj = utc_tz.localize(obj).astimezone(brasilia_tz)
        else:
            # Se já tem timezone, converter para Brasília
            obj = obj.astimezone(brasilia_tz)
        return obj.isoformat()
    elif hasattr(obj, '__str__'):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

# Configuração do Azure SQL
AZURE_CONFIG = {
    'server': os.getenv('AZURE_SQL_SERVER'),
    'database': os.getenv('AZURE_SQL_DATABASE'),
    'username': os.getenv('AZURE_SQL_USERNAME'),
    'password': os.getenv('AZURE_SQL_PASSWORD')
}

# Configuração do Azure Blob Storage
AZURE_BLOB_CONFIG = {
    'account_name': os.getenv('AZURE_STORAGE_ACCOUNT'),
    'container_name': os.getenv('AZURE_STORAGE_CONTAINER'),
    'sas_token': os.getenv('AZURE_SAS_TOKEN')
}

# Configurações Azure carregadas

# ==========================================================================
# IAM LARSIL — identidade única
#
# A identidade NÃO mora aqui. Este sistema não tem tabela de usuário e nunca
# compara senha: quem autentica é a IAM (ver INTEGRACAO.md do projeto
# iam_larsil). Aqui só validamos o token dela e lemos papéis/permissões/escopos.
#
# Validação no modo REMOTO (INTEGRACAO.md §2-B): perguntamos à IAM em
# /api/auth/resolve em vez de compartilhar o JWT_SECRET dela. Assim uma mudança
# de permissão no console vale aqui em até 60s, sem esperar o token expirar, e
# não guardamos segredo de outro sistema neste servidor.
# ==========================================================================
IAM_URL = os.getenv('IAM_URL', 'https://painelgestor.up.railway.app').rstrip('/')
IAM_SISTEMA = os.getenv('IAM_SISTEMA', 'REFEICOES')
IAM_REGISTRY_KEY = os.getenv('IAM_REGISTRY_KEY', '')

# Namespace das permissões deste sistema (minúsculo, por convenção da IAM)
IAM_NAMESPACE = IAM_SISTEMA.lower()
PERMISSAO_ACESSO = f'{IAM_NAMESPACE}.acesso'

# Exigir a permissão "<sistema>.acesso" para entrar.
#
# Fica DESLIGADO por padrão durante a transição: o sistema acabou de ser
# registrado na IAM e, enquanto a TI não conceder a permissão aos papéis dos
# líderes de campo, ligar isto trancaria todo mundo para fora. Assim que a
# concessão estiver feita, defina IAM_EXIGIR_ACESSO=true.
IAM_EXIGIR_ACESSO = os.getenv('IAM_EXIGIR_ACESSO', '').lower() == 'true'

# URL do Painel PCP, que resolve a foto de perfil de qualquer pessoa por nome.
# A IAM não guarda foto (INTEGRACAO.md §5.3).
PCP_URL = os.getenv('PCP_URL', 'https://gestao.up.railway.app').rstrip('/')

# ==========================================================================
# Z-API — aprovação de pedido por WhatsApp
#
# O pedido no PAGCORP precisa do aval de quem controla o saldo do cartão.
# Em vez de esperar alguém abrir um painel, a mensagem chega no WhatsApp com
# os botões Aprovar/Reprovar; a resposta volta pelo webhook e o solicitante
# é avisado no número que a IAM tem dele.
# ==========================================================================
ZAPI_INSTANCE = os.getenv('ZAPI_INSTANCE', '')
ZAPI_TOKEN = os.getenv('ZAPI_TOKEN', '')
ZAPI_CLIENT_TOKEN = os.getenv('ZAPI_CLIENT_TOKEN', '')
WHATSAPP_APROVADOR = os.getenv('WHATSAPP_APROVADOR', '')
NOME_APROVADOR = os.getenv('NOME_APROVADOR', 'Elaine Klug')

# Segredo do webhook. A Z-API chama de fora, sem token da IAM, então esta é a
# única barreira: sem ela qualquer um que descubra a URL aprova gasto de
# cartão corporativo. Ausência de segredo = webhook desligado, nunca aberto.
ZAPI_WEBHOOK_SEGREDO = os.getenv('ZAPI_WEBHOOK_SEGREDO', '')

# De quem é cada solicitação, para a devolutiva ir ao telefone certo.
# Guardado no envio (quando a pessoa está autenticada) e lido no retorno —
# jamais tirado do corpo do webhook, que é entrada não confiável.
ARQUIVO_SOLICITANTES = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '_aprovacoes_pendentes.json')
_lock_solicitantes = threading.Lock()


def _ler_solicitantes():
    try:
        with open(ARQUIVO_SOLICITANTES, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def registrar_solicitante(referencia, dados):
    """Guarda quem pediu, para avisar quando o aprovador responder."""
    with _lock_solicitantes:
        mapa = _ler_solicitantes()
        mapa[str(referencia)] = dados
        # Não deixa crescer para sempre
        if len(mapa) > 500:
            for k in list(mapa)[:-500]:
                mapa.pop(k, None)
        try:
            with open(ARQUIVO_SOLICITANTES, 'w', encoding='utf-8') as f:
                json.dump(mapa, f, ensure_ascii=False)
        except Exception as e:
            print(f'⚠️ Não consegui registrar o solicitante: {e}', flush=True)


def buscar_solicitante(referencia):
    with _lock_solicitantes:
        return _ler_solicitantes().get(str(referencia))


def zapi_configurada():
    return all((ZAPI_INSTANCE, ZAPI_TOKEN, WHATSAPP_APROVADOR))


# Mesma história do Telegram: conexão reaproveitada em vez de handshake
# TLS novo a cada mensagem.
_sessao_zapi = requests.Session()
_sessao_iam = requests.Session()


def _zapi_url(rota):
    return f'https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/{rota}'


def _zapi_headers():
    cab = {'Content-Type': 'application/json'}
    if ZAPI_CLIENT_TOKEN:
        cab['Client-Token'] = ZAPI_CLIENT_TOKEN
    return cab


def _so_digitos(numero):
    return ''.join(c for c in str(numero or '') if c.isdigit())


def zapi_enviar_texto(numero, mensagem):
    """Mensagem simples. Devolve (ok, detalhe)."""
    if not zapi_configurada():
        return False, 'Z-API não configurada'

    numero = _so_digitos(numero)
    if not numero:
        return False, 'número vazio'

    try:
        r = _sessao_zapi.post(_zapi_url('send-text'), headers=_zapi_headers(),
                          json={'phone': numero, 'message': mensagem}, timeout=10)
        if r.status_code in (200, 201):
            return True, r.text[:200]
        return False, f'HTTP {r.status_code}: {r.text[:200]}'
    except requests.exceptions.RequestException as e:
        return False, f'rede: {e}'


def zapi_enviar_aprovacao(numero, mensagem, pedido_ref, titulo=None, rodape=None):
    """
    Mensagem com botões de decisão.

    A Z-API tem três formatos e nem toda conexão do WhatsApp aceita todos.
    Tentamos do mais capaz para o mais simples, e o webhook entende a resposta
    de qualquer um deles — o fluxo nunca trava por causa do formato:

      1. send-button-actions  → o atual; até 3 botões, tipos REPLY/URL/CALL
      2. send-button-list     → o antigo, mantido só como rede de segurança
      3. send-text            → pede a resposta escrita ("APROVAR 123")
    """
    numero = _so_digitos(numero)
    if not zapi_configurada() or not numero:
        return False, 'Z-API não configurada'

    # --- 1. Formato atual ------------------------------------------------
    corpo = {
        'phone': numero,
        'message': mensagem,
        'buttonActions': [
            {'id': f'aprovar:{pedido_ref}', 'type': 'REPLY', 'label': '✅ APROVAR'},
            {'id': f'reprovar:{pedido_ref}', 'type': 'REPLY', 'label': '❌ REPROVAR'},
        ]
    }
    if titulo:
        corpo['title'] = titulo
    if rodape:
        corpo['footer'] = rodape

    try:
        r = _sessao_zapi.post(_zapi_url('send-button-actions'), headers=_zapi_headers(),
                          json=corpo, timeout=20)
        if r.status_code in (200, 201):
            print(f'✅ Aprovação enviada (button-actions, ref {pedido_ref})', flush=True)
            return True, 'button-actions'
        print(f'⚠️ button-actions recusado ({r.status_code}): {r.text[:160]}', flush=True)
    except requests.exceptions.RequestException as e:
        print(f'⚠️ button-actions falhou: {e}', flush=True)

    # --- 2. Formato antigo -----------------------------------------------
    try:
        r = _sessao_zapi.post(_zapi_url('send-button-list'), headers=_zapi_headers(), json={
            'phone': numero,
            'message': mensagem,
            'buttonList': {'buttons': [
                {'id': f'aprovar:{pedido_ref}', 'label': 'APROVAR'},
                {'id': f'reprovar:{pedido_ref}', 'label': 'REPROVAR'},
            ]}
        }, timeout=20)
        if r.status_code in (200, 201):
            print(f'✅ Aprovação enviada (button-list, ref {pedido_ref})', flush=True)
            return True, 'button-list'
        print(f'⚠️ button-list recusado ({r.status_code}); enviando como texto', flush=True)
    except requests.exceptions.RequestException as e:
        print(f'⚠️ button-list falhou ({e}); enviando como texto', flush=True)

    # --- 3. Texto puro ---------------------------------------------------
    texto = (mensagem + '\n\n'
             f'Responda *APROVAR {pedido_ref}* ou *REPROVAR {pedido_ref}*')
    return zapi_enviar_texto(numero, texto)


def telefone_do_solicitante(usuario):
    """
    Telefone de quem pediu, para receber a devolutiva.
    A IAM é a fonte; o cadastro de colaboradores não guarda telefone.
    """
    tel = _so_digitos(usuario.get('telefone'))
    if not tel:
        return None
    # Número brasileiro sem DDI: a Z-API exige o 55 na frente
    if len(tel) in (10, 11):
        tel = '55' + tel
    return tel


# ==========================================================================
# TELEGRAM — quem aprova
#
# Long-polling em vez de webhook: uma thread pergunta a cada poucos segundos
# se chegou resposta. Some a necessidade de URL pública (e de ngrok), e a
# instância Z-API não precisa virar receptora — ela segue só enviando.
#
# O chat de quem aprova é descoberto sozinho: a pessoa manda /start para o
# bot e o id fica gravado. Ninguém precisa caçar número.
# ==========================================================================
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_BOT_USER = os.getenv('TELEGRAM_BOT_USER', '')
TELEGRAM_CHAT_APROVADOR = os.getenv('TELEGRAM_CHAT_APROVADOR', '')

# Senha do /start. O bot é pesquisável pelo nome: sem isto, quem o encontrasse
# viraria aprovador e passaria a decidir gasto de cartão corporativo.
TELEGRAM_SENHA_REGISTRO = os.getenv('TELEGRAM_SENHA_REGISTRO', '')

# Ids do Telegram autorizados a decidir. Num grupo, conferir só o chat não
# basta: qualquer membro consegue tocar no botão. Vazio = todo mundo do chat
# registrado pode decidir (serve quando o grupo já é fechado).
TELEGRAM_APROVADORES = {
    p.strip() for p in os.getenv('TELEGRAM_APROVADORES', '').split(',') if p.strip()
}

ARQUIVO_TELEGRAM = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '_telegram.json')

_lock_telegram = threading.Lock()


def telegram_configurado():
    return bool(TELEGRAM_TOKEN)


# ==========================================================================
# DOIS GRUPOS, DOIS FLUXOS
#
# PAGCORP e Fechamento são dinheiros diferentes: um repõe saldo de cartão e
# vira depósito do financeiro; o outro é pago no fechamento do mês. Misturar
# os dois num grupo só fez a Elaine aprovar Fechamento achando que era
# PAGCORP — e pedido de Fechamento aprovado entra na fila de depósito e
# corre risco de ser pago duas vezes.
#
# Cada fluxo tem seu grupo. O chat de cada um é descoberto pelo /start (ver
# _tg_tratar_mensagem), com o env como valor inicial.
# ==========================================================================
TELEGRAM_CHAT_FECHAMENTO = os.getenv('TELEGRAM_CHAT_FECHAMENTO', '')

FLUXO_PAGCORP = 'PAGCORP'
FLUXO_FECHAMENTO = 'FECHAMENTO'


def _tg_estado():
    try:
        with open(ARQUIVO_TELEGRAM, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {
            'chat_aprovador': TELEGRAM_CHAT_APROVADOR,
            'chat_fechamento': TELEGRAM_CHAT_FECHAMENTO,
            'ultimo_update': 0,
        }


def _tg_salvar(estado):
    try:
        with open(ARQUIVO_TELEGRAM, 'w', encoding='utf-8') as f:
            json.dump(estado, f, ensure_ascii=False)
    except Exception as e:
        print(f'⚠️ Não consegui salvar o estado do Telegram: {e}', flush=True)


def telegram_chat_do_fluxo(fluxo):
    """Para qual grupo vai este pedido."""
    with _lock_telegram:
        estado = _tg_estado()
    if fluxo == FLUXO_FECHAMENTO:
        return estado.get('chat_fechamento') or TELEGRAM_CHAT_FECHAMENTO
    return estado.get('chat_aprovador') or TELEGRAM_CHAT_APROVADOR


def telegram_chat_aprovador():
    """Grupo do PAGCORP (nome antigo, mantido por compatibilidade)."""
    return telegram_chat_do_fluxo(FLUXO_PAGCORP)


# Conexão reaproveitada (keep-alive). Abrir conexão nova a cada chamada
# custava de 1 a 8 segundos só de handshake TLS; reaproveitando, a mesma
# chamada leva ~0,2s. São duas sessões porque o long-polling segura uma
# conexão por 25s — se fosse a mesma, o envio ficaria esperando por ela.
_sessao_telegram = requests.Session()
_sessao_telegram_polling = requests.Session()


def _tg_api(metodo, **dados):
    if not telegram_configurado():
        return None
    try:
        r = _sessao_telegram.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/{metodo}',
            json=dados, timeout=12)
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f'⚠️ Telegram {metodo}: {e}', flush=True)
        return None


def telegram_enviar(chat_id, texto, botoes=None):
    """Mensagem para o Telegram. `botoes` = [[(rótulo, dado)]]."""
    corpo = {
        'chat_id': chat_id,
        'text': texto,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }
    if botoes:
        corpo['reply_markup'] = {
            'inline_keyboard': [
                [{'text': rot, 'callback_data': dado} for rot, dado in linha]
                for linha in botoes
            ]
        }

    r = _tg_api('sendMessage', **corpo)
    if r and r.get('ok'):
        return True, r['result'].get('message_id')
    return False, (r or {}).get('description', 'sem resposta')


def telegram_pedir_aprovacao(resumo, ids, fluxo=FLUXO_PAGCORP):
    """Manda o pedido para quem aprova, com os botões de decisão.

    `fluxo` escolhe o grupo: PAGCORP repõe saldo de cartão e vira depósito;
    Fechamento é pago no fim do mês. Cada um no seu grupo, senão a decisão
    de um vira pagamento indevido no outro.
    """
    chat = telegram_chat_do_fluxo(fluxo)
    if not chat:
        return False, (f'Grupo de {fluxo} ainda não registrado. '
                       f'Mande /start SENHA {fluxo} no grupo, com @{TELEGRAM_BOT_USER} dentro.')

    referencia = str(min(int(i) for i in ids))

    def esc(v):
        return (str(v if v is not None else '—')
                .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))

    fechamento = fluxo == FLUXO_FECHAMENTO
    linhas = [
        ('📋 <b>REFEIÇÃO — FECHAMENTO</b>' if fechamento
         else '💳 <b>REFEIÇÃO — PAGCORP</b>'),
        '',
        f"👤 <b>Solicitante:</b> {esc(resumo.get('solicitante'))}",
        f"🏢 <b>Projeto/Equipe:</b> {esc(resumo.get('projeto'))} / {esc(resumo.get('equipe'))}",
    ]
    # No Fechamento o cartão não entra na conta — quem paga é o mês.
    if not fechamento:
        linhas.append(f"💳 <b>PAGCORP:</b> {esc(resumo.get('pagcorp'))}")
    linhas += [
        f"📅 <b>Data:</b> {esc(resumo.get('data'))}",
        f"📍 <b>Cidade:</b> {esc(resumo.get('cidade'))}",
    ]

    # A conta aberta, uma refeição por linha.
    #
    # Antes ia só "Refeições: café, almoço" + o total. Quem aprova via o
    # valor final sem ter como conferir de onde saiu — e é justamente o
    # unitário que revela um preço fora do combinado. Com pessoas × preço
    # por refeição, a soma dá para conferir de cabeça.
    itens = resumo.get('itens') or []
    if itens:
        linhas.append('🍴 <b>Refeições</b>')
        for it in itens:
            local = esc(it.get('local') or '')
            pessoas = esc(it.get('pessoas'))
            valor = esc(it.get('valor'))
            subtotal = esc(it.get('subtotal'))
            linhas.append(f"  • {esc(it.get('refeicao'))}"
                          + (f" — {local}" if local else ''))
            linhas.append(f"     {esc(it.get('fornecedor'))}")
            linhas.append(f"     {pessoas} × R$ {valor} = <b>R$ {subtotal}</b>")
    else:
        # Pedido antigo, sem o detalhamento: mantém o formato de antes
        linhas.append(f"🍴 <b>Refeições:</b> {esc(resumo.get('refeicoes'))}")

    linhas += [
        '',
        f"👥 <b>Pessoas:</b> {esc(resumo.get('pessoas'))}",
        f"💰 <b>Total:</b> R$ {esc(resumo.get('total'))}",
    ]
    if resumo.get('motivo'):
        linhas += ['', f"📝 <b>Motivo:</b> {esc(resumo['motivo'])}"]
    linhas += ['', f"<i>Pedido(s): {', '.join(str(i) for i in ids)}</i>"]

    # Empilhados (uma linha por botão) em vez de lado a lado: no celular
    # fica maior e mais fácil de acertar o dedo, do jeito que bot de
    # aprovação de venda costuma fazer.
    ok, detalhe = telegram_enviar(chat, '\n'.join(linhas), botoes=[
        [('✅ APROVAR', f'aprovar:{referencia}')],
        [('❌ REPROVAR', f'reprovar:{referencia}')],
    ])

    if ok:
        print(f'✅ Aprovação enviada no Telegram (ref {referencia})', flush=True)
    return ok, detalhe


def _tg_texto_decidido(msg, carimbo):
    """Texto do pedido RISCADO + o carimbo da decisão embaixo.

    Riscar deixa claro de relance que aquela mensagem já foi resolvida e não
    espera mais nada — só tirar os botões não bastava, o pedido continuava
    com cara de pendente no meio da conversa.

    O texto vem de msg['text'], que o Telegram entrega já sem as marcações
    (o negrito do envio se perde). Por isso ele é escapado antes de voltar
    como HTML: um '&' ou '<' vindo de nome de restaurante quebraria a
    edição inteira e a mensagem ficaria sem carimbo nenhum.
    """
    bruto = msg.get('text') or ''
    seguro = (bruto.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
    return f'<s>{seguro}</s>\n\n{carimbo}'


def _tg_tratar_callback(cb):
    """Clique em APROVAR/REPROVAR."""
    # Cronômetro pra achar ONDE o tempo some, caso volte a demorar: sem
    # hora em cada etapa, "demorou" não diz se o problema é o Telegram não
    # entregar o clique, o servidor demorar pra responder, ou outra coisa.
    t0 = time.monotonic()
    dado = cb.get('data') or ''
    chat = str(((cb.get('message') or {}).get('chat') or {}).get('id') or '')

    quem = cb.get('from') or {}
    quem_id = str(quem.get('id') or '')
    quem_nome = ' '.join(filter(None, [quem.get('first_name'), quem.get('last_name')])) \
        or quem.get('username') or 'Desconhecido'

    # 1. A decisão tem de vir do chat registrado
    if chat != str(telegram_chat_aprovador()):
        print(f'🚫 Decisão de chat não autorizado ({chat}) — ignorada', flush=True)
        _tg_api('answerCallbackQuery', callback_query_id=cb['id'],
                text='Este chat não está registrado para aprovar.')
        return

    # 2. Em grupo, o chat certo não garante a pessoa certa: se houver lista
    #    de aprovadores, ela manda.
    if TELEGRAM_APROVADORES and quem_id not in TELEGRAM_APROVADORES:
        print(f'🚫 {quem_nome} (id {quem_id}) não está na lista de aprovadores', flush=True)
        _tg_api('answerCallbackQuery', callback_query_id=cb['id'],
                text='Você não tem permissão para aprovar.', show_alert=True)
        return

    acao, _, referencia = dado.partition(':')
    decisao = 'APROVADO' if acao == 'aprovar' else 'REPROVADO' if acao == 'reprovar' else None
    if not decisao or not referencia.isdigit():
        return

    # Responde ao toque JÁ — antes de tocar em banco ou WhatsApp. O worker
    # processa um clique de cada vez; se isso só viesse depois do envio pro
    # WhatsApp (rede mais lenta e instável), o botão ficava "girando" até lá
    # e parecia travado, o que levava a clicar de novo e de novo. A reação
    # animada é o "explode e vibra" que se vê em bot de pagamento — o resto
    # (gravar, editar, avisar por WhatsApp) acontece por trás, sem prender
    # o toque.
    t_recebido = time.monotonic() - t0
    _tg_api('answerCallbackQuery', callback_query_id=cb['id'],
            text='Aprovado ✅' if decisao == 'APROVADO' else 'Reprovado ❌')
    t_respondido = time.monotonic() - t0
    print(f'⏱️ Telegram: clique de {quem_nome} → resposta em {t_respondido:.2f}s '
          f'(processado {t_recebido*1000:.0f}ms antes de chamar o Telegram)', flush=True)

    msg = cb.get('message') or {}
    if msg.get('message_id'):
        _tg_api('setMessageReaction', chat_id=chat, message_id=msg['message_id'],
                reaction=[{'type': 'emoji', 'emoji': '👍' if decisao == 'APROVADO' else '👎'}],
                is_big=True)

    base = executar_query(
        "SELECT LIDER, DATA_RETIRADA, Criado FROM PEDIDOS WHERE ID = %s", [int(referencia)])
    if not base:
        # Pedido sumiu do banco: some com os botões e diz o que houve, senão
        # a mensagem fica clicável para sempre sem nunca resolver nada.
        if msg.get('message_id'):
            _tg_api('editMessageText', chat_id=chat, message_id=msg['message_id'],
                    text=_tg_texto_decidido(msg, '⚠️ <b>Pedido não encontrado</b>'),
                    parse_mode='HTML')
        return

    b = base[0]

    # Decide SÓ os pedidos DESTA mensagem.
    #
    # Antes o UPDATE era por LIDER + DATA_RETIRADA, e isso varria todos os
    # lotes do dia daquela equipe. Uma equipe costuma pedir em levas (o café
    # e o almoço às 22h, a janta às 23h), e cada leva vira uma mensagem: o
    # primeiro clique aprovava também o que estava na SEGUNDA mensagem, sem
    # ninguém ter olhado. Depois, ao clicar nessa segunda, já não havia nada
    # em AGUARDANDO, o código saía antes e ela ficava sem o risco — foi assim
    # que o problema apareceu ("a janta não risca").
    #
    # Os ids exatos ficaram guardados no envio (registrar_solicitante).
    solicitante = buscar_solicitante(referencia) or {}
    ids_desta = [int(i) for i in (solicitante.get('pedidos') or []) if str(i).isdigit()]

    if ids_desta:
        marcadores = ', '.join(['%s'] * len(ids_desta))
        afetados = executar_query(
            f"UPDATE PEDIDOS SET APROVADO = %s, APROVADO_POR = %s "
            f"WHERE ID IN ({marcadores}) AND APROVADO = 'AGUARDANDO'",
            [decisao, quem_nome[:100]] + ids_desta)
        alcance = f'{len(ids_desta)} pedido(s) desta mensagem'
    else:
        # Sem o registro (ele vive em disco, e o Railway zera o disco a cada
        # deploy): agrupa pelo HORÁRIO do lote. Um lote é gravado em poucos
        # segundos, e levas diferentes ficam separadas por muitos minutos —
        # então uma janela curta em volta do pedido de referência isola a
        # leva certa sem varrer o dia inteiro da equipe.
        afetados = executar_query(
            "UPDATE PEDIDOS SET APROVADO = %s, APROVADO_POR = %s WHERE LIDER = %s "
            "AND CAST(DATA_RETIRADA AS DATE) = CAST(%s AS DATE) "
            "AND ABS(DATEDIFF(second, Criado, %s)) <= 120 "
            "AND APROVADO = 'AGUARDANDO'",
            [decisao, quem_nome[:100], b['LIDER'], b['DATA_RETIRADA'], b['Criado']])
        alcance = f'lote da equipe {b["LIDER"]} em {b["DATA_RETIRADA"]} (por horário)'

    print(f'✅ {decisao} por {quem_nome}: {afetados} de {alcance}', flush=True)

    # Clique repetido: o UPDATE já não achou nada em AGUARDANDO pra mudar.
    # Ainda assim carimbamos a mensagem — ela pode estar sem risco por causa
    # do problema acima, e deixá-la crua faria parecer que o clique não valeu.
    # O que NÃO se repete é a devolutiva por WhatsApp.
    if not afetados:
        if msg.get('message_id'):
            marca = '✅ <b>APROVADO</b>' if decisao == 'APROVADO' else '❌ <b>REPROVADO</b>'
            _tg_api('editMessageText', chat_id=chat, message_id=msg['message_id'],
                    text=_tg_texto_decidido(msg, f'{marca} (já estava decidido)'),
                    parse_mode='HTML')
        return

    # Carimbo na mensagem e devolutiva por WhatsApp são louça: quem já
    # tocou o botão já teve sua resposta lá em cima. Fazer isso em thread
    # separada libera o worker pra buscar o PRÓXIMO clique na hora — sem
    # isso, um WhatsApp lento (Z-API às vezes demora) prendia a fila
    # inteira, e um segundo toque enquanto o primeiro ainda processava
    # ficava girando sem resposta.
    def _finalizar_decisao(chat=chat, msg=msg, decisao=decisao, quem_nome=quem_nome,
                            referencia=referencia, b=b):
        if msg.get('message_id'):
            marca = '✅ <b>APROVADO</b>' if decisao == 'APROVADO' else '❌ <b>REPROVADO</b>'
            quem_seguro = (quem_nome.replace('&', '&amp;')
                                    .replace('<', '&lt;').replace('>', '&gt;'))
            hora = datetime.now(pytz.timezone('America/Sao_Paulo')).strftime('%d/%m às %H:%M')
            _tg_api('editMessageText',
                    chat_id=chat, message_id=msg['message_id'],
                    text=_tg_texto_decidido(msg, f'{marca} por {quem_seguro} · {hora}'),
                    parse_mode='HTML')

        # Devolutiva ao solicitante segue no WhatsApp, que ele já usa
        solicitante = buscar_solicitante(referencia) or {}
        telefone = _so_digitos(solicitante.get('telefone') or '')

        aviso = ('✅ *Pedido APROVADO*' if decisao == 'APROVADO' else '❌ *Pedido REPROVADO*')
        aviso += f"\n\n📅 {b['DATA_RETIRADA']}\n👥 Equipe {b['LIDER']}\n\n_Resposta de {quem_nome}_"

        if telefone:
            ok, _ = zapi_enviar_texto(telefone, aviso)
            print(f'📤 Devolutiva ao solicitante: {"enviada" if ok else "falhou"}', flush=True)
        else:
            print(f'ℹ️ {solicitante.get("login", "solicitante")} sem telefone na IAM — '
                  f'devolutiva não enviada', flush=True)

    threading.Thread(target=_finalizar_decisao, daemon=True).start()


# ==========================================================================
# REDE DE SEGURANÇA DAS APROVAÇÕES
#
# O envio da aprovação acontece no momento do pedido. Quando ele falha — e
# falha: rede, timeout do Telegram, o app perdendo a resposta — o pedido
# fica salvo com APROVADO nulo e NINGUÉM fica sabendo. O líder viu "Pedido
# enviado", quem aprova nunca recebeu, e o depósito atrasa sem motivo
# aparente. Já aconteceu dezenas de vezes.
#
# Este worker fecha esse buraco pelo lado do servidor: de tempos em tempos
# procura pedido PAGCORP sem aprovação e reenvia. Não depende do app, nem
# do aparelho, nem de alguém perceber.
#
# Só entra pedido com alguns minutos de vida — pedido recém-salvo pode
# estar com o envio ainda em curso, e reenviar geraria mensagem dobrada.
# ==========================================================================
INTERVALO_RESGATE = 300      # 5 min entre varreduras
IDADE_MINIMA_RESGATE = 3     # minutos de vida antes de considerar perdido
MAX_POR_VARREDURA = 4        # ritmo: não inunda o grupo nem o Telegram

# Só ontem e hoje. Pedido mais velho que isso já foi consumido: reabrir a
# aprovação dele só encheria o grupo de coisa vencida, sem ajudar o
# depósito. O que ficou para trás se resolve na mão, olhando a lista.


def _resgatar_aprovacoes_perdidas():
    """Uma varredura. Devolve quantos lotes foram reenviados."""
    if not telegram_configurado() or not telegram_chat_aprovador():
        return 0

    linhas = executar_query(f"""
        SELECT ID, LIDER, NOME_LIDER, PROJETO, PAGCORP, DATA_RETIRADA,
               TIPO_REFEICAO, FORNECEDOR, VALOR_PAGO, TOTAL_COLABORADORES,
               TOTAL_PAGAR, CIDADE_PRESTACAO_DO_SERVICO, OBSERVACOES
        FROM PEDIDOS
        WHERE APROVADO IS NULL
          -- O tipo vem da marca que o app deixou, e é ela que decide o GRUPO.
          --
          -- Ter o número do cartão preenchido não serve de critério: pedido
          -- de Fechamento também carrega PAGCORP. Foi filtrando por ele que
          -- mandei Fechamento para o grupo do PAGCORP.
          --
          -- A coluna FECHAMENTO também não serve: uma trigger a preenche a
          -- partir do cadastro do FORNECEDOR, então ela diz o que o
          -- restaurante aceita, não como o pedido foi pago.
          --
          -- Pedido do app antigo não tem marca nenhuma e fica de fora: sem
          -- saber o tipo, mandá-lo é arriscar o grupo errado de novo.
          AND (OBSERVACOES LIKE '%(PAGCORP)%' OR OBSERVACOES LIKE '%(FECHAMENTO)%')
          AND Criado >= CAST(DATEADD(day, -1, GETDATE()) AS DATE)
          AND Criado <= DATEADD(minute, -{IDADE_MINIMA_RESGATE}, GETDATE())
        ORDER BY ID
    """, []) or []

    if not linhas:
        return 0

    # Mesma unidade da aprovação normal: dia + equipe + cartão
    grupos = {}
    for p in linhas:
        chave = (p['LIDER'], str(p['DATA_RETIRADA']), p['PAGCORP'])
        grupos.setdefault(chave, []).append(p)

    reenviados = 0
    for (lider, data, pagcorp), itens in sorted(grupos.items()):
        # Um punhado por vez: quando há muita coisa represada, mandar tudo
        # de uma vez vira uma parede de mensagens no grupo (e o Telegram
        # limita rajada). O resto sai nas próximas varreduras.
        if reenviados >= MAX_POR_VARREDURA:
            print(f'⏸️ Resgate: {len(grupos) - reenviados} lote(s) ficam para a próxima varredura',
                  flush=True)
            break

        base = itens[0]
        ids = [int(p['ID']) for p in itens]

        def fmt(v):
            return f'{float(v or 0):.2f}'.replace('.', ',')

        resumo = {
            'solicitante': base.get('NOME_LIDER') or '—',
            'projeto': base.get('PROJETO') or '',
            'equipe': lider,
            'pagcorp': pagcorp,
            'data': (base['DATA_RETIRADA'].strftime('%d/%m/%Y')
                     if hasattr(base['DATA_RETIRADA'], 'strftime') else str(data)),
            'cidade': base.get('CIDADE_PRESTACAO_DO_SERVICO') or '',
            'refeicoes': ', '.join(p.get('TIPO_REFEICAO') or '' for p in itens),
            'itens': [{
                'refeicao': p.get('TIPO_REFEICAO') or '',
                'fornecedor': p.get('FORNECEDOR') or '—',
                'local': '',
                'pessoas': p.get('TOTAL_COLABORADORES') or 0,
                'valor': fmt(p.get('VALOR_PAGO')),
                'subtotal': fmt(p.get('TOTAL_PAGAR')),
            } for p in itens],
            'pessoas': max((p.get('TOTAL_COLABORADORES') or 0) for p in itens),
            'total': fmt(sum(float(p.get('TOTAL_PAGAR') or 0) for p in itens)),
            'motivo': 'Reenvio automático — o envio no momento do pedido não saiu.',
        }

        # O fluxo vem da marca que o app deixou na observação — é ela que
        # decide o grupo, e trocar isso é mandar dinheiro para o lugar errado.
        obs = str(base.get('OBSERVACOES') or '')
        fluxo = FLUXO_FECHAMENTO if '(FECHAMENTO)' in obs else FLUXO_PAGCORP

        ok, detalhe = telegram_pedir_aprovacao(resumo, ids, fluxo)
        if not ok:
            print(f'⚠️ Resgate falhou para {lider}/{data} ({fluxo}): {detalhe}', flush=True)
            continue

        marcadores = ', '.join(['%s'] * len(ids))
        executar_query(
            f"UPDATE PEDIDOS SET APROVADO = 'AGUARDANDO' "
            f"WHERE ID IN ({marcadores}) AND APROVADO IS NULL", ids)

        # Sem isto a decisão cairia no plano B (por horário) — e é aqui que
        # sabemos exatamente quais pedidos entraram nesta mensagem.
        registrar_solicitante(str(min(ids)), {
            'login': '', 'nome': base.get('NOME_LIDER') or '',
            'telefone': '', 'pedidos': ids, 'equipe': lider,
        })

        reenviados += 1
        print(f'🛟 Aprovação resgatada: {lider} {data} — pedidos {ids}', flush=True)

    return reenviados


def _worker_resgate_aprovacoes():
    while True:
        time.sleep(INTERVALO_RESGATE)
        try:
            _resgatar_aprovacoes_perdidas()
        except Exception as e:
            print(f'❌ Worker de resgate: {e}', flush=True)


def _tg_tratar_mensagem(msg):
    """
    /start <SENHA> registra quem aprova.

    A senha existe porque o bot é encontrável pelo nome no Telegram. Sem ela,
    bastava alguém dar /start para passar a receber — e decidir — as
    aprovações de gasto do cartão corporativo.
    """
    texto = (msg.get('text') or '').strip()
    chat = str((msg.get('chat') or {}).get('id') or '')
    nome = ((msg.get('from') or {}).get('first_name') or 'você')

    if not texto.startswith('/start'):
        return

    # Em grupo o comando vem como /start@NomeDoBot SENHA [FLUXO]
    texto_limpo = re.sub(r'^/start(@\S+)?', '/start', texto)
    partes = texto_limpo.split()
    informada = partes[1].strip() if len(partes) > 1 else ''

    # A terceira palavra escolhe o grupo. Sem ela, PAGCORP — era o único
    # fluxo antes e segue sendo o caso comum.
    pedido_fluxo = (partes[2].strip().upper() if len(partes) > 2 else FLUXO_PAGCORP)
    fluxo = FLUXO_FECHAMENTO if pedido_fluxo.startswith('FECH') else FLUXO_PAGCORP
    campo = 'chat_fechamento' if fluxo == FLUXO_FECHAMENTO else 'chat_aprovador'

    tipo_chat = (msg.get('chat') or {}).get('type') or 'private'
    em_grupo = tipo_chat in ('group', 'supergroup')
    titulo_chat = (msg.get('chat') or {}).get('title') or nome

    with _lock_telegram:
        estado = _tg_estado()
        atual = estado.get(campo)

    # Sem senha configurada o registro fica fechado — falhar fechado, não aberto
    if not TELEGRAM_SENHA_REGISTRO:
        print('🚫 /start recusado: TELEGRAM_SENHA_REGISTRO não configurada', flush=True)
        telegram_enviar(chat,
            'Este bot ainda não foi liberado para registro.\n\n'
            'Procure a TI para configurar a senha de aprovação.')
        return

    # Quem já é o aprovador pode falar com o bot sem repetir a senha
    if atual and chat == str(atual) and not informada:
        telegram_enviar(chat,
            f'Olá de novo, {nome}. ✅\n\n'
            'Você já é quem aprova. Os pedidos chegam aqui com os botões '
            '<b>APROVAR</b> e <b>REPROVAR</b>.')
        return

    if not hmac.compare_digest(informada.upper(), TELEGRAM_SENHA_REGISTRO.upper()):
        print(f'🚫 /start com senha inválida ({nome}, chat {chat})', flush=True)
        telegram_enviar(chat,
            'Para este grupo receber os pedidos, envie uma destas:\n\n'
            '<code>/start SENHA PAGCORP</code>\n'
            '<code>/start SENHA FECHAMENTO</code>\n\n'
            'A senha é fornecida pela TI.')
        return

    # Trocar de aprovador é mudança relevante: quem estava perde o acesso e
    # merece saber, senão a transferência acontece em silêncio.
    substituiu = bool(atual) and chat != str(atual)

    # Um grupo não pode acumular os dois fluxos: seria o mesmo problema de
    # antes, com Fechamento e PAGCORP misturados na mesma conversa.
    outro_campo = 'chat_aprovador' if campo == 'chat_fechamento' else 'chat_fechamento'
    with _lock_telegram:
        estado = _tg_estado()
        if str(estado.get(outro_campo) or '') == chat:
            estado[outro_campo] = ''
        estado[campo] = chat
        estado['registrado_em'] = datetime.now(
            pytz.timezone('America/Sao_Paulo')).isoformat()
        estado['registrado_por'] = nome
        _tg_salvar(estado)

    print(f'✅ Telegram: {fluxo} registrado em '
          + (f'GRUPO "{titulo_chat}"' if em_grupo else f'conversa com {nome}')
          + f' (chat {chat})' + (' — substituiu o anterior' if substituiu else ''),
          flush=True)

    if substituiu:
        telegram_enviar(atual,
            'ℹ️ As aprovações de refeição passaram a ser enviadas para outra '
            f'pessoa ({nome}). Você não receberá mais os pedidos.')

    if em_grupo:
        aviso_extra = ('\n\n⚠️ <b>Atenção:</b> em grupo, qualquer participante consegue '
                       'tocar nos botões. Quem decidir fica registrado no pedido. '
                       'Para restringir, peça à TI para preencher TELEGRAM_APROVADORES.'
                       if not TELEGRAM_APROVADORES else
                       '\n\n🔒 Só as pessoas autorizadas conseguem decidir.')
        telegram_enviar(chat,
            f'Pronto! 👋\n\n'
            f'Os pedidos de <b>{fluxo}</b> passam a chegar aqui em '
            f'<b>{titulo_chat}</b>, com os botões de aprovar e reprovar.' + aviso_extra)
    else:
        telegram_enviar(chat,
            f'Olá, {nome}! 👋\n\n'
            'Este bot avisa quando um líder pede saldo de refeição. '
            'Cada pedido chega aqui com os botões <b>APROVAR</b> e <b>REPROVAR</b>.\n\n'
            '✅ Você está registrado para aprovar.')


def _worker_telegram():
    """Pergunta ao Telegram se chegou resposta. Sem webhook, sem URL pública."""
    if not telegram_configurado():
        print('ℹ️ TELEGRAM_BOT_TOKEN não configurado — aprovação por Telegram desligada', flush=True)
        return

    with _lock_telegram:
        estado = _tg_estado()
        offset = estado.get('ultimo_update', 0)

    print(f'🤖 Telegram: escutando @{TELEGRAM_BOT_USER}', flush=True)

    while True:
        try:
            # timeout alto = long-polling: a chamada só volta quando há algo
            r = _sessao_telegram_polling.get(
                f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates',
                params={'offset': offset + 1, 'timeout': 25},
                timeout=35).json()

            if not r.get('ok'):
                time.sleep(10)
                continue

            if r.get('result'):
                agora = datetime.now(pytz.timezone('America/Sao_Paulo')).strftime('%H:%M:%S')
                print(f'📥 Telegram: {len(r["result"])} update(s) recebido(s) às {agora}', flush=True)

            for upd in r.get('result', []):
                offset = max(offset, upd['update_id'])
                try:
                    if upd.get('callback_query'):
                        _tg_tratar_callback(upd['callback_query'])
                    elif upd.get('message'):
                        _tg_tratar_mensagem(upd['message'])
                except Exception as e:
                    print(f'❌ Telegram, update {upd.get("update_id")}: {e}', flush=True)

            if r.get('result'):
                with _lock_telegram:
                    estado = _tg_estado()
                    estado['ultimo_update'] = offset
                    _tg_salvar(estado)

        except requests.exceptions.RequestException:
            time.sleep(5)          # rede caiu: tenta de novo
        except Exception as e:
            print(f'❌ Worker do Telegram: {e}', flush=True)
            time.sleep(10)


# Rotas /api que dispensam token. Todo o resto é protegido por padrão — assim
# uma rota nova nasce fechada, não aberta por esquecimento.
PUBLIC_API_PATHS = {
    '/api/config',
    '/api/auth/login',
    '/api/auth/logout',
    '/api/teste-conexao',
    '/api/health',
    # Chamado pela Z-API, que não tem token da IAM. A validação é por
    # ZAPI_WEBHOOK_SEGREDO na query string (ver do_POST).
    '/api/webhook/zapi'
}

# Cache das validações de token. Sem ele, toda requisição viraria um
# round-trip à IAM. TTL curto para que negar um acesso lá reflita aqui rápido.
TTL_RESOLVE = 60          # segundos
_cache_resolve = {}       # token -> (usuario, momento)
_cache_lock = threading.Lock()


def _decodificar_payload_jwt(token):
    """
    Lê o payload do JWT SEM validar assinatura.

    Seguro neste ponto porque só é usado depois que /api/auth/resolve já
    autenticou o token na IAM — aqui queremos apenas nome/login/cpf, que a
    resposta do resolve não traz. Nunca use isto para decidir permissão.
    """
    try:
        parte = token.split('.')[1]
        parte += '=' * (-len(parte) % 4)          # restaura o padding do base64url
        bruto = base64.urlsafe_b64decode(parte)
        return json.loads(bruto.decode('utf-8'))
    except Exception:
        return {}


class ErroIAM(Exception):
    """Falha ao validar identidade. `status` é o código a devolver ao cliente."""

    def __init__(self, mensagem, status=401, motivo=None):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = status
        self.motivo = motivo


def resolver_usuario(token):
    """Valida o token na IAM e devolve a identidade + acesso atualizado."""
    agora = time.time()

    with _cache_lock:
        entrada = _cache_resolve.get(token)
        if entrada and (agora - entrada[1]) < TTL_RESOLVE:
            return entrada[0]

    try:
        response = _sessao_iam.get(
            f'{IAM_URL}/api/auth/resolve',
            headers={'Authorization': f'Bearer {token}'},
            timeout=10
        )
    except requests.exceptions.RequestException as e:
        raise ErroIAM(f'Não foi possível falar com a IAM: {e}', status=502)

    if not response.ok:
        corpo = {}
        try:
            corpo = response.json()
        except Exception:
            pass
        raise ErroIAM(
            corpo.get('erro', 'Token inválido'),
            status=response.status_code,
            motivo=corpo.get('motivo')
        )

    acesso = response.json()
    identidade = _decodificar_payload_jwt(token)

    usuario = {
        'login': identidade.get('login') or acesso.get('login') or '',
        'nome': identidade.get('nome') or '',
        'cpf': identidade.get('cpf'),
        'admin': bool(identidade.get('admin')),
        'papeis': acesso.get('papeis') or identidade.get('papeis') or [],
        'permissoes': acesso.get('permissoes') or identidade.get('permissoes') or [],
        'escopos': acesso.get('escopos') or identidade.get('escopos') or [],
        'global': acesso.get('global', identidade.get('global', False)),
        # /resolve traz telefone (e email); o JWT não. Usado na devolutiva de
        # aprovação — sem isto o aviso de WhatsApp nunca tinha para onde ir.
        'telefone': acesso.get('telefone') or identidade.get('telefone'),
        'email': acesso.get('email') or identidade.get('email'),
    }

    with _cache_lock:
        _cache_resolve[token] = (usuario, agora)
        # Limpeza oportunista: sem isso o dicionário cresceria para sempre
        if len(_cache_resolve) > 500:
            for t, (_, quando) in list(_cache_resolve.items()):
                if agora - quando > TTL_RESOLVE:
                    _cache_resolve.pop(t, None)

    return usuario


def invalidar_cache_token(token):
    with _cache_lock:
        _cache_resolve.pop(token, None)


def ve_tudo(usuario):
    """Admin, global, ou algum escopo GLOBAL: enxerga qualquer equipe.

    Isto é ESCOPO DE DADOS — de quais equipes a pessoa enxerga os pedidos.
    Não confunda com permissão de tela: quem é 'global' vê todas as equipes,
    mas continua vendo só as telas que a IAM liberou (ver e_administrador).
    """
    if usuario.get('admin') or usuario.get('global'):
        return True
    return any(e.get('tipo') == 'GLOBAL' for e in usuario.get('escopos') or [])


def e_administrador(usuario):
    """Só o admin de verdade passa por cima das permissões de tela.

    Separado de ve_tudo() de propósito. Antes os dois eram a mesma coisa, e
    quem tinha 'global' (a gerência, por exemplo) via TODAS as telas mesmo
    com a IAM liberando uma só — a conta do financeiro continuava vendo o
    app inteiro por causa disso. 'global' responde "quais equipes eu vejo";
    quem responde "quais telas eu uso" é a lista de permissões.
    """
    return bool(usuario.get('admin'))


def equipes_do_escopo(usuario):
    """
    Escopos "diretos" do token, sem consultar o banco.

    EQUIPE traz o código pronto (700TA). PROJETO traz o código do projeto
    (700), que cobre todas as equipes dele.
    """
    equipes, projetos = set(), set()
    for e in usuario.get('escopos') or []:
        valor = str(e.get('valor') or '').strip().upper()
        if not valor:
            continue
        if e.get('tipo') == 'EQUIPE':
            equipes.add(valor)
        elif e.get('tipo') == 'PROJETO':
            projetos.add(valor)
    return equipes, projetos


# Equipes resolvidas por pessoa. A consulta ao ORGANOGRAMA é barata, mas
# aconteceria em toda requisição sem este cache.
TTL_EQUIPES = 120
_cache_equipes = {}       # login -> (lista, momento)
_cache_equipes_lock = threading.Lock()


def resolver_equipes_do_usuario(usuario):
    """
    Todas as equipes que a pessoa pode operar.

    A IAM não descreve o mundo só por EQUIPE/PROJETO: na Larsil o escopo mais
    comum de liderança é COORDENADOR ou SUPERVISOR, e o VALOR é o NOME da
    pessoa (INTEGRACAO.md §3). Esse nome casa com as colunas COORDENADOR /
    SUPERVISOR do ORGANOGRAMA — é o banco que diz quais equipes são dela.

    Devolve uma lista de dicts: {equipe, projeto, lider, origem}.
    """
    if ve_tudo(usuario):
        linhas = executar_query(
            "SELECT EQUIPE, PROJETO, LIDER FROM ORGANOGRAMA ORDER BY PROJETO, EQUIPE", []
        ) or []
        return [{'equipe': str(l['EQUIPE']).strip().upper(),
                 'projeto': str(l.get('PROJETO') or '').strip(),
                 'lider': l.get('LIDER') or '',
                 'origem': 'GLOBAL'} for l in linhas if l.get('EQUIPE')]

    login = usuario.get('login') or ''
    agora = time.time()

    with _cache_equipes_lock:
        entrada = _cache_equipes.get(login)
        if entrada and (agora - entrada[1]) < TTL_EQUIPES:
            return entrada[0]

    encontradas = {}   # codigo -> dict (evita repetir a mesma equipe)

    def registrar(linha, origem):
        codigo = str(linha.get('EQUIPE') or '').strip().upper()
        if not codigo or codigo in encontradas:
            return
        encontradas[codigo] = {
            'equipe': codigo,
            'projeto': str(linha.get('PROJETO') or '').strip(),
            'lider': linha.get('LIDER') or '',
            'origem': origem
        }

    for e in usuario.get('escopos') or []:
        tipo = e.get('tipo')
        valor = str(e.get('valor') or '').strip()
        if not valor:
            continue

        if tipo == 'EQUIPE':
            linhas = executar_query(
                "SELECT EQUIPE, PROJETO, LIDER FROM ORGANOGRAMA WHERE EQUIPE = %s", [valor.upper()]
            )
            # Equipe pode não estar no organograma; o escopo continua valendo
            if linhas:
                for l in linhas:
                    registrar(l, 'EQUIPE')
            else:
                registrar({'EQUIPE': valor,
                           'PROJETO': ''.join(c for c in valor if c.isdigit())}, 'EQUIPE')

        elif tipo == 'PROJETO':
            for l in (executar_query(
                "SELECT EQUIPE, PROJETO, LIDER FROM ORGANOGRAMA WHERE PROJETO = %s ORDER BY EQUIPE",
                [valor]
            ) or []):
                registrar(l, 'PROJETO')

        elif tipo in ('COORDENADOR', 'SUPERVISOR'):
            # LTRIM/RTRIM porque o cadastro tem espaço sobrando com frequência
            for l in (executar_query(
                f"SELECT EQUIPE, PROJETO, LIDER FROM ORGANOGRAMA "
                f"WHERE LTRIM(RTRIM(UPPER({tipo}))) = %s ORDER BY PROJETO, EQUIPE",
                [valor.upper()]
            ) or []):
                registrar(l, tipo)

    lista = sorted(encontradas.values(), key=lambda x: (x['projeto'], x['equipe']))

    with _cache_equipes_lock:
        _cache_equipes[login] = (lista, agora)

    return lista


def equipe_permitida(usuario, equipe):
    """A pessoa pode operar esta equipe?"""
    equipe = str(equipe or '').strip().upper()
    if not equipe:
        return False
    if ve_tudo(usuario):
        return True

    # Caminho rápido: escopo direto de EQUIPE/PROJETO, sem ir ao banco
    equipes, projetos = equipes_do_escopo(usuario)
    if equipe in equipes:
        return True
    projeto_da_equipe = ''.join(c for c in equipe if c.isdigit())
    if projeto_da_equipe and projeto_da_equipe in projetos:
        return True

    # Escopo por COORDENADOR/SUPERVISOR: quem responde é o ORGANOGRAMA
    return any(e['equipe'] == equipe for e in resolver_equipes_do_usuario(usuario))


def tela_permitida(usuario, rota):
    """A pessoa pode usar esta tela?

    Mesma regra do menu no navegador, de propósito: quem não tem NENHUMA
    'refeicoes.tela:*' passa (o sistema roda em transição e a maioria das
    contas ainda não foi configurada na IAM); a partir da primeira permissão
    de tela concedida, vale exatamente o que está configurado.

    Existe além do menu porque esconder botão não protege nada: sem esta
    conferência, bastava chamar a rota direto para fazer o que a tela
    escondida faria.
    """
    if e_administrador(usuario):
        return True

    permissoes = usuario.get('permissoes') or []
    prefixo = f'{IAM_NAMESPACE}.tela:'
    if not any(str(p).startswith(prefixo) for p in permissoes):
        return True          # ainda não configurado

    return f'{prefixo}{rota}' in permissoes


def tem_permissao(usuario, codigo):
    """A pessoa tem esta permissão específica da IAM (ex.: 'refeicoes.tela:/deposito')?

    Diferente de equipe_permitida: aqui não tem equipe nenhuma envolvida —
    é pra telas como o depósito do financeiro, que cruzam todas as equipes
    e por isso não fazem sentido dentro do modelo de escopo por equipe/projeto.
    """
    if e_administrador(usuario):
        return True
    return codigo in (usuario.get('permissoes') or [])


# ==========================================================================
# FOTO DE PERFIL
#
# Regra: a foto NOSSA manda. Se a pessoa está em SUPERVISOR_FOTOS, é essa
# que aparece; só quem não está cai no Painel PCP, que por sua vez busca na
# Secullum. Antes ia direto no PCP, então a foto cadastrada aqui — a boa,
# escolhida pela empresa — era ignorada.
#
# Cache em memória porque são poucas linhas e mudam raramente; sem ele,
# cada avatar da tela viraria uma consulta ao banco.
# ==========================================================================
_cache_fotos = {}          # nome normalizado -> url
_cache_fotos_quando = 0.0
_cache_fotos_lock = threading.Lock()
TTL_FOTOS = 600            # 10 minutos


def _chave_nome(nome):
    """Nome comparável: sem acento, sem pontuação, caixa alta, espaço único."""
    import unicodedata
    t = unicodedata.normalize('NFKD', str(nome or ''))
    t = ''.join(c for c in t if not unicodedata.combining(c))
    t = ''.join(c if c.isalnum() or c.isspace() else ' ' for c in t)
    return ' '.join(t.upper().split())


def foto_nossa(nome):
    """URL da foto cadastrada por nós, ou None se não tiver.

    FOTO_PERFIL é a tabela de foto de perfil da empresa — é dela que o
    Painel PCP tira a foto certa das pessoas. SUPERVISOR_FOTOS vem depois,
    como complemento: cobre alguns supervisores que não estão na primeira.
    """
    global _cache_fotos, _cache_fotos_quando

    agora = time.time()
    with _cache_fotos_lock:
        vencido = (agora - _cache_fotos_quando) > TTL_FOTOS

    if vencido:
        mapa = {}

        # Da menos específica para a mais: o que vier depois sobrescreve,
        # então FOTO_PERFIL fica por último de propósito e ganha.
        for l in executar_query("SELECT NOME, URL FROM SUPERVISOR_FOTOS", []) or []:
            url = (l.get('URL') or '').strip()
            if url:
                mapa[_chave_nome(l.get('NOME'))] = url

        for l in executar_query(
                "SELECT NOME, NOME_NORM, URL FROM FOTO_PERFIL "
                "WHERE URL IS NOT NULL AND URL <> ''", []) or []:
            url = (l.get('URL') or '').strip()
            if not url:
                continue
            # Grava sob as duas grafias: NOME_NORM já vem normalizado na
            # tabela, mas nem sempre igual ao NOME que a IAM devolve.
            for candidato in (l.get('NOME'), l.get('NOME_NORM')):
                chave = _chave_nome(candidato)
                if chave:
                    mapa[chave] = url

        with _cache_fotos_lock:
            _cache_fotos = mapa
            _cache_fotos_quando = agora

    with _cache_fotos_lock:
        return _cache_fotos.get(_chave_nome(nome))


def registrar_sistema_na_iam():
    """
    Auto-registro do sistema + telas na IAM (INTEGRACAO.md §5).

    Roda uma vez no boot, em thread separada e sem travar o servidor: se a IAM
    estiver fora do ar, o sistema sobe do mesmo jeito. O manifesto é a fonte da
    verdade das nossas permissões (modo sync).
    """
    if not IAM_REGISTRY_KEY:
        print('ℹ️ IAM_REGISTRY_KEY não configurada — pulando auto-registro na IAM', flush=True)
        return

    manifesto = {
        # A chave em uso é MESTRA (vale para qualquer sistema), então o código
        # precisa vir no corpo. Com uma chave própria do sistema este campo é
        # ignorado — mandar sempre não atrapalha e evita registrar no lugar errado.
        'sistema': IAM_SISTEMA,
        'codigo': IAM_SISTEMA,
        'nome': 'Sistema de Refeições',
        'url_base': os.getenv('APP_URL', ''),
        'modo': 'sync',
        'permissoes': [
            {'codigo': f'{IAM_NAMESPACE}.acesso', 'descricao': 'Entrar no sistema'},
            {'codigo': f'{IAM_NAMESPACE}.tela:/pedido', 'descricao': 'Novo pedido', 'grupo': 'Pedidos'},
            {'codigo': f'{IAM_NAMESPACE}.tela:/temperatura', 'descricao': 'Aferição de temperatura', 'grupo': 'Pedidos'},
            {'codigo': f'{IAM_NAMESPACE}.tela:/historico', 'descricao': 'Histórico de pedidos', 'grupo': 'Pedidos'},
            {'codigo': f'{IAM_NAMESPACE}.tela:/problema', 'descricao': 'Problema com refeição', 'grupo': 'Ocorrências'},
            {'codigo': f'{IAM_NAMESPACE}.tela:/deposito', 'descricao': 'Depósito financeiro (PAGCORP aprovado)', 'grupo': 'Financeiro'},
            {'codigo': f'{IAM_NAMESPACE}.pedido.criar', 'descricao': 'Criar pedido de refeição'},
            {'codigo': f'{IAM_NAMESPACE}.temperatura.aferir', 'descricao': 'Registrar aferição de temperatura'},
            {'codigo': f'{IAM_NAMESPACE}.problema.reportar', 'descricao': 'Reportar problema com refeição'},
        ]
    }

    try:
        response = requests.post(
            f'{IAM_URL}/api/registry/sync',
            headers={'Content-Type': 'application/json', 'X-Registry-Key': IAM_REGISTRY_KEY},
            json=manifesto,
            timeout=15
        )
        if response.ok:
            r = response.json()
            print(f"✅ Sistema registrado na IAM: {r.get('sistema')} "
                  f"(criadas {r.get('criadas')}, atualizadas {r.get('atualizadas')}, "
                  f"removidas {r.get('removidas')})", flush=True)
        else:
            print(f'⚠️ Auto-registro na IAM recusado ({response.status_code}): {response.text[:200]}', flush=True)
    except Exception as e:
        print(f'⚠️ Não foi possível auto-registrar na IAM: {e}', flush=True)

def conectar_azure_sql():
    """Abre uma conexão NOVA com o Azure SQL. Use _obter_conexao() no dia a
    dia — isto aqui só existe pra alimentar o pool (ou pra quem realmente
    precisa de uma conexão fora do pool)."""
    try:
        # Usando pymssql em vez de pyodbc para melhor compatibilidade no Railway
        conn = pymssql.connect(
            server=AZURE_CONFIG['server'],
            database=AZURE_CONFIG['database'],
            user=AZURE_CONFIG['username'],
            password=AZURE_CONFIG['password'],
            timeout=30,  # Timeout de conexão de 30 segundos
            login_timeout=15  # Timeout de login de 15 segundos
        )
        # Sem isto o pymssql abre uma transação implícita a CADA select e só
        # fecha quando a conexão morre. Antes do pool isso passava batido (a
        # conexão era descartada logo em seguida); com o pool, a conexão fica
        # guardada e a transação segue aberta segurando trava no banco —
        # confirmei sessões ociosas com open_transaction_count = 1. Escrita
        # de outro pedido acaba esperando essa trava.
        conn.autocommit(True)
        return conn
    except Exception as e:
        print(f"❌ Erro ao conectar no Azure SQL: {e}")
        import traceback
        traceback.print_exc()
        return None


# ==========================================================================
# POOL DE CONEXÕES
#
# Antes, toda chamada de executar_query() abria uma conexão nova — TCP + TLS
# + login do zero no Azure SQL, sempre, mesmo pra um SELECT de uma linha.
# Isso sozinho custava um a dois segundos POR QUERY, e um único request no
# app às vezes dispara várias (checar coluna, inserir, buscar organograma…).
# Foi a causa raiz do "enviar pedido demorou quase um minuto".
#
# Um pool pequeno resolve: conecta uma vez, reusa. Quando uma conexão falha
# no meio de uma query (rede caiu, Azure fechou por ociosidade), ela é
# descartada e a query tenta de novo com uma conexão nova — uma vez só, pra
# não mascarar um erro real de SQL como se fosse de rede.
# ==========================================================================
POOL_MAX = 8
_pool_conexoes = queue.Queue(maxsize=POOL_MAX)
_pool_criadas = 0
_pool_lock = threading.Lock()


def _obter_conexao():
    """Pega uma conexão do pool, ou cria uma nova se ainda há vaga."""
    global _pool_criadas
    try:
        return _pool_conexoes.get_nowait()
    except queue.Empty:
        pass

    with _pool_lock:
        if _pool_criadas < POOL_MAX:
            conn = conectar_azure_sql()
            if conn is not None:
                _pool_criadas += 1
            return conn

    # Pool cheio: espera alguém devolver em vez de abrir uma conexão a mais.
    # Com prazo — sem ele, um pico de acessos deixava a requisição travada
    # para sempre esperando conexão, sem erro e sem resposta.
    try:
        return _pool_conexoes.get(timeout=20)
    except queue.Empty:
        print('⚠️ Pool de conexões esgotado por 20s', flush=True)
        return None


def _devolver_conexao(conn, com_erro=False):
    """Devolve ao pool — ou descarta, se a conexão pode estar quebrada."""
    global _pool_criadas
    if conn is None:
        return

    if com_erro:
        try:
            conn.close()
        except Exception:
            pass
        with _pool_lock:
            _pool_criadas -= 1
        return

    try:
        _pool_conexoes.put_nowait(conn)
    except queue.Full:
        try:
            conn.close()
        except Exception:
            pass
        with _pool_lock:
            _pool_criadas -= 1

# ==========================================================================
# ENVIO DE IMAGENS PARA O AZURE BLOB
#
# Regra de ouro: uma foto de aferição NUNCA pode sumir em silêncio. Ela é a
# prova de que a refeição foi conferida. Por isso, três camadas:
#
#   1. tentativa imediata, com repetição e espera progressiva;
#   2. fila em disco, para reenviar sozinho o que falhou;
#   3. status honesto na resposta — o app sabe se a foto chegou ou não.
#
# A credencial é conferida no boot: SAS vencido derruba o envio inteiro e é
# exatamente o tipo de coisa que passa meses despercebida.
# ==========================================================================

PASTA_FILA_BLOB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_fila_blob')
MAX_TENTATIVAS_BLOB = 3
TAMANHO_MAX_IMAGEM = 8 * 1024 * 1024   # 8MB


def _blob_configurado():
    return all(AZURE_BLOB_CONFIG.values())


def diagnosticar_sas():
    """
    Lê validade e permissões do SAS token.
    Devolve (ok, mensagem). Nunca imprime a assinatura.
    """
    token = AZURE_BLOB_CONFIG.get('sas_token') or ''
    if not token:
        return False, 'AZURE_SAS_TOKEN não configurado'

    p = urllib.parse.parse_qs(token.lstrip('?'))

    expira = (p.get('se') or [''])[0]
    if expira:
        try:
            from datetime import timezone
            dt = datetime.fromisoformat(expira.replace('Z', '+00:00'))
            agora = datetime.now(timezone.utc)
            if dt < agora:
                return False, f'SAS token VENCIDO em {dt:%d/%m/%Y} — gere um novo no portal do Azure'
            dias = (dt - agora).days
            if dias <= 15:
                return True, f'SAS token vence em {dias} dia(s) ({dt:%d/%m/%Y}) — renove antes'
        except Exception:
            pass

    permissoes = (p.get('sp') or [''])[0]
    if permissoes and not any(c in permissoes for c in ('w', 'c', 'a')):
        return False, f"SAS token sem permissão de escrita (sp={permissoes})"

    return True, 'SAS token válido'


def _enviar_bytes_blob(imagem_bytes, nome_unico):
    """
    Uma tentativa de PUT no blob.
    Devolve (url, None) em caso de sucesso, ou (None, motivo).
    """
    url_upload = (f"https://{AZURE_BLOB_CONFIG['account_name']}.blob.core.windows.net/"
                  f"{AZURE_BLOB_CONFIG['container_name']}/{nome_unico}"
                  f"?{AZURE_BLOB_CONFIG['sas_token'].lstrip('?')}")

    resposta = requests.put(
        url_upload,
        data=imagem_bytes,
        headers={'x-ms-blob-type': 'BlockBlob', 'Content-Type': 'image/jpeg'},
        timeout=20
    )

    if resposta.status_code in (200, 201):
        return (f"https://{AZURE_BLOB_CONFIG['account_name']}.blob.core.windows.net/"
                f"{AZURE_BLOB_CONFIG['container_name']}/{nome_unico}"), None

    # 403 quase sempre é SAS vencido ou sem permissão — vale nomear
    detalhe = resposta.text[:160].replace('\n', ' ')
    if resposta.status_code == 403:
        return None, f'403 negado pelo Azure (SAS vencido ou sem permissão): {detalhe}'
    return None, f'HTTP {resposta.status_code}: {detalhe}'


def _decodificar_imagem(imagem_base64):
    """base64 (com ou sem prefixo data:) -> bytes. Levanta ValueError se inválida."""
    if not imagem_base64:
        raise ValueError('imagem vazia')
    if ',' in imagem_base64:
        imagem_base64 = imagem_base64.split(',', 1)[1]

    dados = base64.b64decode(imagem_base64)
    if len(dados) > TAMANHO_MAX_IMAGEM:
        raise ValueError(f'imagem grande demais ({len(dados)/1024/1024:.1f}MB)')
    return dados


def upload_imagem_blob(imagem_base64, nome_arquivo, tentativas=MAX_TENTATIVAS_BLOB):
    """
    Sobe uma imagem para o blob, repetindo em caso de falha temporária.

    Devolve (url, None) em caso de sucesso ou (None, motivo) quando desiste.
    Nunca devolve string de "sucesso falso": quem chama precisa distinguir.
    """
    if not _blob_configurado():
        return None, 'Azure Blob não configurado (conta, container ou SAS ausentes)'

    try:
        dados = _decodificar_imagem(imagem_base64)
    except Exception as e:
        return None, f'imagem inválida: {e}'

    brasilia = pytz.timezone('America/Sao_Paulo')
    nome_unico = f"temp_{datetime.now(brasilia).strftime('%Y%m%d_%H%M%S_%f')}_{nome_arquivo}"

    print(f'📷 Enviando {nome_arquivo} ({len(dados)/1024:.0f}KB) para o blob…', flush=True)

    ultimo_erro = 'desconhecido'
    for tentativa in range(1, tentativas + 1):
        try:
            url, erro = _enviar_bytes_blob(dados, nome_unico)
            if url:
                print(f'✅ Blob OK ({tentativa}ª tentativa): {nome_unico}', flush=True)
                return url, None

            ultimo_erro = erro
            # 403 é problema de credencial: repetir não resolve
            if erro and erro.startswith('403'):
                print(f'❌ {erro}', flush=True)
                return None, erro

        except requests.exceptions.RequestException as e:
            ultimo_erro = f'rede: {e}'

        if tentativa < tentativas:
            espera = 2 ** tentativa          # 2s, 4s
            print(f'⏳ Tentativa {tentativa} falhou ({ultimo_erro}); repetindo em {espera}s', flush=True)
            time.sleep(espera)

    print(f'❌ Desisti de {nome_arquivo} após {tentativas} tentativas: {ultimo_erro}', flush=True)
    return None, ultimo_erro


# --------------------------------------------------------------------------
# FILA EM DISCO
#
# O que não subiu fica gravado e é reenviado sozinho. Não é eterno (no Railway
# o disco some a cada deploy), mas cobre o caso comum: Azure ou rede fora do ar
# por alguns minutos. A garantia de longo prazo é a fila do próprio aparelho,
# que só apaga a foto quando o servidor confirma que ela chegou.
# --------------------------------------------------------------------------

def enfileirar_blob(pedido_id, campo, imagem_base64):
    """Guarda uma imagem que falhou, para tentar de novo depois."""
    try:
        os.makedirs(PASTA_FILA_BLOB, exist_ok=True)
        caminho = os.path.join(PASTA_FILA_BLOB, f'{pedido_id}__{campo}.txt')
        with open(caminho, 'w', encoding='utf-8') as f:
            f.write(imagem_base64)
        print(f'📥 {campo} do pedido {pedido_id} guardado na fila de reenvio', flush=True)
        return True
    except Exception as e:
        print(f'❌ Não consegui enfileirar {campo} do pedido {pedido_id}: {e}', flush=True)
        return False


def processar_fila_blob():
    """Reenvia o que está na fila e grava a URL no banco quando dá certo."""
    if not os.path.isdir(PASTA_FILA_BLOB):
        return 0, 0

    ok = falhou = 0
    for arquivo in sorted(os.listdir(PASTA_FILA_BLOB)):
        if not arquivo.endswith('.txt'):
            continue

        caminho = os.path.join(PASTA_FILA_BLOB, arquivo)
        try:
            pedido_id, campo = arquivo[:-4].split('__', 1)
            with open(caminho, encoding='utf-8') as f:
                imagem = f.read()

            url, erro = upload_imagem_blob(imagem, f'{campo.lower()}_pedido_{pedido_id}.jpg',
                                           tentativas=1)
            if not url:
                falhou += 1
                continue

            executar_query(f'UPDATE PEDIDOS SET {campo} = %s WHERE ID = %s', [url, int(pedido_id)])
            os.remove(caminho)
            ok += 1
            print(f'✅ Reenvio concluído: {campo} do pedido {pedido_id}', flush=True)

        except Exception as e:
            falhou += 1
            print(f'❌ Erro ao reprocessar {arquivo}: {e}', flush=True)

    if ok or falhou:
        print(f'📤 Fila do blob: {ok} enviada(s), {falhou} pendente(s)', flush=True)
    return ok, falhou


def _worker_fila_blob():
    """Tenta a fila de tempos em tempos, em segundo plano."""
    while True:
        time.sleep(300)   # 5 min
        try:
            processar_fila_blob()
        except Exception as e:
            print(f'❌ Worker da fila do blob: {e}', flush=True)


def _executar_query_uma_vez(conn, query, params):
    """A tentativa de fato. Levanta exceção pra quem chama decidir se
    descarta a conexão (rede) ou não (erro de SQL, não adianta repetir)."""
    cursor = conn.cursor()
    if params:
        cursor.execute(query, params)
    else:
        cursor.execute(query)

    if query.strip().upper().startswith('SELECT'):
        columns = [column[0] for column in cursor.description]
        results = []
        for row in cursor.fetchall():
            row_dict = {}
            for i, value in enumerate(row):
                row_dict[columns[i]] = value
            results.append(row_dict)
        return results
    elif query.strip().upper().startswith('INSERT'):
        conn.commit()
        cursor.execute("SELECT @@IDENTITY AS id")
        inserted_id = cursor.fetchone()[0]
        return {"rowcount": cursor.rowcount, "inserted_id": int(inserted_id)}
    else:
        conn.commit()
        return cursor.rowcount


def executar_query(query, params=None):
    """Executa uma query no Azure SQL usando uma conexão do pool.

    Uma tentativa extra existe só pro caso "a conexão estava parada há
    tempo e o Azure já tinha fechado ela" — não é retry de erro de SQL
    (sintaxe errada continua errada na segunda vez também)."""
    for tentativa in (1, 2):
        conn = _obter_conexao()
        if conn is None:
            return None

        try:
            resultado = _executar_query_uma_vez(conn, query, params)
            _devolver_conexao(conn)
            return resultado

        except (pymssql.OperationalError, pymssql.InterfaceError) as e:
            # Cheira a conexão morta, não a query errada: descarta e tenta
            # de novo com uma conexão nova, silenciosamente na primeira vez.
            _devolver_conexao(conn, com_erro=True)
            if tentativa == 2:
                print(f"❌ Erro de conexão persistente: {e}")
                return None
            continue

        except Exception as e:
            _devolver_conexao(conn, com_erro=True)
            print(f"❌ Erro ao executar query: {e}")
            print(f"❌ Tipo do erro: {type(e).__name__}")
            print(f"❌ Query que falhou: {query[:200]}...")
            if params:
                print(f"❌ Parâmetros: {len(params) if params else 0} itens")
                if len(params) <= 10:
                    print(f"❌ Parâmetros detalhados: {params}")
            import traceback
            print(f"❌ Stack trace: {traceback.format_exc()}")
            return None

class RefeicaoHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        """Override para evitar crash em log quando pipe quebra"""
        try:
            super().log_message(format, *args)
        except BrokenPipeError:
            pass

    def serve_html_file(self, filename):
        """Serve arquivos HTML estáticos"""
        try:
            with open(filename, 'r', encoding='utf-8') as file:
                content = file.read()

            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content.encode('utf-8'))

        except BrokenPipeError:
            # Cliente desconectou antes de receber a resposta completa - ignorar
            pass
        except FileNotFoundError:
            self.send_response(404)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h1>404 - Arquivo nao encontrado</h1>')
        except Exception as e:
            try:
                self.send_response(500)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(f'<h1>500 - Erro do servidor: {str(e)}</h1>'.encode('utf-8'))
            except BrokenPipeError:
                pass

    def serve_static_file(self, filename, content_type):
        """Serve arquivos estáticos (JS, JSON, CSS) com MIME type correto"""
        try:
            # Determinar modo de leitura baseado no tipo
            mode = 'r' if content_type.startswith('text/') or content_type == 'application/json' or content_type == 'application/javascript' else 'rb'
            encoding = 'utf-8' if mode == 'r' else None

            with open(filename, mode, encoding=encoding) as file:
                content = file.read()

            self.send_response(200)
            self.send_header('Content-type', f'{content_type}; charset=utf-8' if encoding else content_type)
            self.send_header('Access-Control-Allow-Origin', '*')
            # Sem isto o navegador guarda CSS/JS por conta própria (cache HTTP
            # comum, fora do Service Worker) e ignora edições no servidor até
            # o cache heurístico expirar sozinho — o SW faz "rede primeiro",
            # mas essa rede primeiro ainda passa pelo cache HTTP do navegador.
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()

            if encoding:
                self.wfile.write(content.encode('utf-8'))
            else:
                self.wfile.write(content)

        except BrokenPipeError:
            pass
        except FileNotFoundError:
            self.send_response(404)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h1>404 - Arquivo nao encontrado</h1>')
        except Exception as e:
            try:
                self.send_response(500)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(f'<h1>500 - Erro do servidor: {str(e)}</h1>'.encode('utf-8'))
            except BrokenPipeError:
                pass

    # ======================================================================
    # AUTENTICAÇÃO (IAM Larsil)
    # ======================================================================

    def _enviar_json(self, payload, status=200):
        """Resposta JSON completa (headers + corpo). Use para erros e atalhos."""
        try:
            corpo = json.dumps(payload, ensure_ascii=False, default=decimal_default).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Equipe')
            self.send_header('Content-Length', str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        except BrokenPipeError:
            pass

    def _token_do_header(self):
        cabecalho = self.headers.get('Authorization', '')
        if cabecalho.startswith('Bearer '):
            return cabecalho[7:].strip()
        return None

    def _exigir_autenticacao(self, path):
        """
        Garante que a requisição tem um token válido da IAM.

        Devolve True quando pode seguir. Quando não pode, JÁ respondeu ao
        cliente (401/403/502) e o chamador deve apenas retornar.
        Em caso de sucesso, deixa a identidade em self.usuario e o token em
        self.token.
        """
        self.usuario = None
        self.token = None

        # Quando chegamos aqui, os arquivos estáticos já foram servidos e
        # retornaram: o que sobra é endpoint. Por isso a regra é "protege tudo,
        # menos o que está explicitamente na lista pública" — assim uma rota
        # fora do prefixo /api (como /upload-blob) não escapa por descuido.
        if path in PUBLIC_API_PATHS:
            return True

        # Foto de perfil precisa ser pública: quem busca é a tag <img>, e ela
        # não manda cabeçalho de autorização. Só devolve um redirecionamento
        # para a imagem — o mesmo que o Painel PCP já servia aberto.
        if path.startswith('/api/foto/'):
            return True

        token = self._token_do_header()
        if not token:
            self._enviar_json({'error': True, 'message': 'Não autenticado'}, status=401)
            return False

        try:
            usuario = resolver_usuario(token)
        except ErroIAM as e:
            self._enviar_json(
                {'error': True, 'message': e.mensagem, 'motivo': e.motivo},
                status=e.status if e.status in (401, 403, 502) else 401
            )
            return False

        if IAM_EXIGIR_ACESSO and not ve_tudo(usuario) \
                and PERMISSAO_ACESSO not in (usuario.get('permissoes') or []):
            self._enviar_json({
                'error': True,
                'message': 'Você não tem acesso ao sistema de refeições. Peça liberação à TI.'
            }, status=403)
            return False

        self.usuario = usuario
        self.token = token
        return True

    def _equipe_ativa(self, query_params=None):
        """
        A equipe que a requisição pode operar.

        Vem do header X-Equipe (a equipe escolhida no app) e, por
        compatibilidade com as telas ainda não migradas, cai para o parâmetro
        `equipe` da query. Nos dois casos ela é CONFERIDA contra o escopo do
        token — nunca confiamos no que o cliente manda.

        Devolve (equipe, None) ou (None, mensagem_de_erro).
        """
        usuario = getattr(self, 'usuario', None)
        if not usuario:
            return None, 'Não autenticado'

        pedida = (self.headers.get('X-Equipe') or '').strip().upper()
        if not pedida and query_params:
            pedida = (query_params.get('equipe', [''])[0] or '').strip().upper()

        if not pedida:
            # Sem escolha explícita: se houver uma equipe só no escopo, usa ela
            equipes, _ = equipes_do_escopo(usuario)
            if len(equipes) == 1:
                return next(iter(equipes)), None
            return None, 'Equipe não informada'

        if not equipe_permitida(usuario, pedida):
            return None, f'Equipe {pedida} fora do seu escopo de acesso'

        return pedida, None

    def do_GET(self):
        # Parse da URL
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query_params = urllib.parse.parse_qs(parsed_path.query)
        
        # Foto de perfil: a nossa (SUPERVISOR_FOTOS) tem prioridade; quem não
        # está lá cai no Painel PCP, que busca na Secullum. Responde com
        # redirecionamento para a <img> seguir sozinha, sem proxy de bytes.
        if path.startswith('/api/foto/'):
            nome = urllib.parse.unquote(path[len('/api/foto/'):]).strip()
            try:
                destino = foto_nossa(nome) or f'{PCP_URL}/api/foto/{urllib.parse.quote(nome)}'
            except Exception as e:
                print(f'⚠️ Foto de {nome}: {e}', flush=True)
                destino = f'{PCP_URL}/api/foto/{urllib.parse.quote(nome)}'

            self.send_response(302)
            self.send_header('Location', destino)
            self.send_header('Access-Control-Allow-Origin', '*')
            # Curto: se cadastrarem a foto agora, aparece no mesmo dia
            self.send_header('Cache-Control', 'public, max-age=600')
            self.end_headers()
            return

        # 🛡️ HEALTH CHECK - Railway usa isso para verificar se o servidor está vivo
        if path == '/health' or path == '/healthz' or path == '/_health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            response = {"status": "ok", "timestamp": datetime.now().isoformat()}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return
        
        # Servir arquivos estáticos.
        # A raiz vai direto ao login (que já reencaminha quem tem sessão) —
        # um hop a menos que passar pelo roteador do index.html.
        if path == '/':
            self.serve_html_file('login.html')
            return
        elif path.endswith('.html'):
            self.serve_html_file(path[1:])  # Remove a / inicial
            return
        elif path.endswith('.js'):
            self.serve_static_file(path[1:], 'application/javascript')
            return
        elif path.endswith('.json'):
            self.serve_static_file(path[1:], 'application/json')
            return
        elif path.endswith('.css'):
            self.serve_static_file(path[1:], 'text/css')
            return
        elif path.endswith('.png'):
            self.serve_static_file(path[1:], 'image/png')
            return
        elif path.endswith('.svg'):
            self.serve_static_file(path[1:], 'image/svg+xml')
            return

        # 🔒 PORTÃO: tudo sob /api exige token da IAM, menos o que está em
        # PUBLIC_API_PATHS. Rota nova nasce protegida.
        if not self._exigir_autenticacao(path):
            return

        # 🔒 PERMISSÃO DE TELA — AQUI, antes dos cabeçalhos.
        #
        # Tem de vir antes do send_response(200) logo abaixo: uma vez que o
        # 200 saiu, responder 403 mais adiante escreve uma SEGUNDA resposta
        # dentro do corpo da primeira, e o navegador recebe um JSON que
        # começa com "HTTP/1.0 403..." e quebra na hora de interpretar.
        TELA_DA_ROTA = {
            '/api/historico-pedidos': '/historico',
            '/api/pedidos-pendentes-temperatura': '/temperatura',
            '/api/deposito-financeiro': '/deposito',
        }
        tela_exigida = TELA_DA_ROTA.get(path)
        if tela_exigida and not tela_permitida(self.usuario, tela_exigida):
            self._enviar_json(
                {"error": True, "message": "Você não tem acesso a esta tela"}, status=403)
            return

        # Headers CORS para APIs
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Equipe')
        self.end_headers()

        # APIs simuladas
        if path == '/api/config':
            # Configuração PÚBLICA do frontend — só URLs, nenhum segredo.
            # A tela de login precisa da base das fotos antes de existir token.
            response = {
                # Nosso resolvedor, não o PCP direto: ele consulta primeiro a
                # foto cadastrada por nós e só cai na Secullum se não houver.
                "fotoBaseUrl": "/api/foto",
                "sistema": IAM_SISTEMA,
                "aprovador": NOME_APROVADOR,
                "iamUrl": IAM_URL,
                "EMAILJS_PUBLIC_KEY": os.getenv('EMAILJS_PUBLIC_KEY', ''),
                "EMAILJS_SERVICE_ID": os.getenv('EMAILJS_SERVICE_ID', ''),
                "EMAILJS_TEMPLATE_ID": os.getenv('EMAILJS_TEMPLATE_ID', '')
            }

        elif path == '/api/auth/verify':
            # O app chama isto ao abrir, ao voltar do segundo plano e a cada
            # minuto — é o que faz liberar/negar uma tela na IAM valer sem
            # pedir novo login.
            #
            # Ignora o cache de propósito: as demais rotas reaproveitam o
            # resolve por 60s (senão toda requisição viraria uma ida à IAM),
            # mas esta existe justamente para perguntar de novo. Sem furar o
            # cache, tirar um acesso ainda demoraria mais um minuto para
            # valer, e a pergunta é "e agora, o que eu posso?".
            usuario_atual = self.usuario
            try:
                invalidar_cache_token(self.token)
                usuario_atual = resolver_usuario(self.token)
            except ErroIAM as e:
                # IAM fora do ar não derruba quem já está dentro: segue com o
                # que temos e tenta de novo no próximo minuto.
                print(f'⚠️ verify: IAM não respondeu ({e.mensagem}) — mantendo acesso atual',
                      flush=True)

            response = {
                "error": False,
                "user": usuario_atual,
                "equipes": resolver_equipes_do_usuario(usuario_atual),
                "verTudo": ve_tudo(usuario_atual),
                "fotoBaseUrl": "/api/foto"
            }

        elif path == '/api/minhas-equipes':
            # Quais equipes esta pessoa opera.
            #
            # Não dá para o front resolver isso sozinho: o escopo mais comum de
            # liderança é COORDENADOR/SUPERVISOR pelo NOME, e só o ORGANOGRAMA
            # sabe traduzir esse nome em códigos de equipe.
            try:
                equipes = resolver_equipes_do_usuario(self.usuario)
                response = {
                    "error": False,
                    "total": len(equipes),
                    "equipes": equipes,
                    "verTudo": ve_tudo(self.usuario),
                    "escopos": self.usuario.get('escopos') or []
                }
            except Exception as e:
                print(f"❌ Erro ao resolver equipes: {e}", flush=True)
                response = {"error": True, "message": f"Erro ao resolver equipes: {e}"}

        elif path == '/api/resumo-equipes':
            # Retrato de TODAS as equipes da pessoa de uma vez: quantas
            # aferições estão pendentes e quando foi o último pedido.
            #
            # Um endpoint só porque são três telas com a mesma pergunta: a
            # etapa de escolha de equipe no pedido (marca em verde quem já
            # pediu e mostra a data), o filtro das pendências (o número ao
            # lado de cada equipe) e o total do menu. Pedir de novo em cada
            # tela seria N idas ao banco para a mesma informação.
            try:
                equipes = resolver_equipes_do_usuario(self.usuario)
                codigos = [e['equipe'] for e in equipes if e.get('equipe')]

                pendentes_por_equipe = {}
                ultimo_por_equipe = {}

                if codigos:
                    marcadores = ', '.join(['%s'] * len(codigos))

                    # Mesmo critério da tela de pendências (MARMITEX sem
                    # aferição nos últimos 7 dias) — se divergir, o número do
                    # menu não bate com a lista, que é pior que não ter número.
                    for linha in executar_query(f"""
                        SELECT LIDER, COUNT(*) AS QTD
                        FROM PEDIDOS
                        WHERE (TIPO_REFEICAO LIKE '%%MARMITEX%%' OR TIPO_REFEICAO LIKE '%%MARMITA%%')
                          AND (AFERIU_TEMPERATURA IS NULL OR AFERIU_TEMPERATURA = ''
                               OR AFERIU_TEMPERATURA = 'NAO')
                          AND DATA_RETIRADA >= DATEADD(day, -7, GETDATE())
                          AND LIDER IN ({marcadores})
                        GROUP BY LIDER
                    """, codigos) or []:
                        pendentes_por_equipe[linha['LIDER']] = int(linha['QTD'] or 0)

                    for linha in executar_query(f"""
                        SELECT LIDER, MAX(DATA_RETIRADA) AS ULTIMA
                        FROM PEDIDOS
                        WHERE LIDER IN ({marcadores})
                        GROUP BY LIDER
                    """, codigos) or []:
                        d = linha['ULTIMA']
                        ultimo_por_equipe[linha['LIDER']] = {
                            'iso': d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d or ''),
                            'br': d.strftime('%d/%m/%Y') if hasattr(d, 'strftime') else '',
                        }

                lista = []
                for e in equipes:
                    cod = e.get('equipe') or ''
                    ult = ultimo_por_equipe.get(cod) or {}
                    lista.append({
                        'equipe': cod,
                        'projeto': e.get('projeto') or '',
                        'lider': e.get('lider') or '',
                        'pendencias': pendentes_por_equipe.get(cod, 0),
                        'ultimo_pedido': ult.get('br', ''),
                        'ultimo_pedido_iso': ult.get('iso', ''),
                    })

                response = {
                    "error": False,
                    "equipes": lista,
                    "total_pendencias": sum(pendentes_por_equipe.values()),
                }
            except Exception as e:
                print(f"❌ Erro no resumo de equipes: {e}", flush=True)
                response = {"error": True, "message": str(e)}

        elif path == '/api/teste-conexao':
            response = {
                "success": True,
                "message": "Servidor Python funcionando!",
                "timestamp": datetime.now(pytz.timezone('America/Sao_Paulo')).isoformat()
            }
            
        elif path == '/api/debug-azure':
            # Diagnóstico de configuração. Diz se está configurado, NUNCA o valor:
            # o SAS token dá escrita no blob e não pode sair daqui.
            response = {
                "azure_blob": {
                    "account_name": AZURE_BLOB_CONFIG['account_name'] or 'NÃO DEFINIDA',
                    "container_name": AZURE_BLOB_CONFIG['container_name'] or 'NÃO DEFINIDA',
                    "sas_token_configurado": bool(AZURE_BLOB_CONFIG['sas_token'])
                },
                "iam": {
                    "url": IAM_URL,
                    "sistema": IAM_SISTEMA,
                    "exigir_acesso": IAM_EXIGIR_ACESSO,
                    "registry_key_configurada": bool(IAM_REGISTRY_KEY)
                },
                "timestamp": datetime.now(pytz.timezone('America/Sao_Paulo')).isoformat()
            }

        elif path == '/api/fornecedores':
            # O projeto sai da EQUIPE ATIVA (validada contra o escopo do token),
            # não do que o cliente mandar na query.
            equipe_ativa, erro_escopo = self._equipe_ativa(query_params)

            if equipe_ativa:
                projeto = ''.join(c for c in equipe_ativa if c.isdigit())
            elif ve_tudo(self.usuario):
                projeto = query_params.get('projeto', [''])[0]
            else:
                projeto = ''

            if not projeto:
                response = {"error": True, "message": erro_escopo or "Não foi possível determinar o projeto"}
            else:
                # Buscar fornecedores reais do Azure SQL
                query = """
                SELECT ID, PROJETO, LOCAL, FORNECEDOR, TIPO_FORN, VALOR, STATUS,
                       ISNULL(FECHAMENTO, '') as FECHAMENTO,
                       ISNULL(LOCAL, '') as FAZENDA
                FROM tb_fornecedores 
                WHERE PROJETO = %s AND STATUS = 'ATIVO'
                ORDER BY TIPO_FORN, FORNECEDOR
                """
                
                fornecedores_reais = executar_query(query, [projeto])
                
                if fornecedores_reais is not None:
                    response = {
                        "error": False,
                        "projeto": projeto,
                        "total": len(fornecedores_reais),
                        "fornecedores": fornecedores_reais
                    }
                else:
                    # Fallback para dados simulados
                    fornecedores_mock = [
                        {"ID": 1, "PROJETO": "700", "LOCAL": "TESTE", "FORNECEDOR": "FORNECEDOR TESTE", "TIPO_FORN": "CM", "VALOR": 5.50, "STATUS": "ATIVO", "FECHAMENTO": "TESTE FECHAMENTO", "FAZENDA": "FAZENDA TESTE"}
                    ]
                    fornecedores_filtrados = [f for f in fornecedores_mock if f["PROJETO"] == projeto]
                    response = {
                        "error": False,
                        "projeto": projeto,
                        "total": len(fornecedores_filtrados),
                        "fornecedores": fornecedores_filtrados,
                        "warning": "Usando dados simulados - erro na conexão com Azure SQL"
                    }
            
        elif path == '/api/organograma':
            # Idem fornecedores: projeto/equipe vêm do escopo, não da query
            equipe_ativa, erro_escopo = self._equipe_ativa(query_params)

            if equipe_ativa:
                equipe = equipe_ativa
                projeto = ''.join(c for c in equipe_ativa if c.isdigit())
            elif ve_tudo(self.usuario):
                projeto = query_params.get('projeto', [''])[0]
                equipe = query_params.get('equipe', [''])[0]
            else:
                projeto, equipe = '', ''

            if not projeto:
                response = {"error": True, "message": erro_escopo or "Não foi possível determinar o projeto"}
            else:
                # Buscar organograma real do Azure SQL
                if equipe:
                    # Se equipe foi informada, filtrar por projeto E equipe
                    query = """
                    SELECT ID, PROJETO, EQUIPE, LIDER, COORDENADOR, SUPERVISOR 
                    FROM ORGANOGRAMA 
                    WHERE PROJETO = %s AND EQUIPE = %s
                    ORDER BY EQUIPE
                    """
                    organograma_real = executar_query(query, [projeto, equipe])
                else:
                    # Se apenas projeto foi informado, buscar todas as equipes do projeto
                    query = """
                    SELECT ID, PROJETO, EQUIPE, LIDER, COORDENADOR, SUPERVISOR 
                    FROM ORGANOGRAMA 
                    WHERE PROJETO = %s
                    ORDER BY EQUIPE
                    """
                    organograma_real = executar_query(query, [projeto])
                
                if organograma_real is not None:
                    response = {
                        "error": False,
                        "projeto": projeto,
                        "equipe": equipe if equipe else "TODAS",
                        "total": len(organograma_real),
                        "organograma": organograma_real
                    }
                else:
                    # Fallback para dados simulados
                    organograma_mock = [
                        {"ID": 1, "PROJETO": "700", "EQUIPE": "700TA", "LIDER": "TESTE LIDER", "COORDENADOR": "TESTE COORD", "SUPERVISOR": "TESTE SUPER"}
                    ]
                    if equipe:
                        org_filtrado = [o for o in organograma_mock if o["PROJETO"] == projeto and o["EQUIPE"] == equipe]
                    else:
                        org_filtrado = [o for o in organograma_mock if o["PROJETO"] == projeto]
                    
                    response = {
                        "error": False,
                        "projeto": projeto,
                        "equipe": equipe if equipe else "TODAS",
                        "total": len(org_filtrado),
                        "organograma": org_filtrado,
                        "warning": "Usando dados simulados - erro na conexão com Azure SQL"
                    }
            
        elif path == '/api/colaboradores':
            equipe, erro_escopo = self._equipe_ativa(query_params)

            if not equipe:
                response = {"error": True, "message": erro_escopo or "Equipe não informada"}
            else:
                # Buscar colaboradores reais baseado na EQUIPE, em ordem alfabética
                # CLASSE = 'LDF' identifica líderes para destaque especial
                query = """
                SELECT ID, EQUIPE, NOME, FUNCAO, PROJETO, COORDENADOR, SUPERVISOR, CLASSE 
                FROM COLABORADORES 
                WHERE EQUIPE = %s
                ORDER BY NOME
                """
                
                colaboradores_reais = executar_query(query, [equipe])
                
                if colaboradores_reais is not None:
                    # Destacar líderes (classe LDF) adicionando flag especial
                    for colaborador in colaboradores_reais:
                        colaborador['IS_LIDER'] = colaborador.get('CLASSE') == 'LDF'
                    
                    response = {
                        "error": False,
                        "equipe": equipe,
                        "total": len(colaboradores_reais),
                        "colaboradores": colaboradores_reais,
                        "message": f"Colaboradores da equipe {equipe} carregados com sucesso!"
                    }
                else:
                    # Fallback para dados simulados
                    colaboradores_mock = [
                        {"ID": 1, "EQUIPE": equipe, "NOME": "COLABORADOR TESTE", "FUNCAO": "TESTE", "CLASSE": "COL", "IS_LIDER": False}
                    ]
                    response = {
                        "error": False,
                        "equipe": equipe,
                        "total": len(colaboradores_mock),
                        "colaboradores": colaboradores_mock,
                        "warning": "Usando dados simulados - erro na conexão com Azure SQL"
                    }
            
        elif path == '/api/historico-pedidos':
            # Pedidos já feitos pela equipe, do mais recente para o mais antigo.
            #
            # Traz as URLs das fotos junto: o histórico é onde alguém vai
            # conferir se a aferição de um dia específico realmente aconteceu,
            # e a foto é a prova disso.
            equipe_hist, erro_escopo = self._equipe_ativa(query_params)

            if not equipe_hist:
                response = {"error": True, "message": erro_escopo or "Equipe não informada"}
            else:
                try:
                    limite = int(query_params.get('limite', ['60'])[0])
                except (TypeError, ValueError):
                    limite = 60
                limite = max(10, min(limite, 300))

                # Filtro por período (dd range escolhido na tela). Datas soltas
                # de query string, nunca concatenadas na query — vão como
                # parâmetro como tudo o mais aqui.
                de = (query_params.get('de', ['']) or [''])[0].strip()
                ate = (query_params.get('ate', ['']) or [''])[0].strip()

                condicoes = ["LIDER = %s"]
                parametros = [equipe_hist]
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}', de):
                    condicoes.append("CAST(DATA_RETIRADA AS DATE) >= %s")
                    parametros.append(de)
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}', ate):
                    condicoes.append("CAST(DATA_RETIRADA AS DATE) <= %s")
                    parametros.append(ate)

                pedidos = executar_query(f"""
                    SELECT TOP {limite}
                        ID, DATA_RETIRADA, DATA_ENVIO1, TIPO_REFEICAO, FORNECEDOR,
                        VALOR_PAGO, TOTAL_COLABORADORES, A_CONTRATAR, TOTAL_PAGAR,
                        COLABORADORES, FAZENDA, CIDADE_PRESTACAO_DO_SERVICO,
                        PAGCORP, RESPONSAVEL_PELO_CARTAO, NOME_LIDER,
                        ISNULL(APROVADO, '') AS APROVADO,
                        ISNULL(APROVADO_POR, '') AS APROVADO_POR,
                        AFERIU_TEMPERATURA,
                        TEMPERATURA_RETIRADA, TEMPERATURA_CONSUMO,
                        HORA_RETIRADA, HORA_CONSUMO,
                        IMG_RETIRADA, IMG_CONSUMO,
                        OBSERVACOES
                    FROM PEDIDOS
                    WHERE {' AND '.join(condicoes)}
                    ORDER BY DATA_RETIRADA DESC, ID DESC
                """, parametros) or []

                # Agrupa por DIA: um pedido de café + almoço + janta são três
                # linhas na tabela, mas para quem pediu foi um pedido só.
                dias = {}
                for p in pedidos:
                    data = p.get('DATA_RETIRADA')
                    chave = data.strftime('%Y-%m-%d') if hasattr(data, 'strftime') else str(data)

                    if chave not in dias:
                        dias[chave] = {
                            'data': chave,
                            'data_br': data.strftime('%d/%m/%Y') if hasattr(data, 'strftime') else str(data),
                            'fazenda': p.get('FAZENDA') or '',
                            'cidade': p.get('CIDADE_PRESTACAO_DO_SERVICO') or '',
                            'pagcorp': p.get('PAGCORP') or '',
                            'solicitante': p.get('NOME_LIDER') or '',
                            'aprovado': p.get('APROVADO') or '',
                            'total': 0.0,
                            'pessoas': 0,
                            'itens': [],
                        }

                    d = dias[chave]

                    try:
                        d['total'] += float(p.get('TOTAL_PAGAR') or 0)
                    except (TypeError, ValueError):
                        pass

                    # Todas as refeições do dia têm a mesma turma: não somar
                    try:
                        d['pessoas'] = max(d['pessoas'], int(p.get('TOTAL_COLABORADORES') or 0))
                    except (TypeError, ValueError):
                        pass

                    def hora(v):
                        return v.strftime('%H:%M') if hasattr(v, 'strftime') else (str(v) if v else '')

                    d['itens'].append({
                        'id': int(p['ID']),
                        'tipo': p.get('TIPO_REFEICAO') or '',
                        'fornecedor': p.get('FORNECEDOR') or '',
                        'valor': float(p.get('VALOR_PAGO') or 0),
                        'pessoas': int(p.get('TOTAL_COLABORADORES') or 0),
                        'a_contratar': int(p.get('A_CONTRATAR') or 0),
                        'total': float(p.get('TOTAL_PAGAR') or 0),
                        'colaboradores': p.get('COLABORADORES') or '',
                        'aprovado': p.get('APROVADO') or '',
                        'aprovado_por': p.get('APROVADO_POR') or '',
                        'aferiu': p.get('AFERIU_TEMPERATURA') or '',
                        'temp_retirada': p.get('TEMPERATURA_RETIRADA'),
                        'temp_consumo': p.get('TEMPERATURA_CONSUMO'),
                        'hora_retirada': hora(p.get('HORA_RETIRADA')),
                        'hora_consumo': hora(p.get('HORA_CONSUMO')),
                        'img_retirada': p.get('IMG_RETIRADA') or '',
                        'img_consumo': p.get('IMG_CONSUMO') or '',
                        'observacoes': p.get('OBSERVACOES') or '',
                    })

                lista = sorted(dias.values(), key=lambda x: x['data'], reverse=True)

                response = {
                    "error": False,
                    "equipe": equipe_hist,
                    "total_dias": len(lista),
                    "total_pedidos": len(pedidos),
                    "dias": lista,
                }

        elif path == '/api/deposito-financeiro':
            # Fila do financeiro: pedidos PAGCORP já aprovados pela Elaine,
            # ainda sem depósito. Fechamento nunca aparece aqui — não tem
            # cartão, não tem saldo pra repor. Cruza equipes de propósito:
            # quem faz depósito não trabalha por equipe, trabalha por fila.
            # DEPOSITADO guarda a palavra 'DEPOSITADO', não 'SIM' — é a
            # convenção do sistema antigo, com milhares de linhas assim.
            NAO_DEPOSITADO = ("(DEPOSITADO IS NULL OR LTRIM(RTRIM(DEPOSITADO)) "
                              "NOT IN ('DEPOSITADO', 'SIM'))")

            status = (query_params.get('status', ['aprovado']) or ['aprovado'])[0].strip().lower()
            de = (query_params.get('de', ['']) or [''])[0].strip()
            ate = (query_params.get('ate', ['']) or [''])[0].strip()

            # PAGCORP sempre: Fechamento não tem cartão nem saldo pra repor,
            # então nunca aparece nesta fila, em nenhum filtro.
            condicoes = ["PAGCORP IS NOT NULL", "LTRIM(RTRIM(PAGCORP)) <> ''"]
            parametros = []

            if status == 'aprovado':
                condicoes += ["APROVADO = 'APROVADO'", NAO_DEPOSITADO]
            elif status == 'pendente':
                condicoes.append("APROVADO = 'AGUARDANDO'")
            elif status == 'depositado':
                condicoes.append("LTRIM(RTRIM(DEPOSITADO)) IN ('DEPOSITADO', 'SIM')")
            # 'todos' não acrescenta nada além do PAGCORP

            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', de):
                condicoes.append("CAST(DATA_RETIRADA AS DATE) >= %s")
                parametros.append(de)
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', ate):
                condicoes.append("CAST(DATA_RETIRADA AS DATE) <= %s")
                parametros.append(ate)

            pedidos = executar_query(f"""
                SELECT TOP 500
                    ID, DATA_RETIRADA, PROJETO, LIDER, NOME_LIDER, PAGCORP,
                    RESPONSAVEL_PELO_CARTAO, TOTAL_PAGAR,
                    ISNULL(APROVADO, '') AS APROVADO,
                    ISNULL(DEPOSITADO, '') AS DEPOSITADO,
                    ISNULL(APROVADO_POR, '') AS APROVADO_POR
                FROM PEDIDOS
                WHERE {' AND '.join(condicoes)}
                ORDER BY DATA_RETIRADA DESC, ID DESC
            """, parametros) or []

            # Um depósito por (dia, equipe, cartão) — é assim que o pedido
            # de aprovação já agrupa lá na frente, então é a mesma unidade
            # que o financeiro reconhece como "um pedido só".
            grupos = {}
            for p in pedidos:
                data = p.get('DATA_RETIRADA')
                data_iso = data.strftime('%Y-%m-%d') if hasattr(data, 'strftime') else str(data)
                chave = (data_iso, p.get('LIDER') or '', p.get('PAGCORP') or '')

                if chave not in grupos:
                    depositado = (p.get('DEPOSITADO') or '').strip().upper() in ('DEPOSITADO', 'SIM')
                    grupos[chave] = {
                        'data': data_iso,
                        'data_br': data.strftime('%d/%m/%Y') if hasattr(data, 'strftime') else str(data),
                        'projeto': p.get('PROJETO') or '',
                        'equipe': p.get('LIDER') or '',
                        'nome_lider': p.get('NOME_LIDER') or '',
                        'pagcorp': p.get('PAGCORP') or '',
                        'responsavel_cartao': p.get('RESPONSAVEL_PELO_CARTAO') or '',
                        'aprovado_por': p.get('APROVADO_POR') or '',
                        'aprovado': (p.get('APROVADO') or '').strip().upper(),
                        'depositado': depositado,
                        'valor': 0.0,
                        'ids': [],
                    }

                g = grupos[chave]
                try:
                    g['valor'] += float(p.get('TOTAL_PAGAR') or 0)
                except (TypeError, ValueError):
                    pass
                g['ids'].append(int(p['ID']))

            lista = sorted(grupos.values(), key=lambda x: x['data'], reverse=True)

            response = {
                "error": False,
                "total": len(lista),
                "status": status,
                "total_valor": round(sum(g['valor'] for g in lista), 2),
                "pedidos": lista,
            }

        elif path == '/api/pagcorp-lista':
            # Cartões PAGCORP para o líder escolher outro que não o dele.
            # Busca por nome de quem é titular do cartão.
            busca = (query_params.get('busca', [''])[0] or '').strip()

            if busca:
                cartoes = executar_query(
                    "SELECT ID, CONTA, CC, LIDER FROM PAGCORP_CAD "
                    "WHERE LIDER LIKE %s ORDER BY LIDER",
                    ['%' + busca.upper() + '%'])
            else:
                cartoes = executar_query(
                    "SELECT ID, CONTA, CC, LIDER FROM PAGCORP_CAD ORDER BY LIDER", [])

            response = {
                "error": False,
                "total": len(cartoes or []),
                "cartoes": cartoes or []
            }

        elif path == '/api/colaboradores-busca':
            # Busca em TODA a empresa (não só na equipe): o líder às vezes
            # leva alguém de outra frente para a mesma refeição.
            termo = (query_params.get('q', [''])[0] or '').strip()

            if len(termo) < 2:
                response = {"error": False, "total": 0, "colaboradores": [],
                            "message": "Digite ao menos 2 letras"}
            else:
                achados = executar_query("""
                    SELECT TOP 40 ID, NOME, FUNCAO, EQUIPE, PROJETO, EMPRESA
                    FROM COLABORADORES
                    WHERE NOME LIKE %s AND SITUACAO = '1'
                    ORDER BY NOME""", ['%' + termo.upper() + '%'])

                response = {
                    "error": False,
                    "total": len(achados or []),
                    "colaboradores": achados or []
                }

        elif path == '/api/fornecedores-cidade':
            # Sugestão por proximidade: fornecedores cadastrados na MESMA
            # cidade em que a equipe está prestando serviço. O cadastro guarda
            # a cidade em LOCAL, com grafia irregular ("ANAPOLIS - GO "), então
            # a comparação é frouxa de propósito.
            cidade = (query_params.get('cidade', [''])[0] or '').strip()
            tipo = (query_params.get('tipo', [''])[0] or '').strip()

            equipe_ativa, erro_escopo = self._equipe_ativa(query_params)
            projeto = ''.join(c for c in (equipe_ativa or '') if c.isdigit())

            if not projeto and ve_tudo(self.usuario):
                projeto = query_params.get('projeto', [''])[0]

            if not projeto:
                response = {"error": True, "message": erro_escopo or "Projeto não identificado"}
            else:
                sql = ("SELECT ID, PROJETO, LOCAL, FORNECEDOR, TIPO_FORN, VALOR, "
                       "ISNULL(FECHAMENTO,'') AS FECHAMENTO "
                       "FROM tb_fornecedores WHERE PROJETO = %s AND STATUS = 'ATIVO'")
                par = [projeto]

                if tipo:
                    sql += " AND TIPO_FORN = %s"
                    par.append(tipo)

                sql += " ORDER BY FORNECEDOR"
                todos = executar_query(sql, par) or []

                # Normaliza para comparar cidade: sem acento, sem UF, sem espaço
                def chave_cidade(txt):
                    import unicodedata
                    t = unicodedata.normalize('NFKD', str(txt or ''))
                    t = ''.join(c for c in t if not unicodedata.combining(c))
                    t = t.upper().replace('-', ' ')
                    for uf in (' GO', ' MG', ' SP', ' PR', ' MS', ' MT', ' BA', ' TO'):
                        if t.strip().endswith(uf):
                            t = t.strip()[: -len(uf)]
                    return ' '.join(t.split())

                alvo = chave_cidade(cidade)
                perto, outros = [], []
                for f in todos:
                    (perto if alvo and chave_cidade(f.get('LOCAL')) == alvo else outros).append(f)

                response = {
                    "error": False,
                    "cidade": cidade,
                    "projeto": projeto,
                    "na_cidade": perto,
                    "outros": outros,
                    "total": len(todos)
                }

        elif path == '/api/pagcorp':
            lider = query_params.get('lider', [''])[0]
            # Buscar PAGCORP para líder
            
            try:
                # Buscar dados reais na tabela PAGCORP_CAD
                query = "SELECT ID, CONTA, CC, LIDER FROM PAGCORP_CAD WHERE LIDER = %s"
                resultado = executar_query(query, [lider])
                
                if resultado and len(resultado) > 0:
                    # PAGCORP encontrado
                    response = {
                        "error": False,
                        "lider": lider,
                        "total": len(resultado),
                        "pagcorp": resultado
                    }
                else:
                    # Nenhum PAGCORP encontrado
                    response = {
                        "error": False,
                        "lider": lider,
                        "total": 0,
                        "pagcorp": []
                    }
                    
            except Exception as e:
                print(f"❌ Erro ao buscar PAGCORP: {e}")
                response = {
                    "error": True,
                    "message": f"Erro ao buscar PAGCORP: {str(e)}",
                    "lider": lider,
                    "total": 0,
                    "pagcorp": []
                }
            
        elif path == '/api/pedidos-pendentes-temperatura':
            # Buscar pedidos reais de MARMITEX que precisam de aferição de temperatura
            # Buscar pedidos MARMITEX pendentes de temperatura
            # 🎯 EQUIPE VALIDADA CONTRA O ESCOPO DO TOKEN
            equipe_param, erro_escopo = self._equipe_ativa(query_params)
            if erro_escopo:
                print(f"⚠️ Escopo: {erro_escopo}")

            print(f"👥 Filtrando por equipe: {equipe_param}")

            # Usar LIDER como critério de filtro se fornecido (LIDER contém o nome da equipe)
            if equipe_param and equipe_param != 'SEM_EQUIPE':
                query = """
                SELECT ID, DATA_RETIRADA, NOME_LIDER, TIPO_REFEICAO, FORNECEDOR,
                       TOTAL_COLABORADORES, TOTAL_PAGAR, DATA_ENVIO1, LIDER,
                       TEMP_RETIRADA, TEMP_CONSUMO, AFERIU_TEMPERATURA
                FROM PEDIDOS
                WHERE (TIPO_REFEICAO LIKE '%%MARMITEX%%' OR TIPO_REFEICAO LIKE '%%MARMITA%%')
                  AND (AFERIU_TEMPERATURA IS NULL OR AFERIU_TEMPERATURA = '' OR AFERIU_TEMPERATURA = 'NAO')
                  AND LIDER = %s
                  AND DATA_RETIRADA >= DATEADD(day, -7, GETDATE())
                ORDER BY DATA_RETIRADA DESC
                """
                query_params_db = [equipe_param]
                # Query para equipe específica - últimos 7 dias apenas
            else:
                # Sem equipe válida, não retornar nada para evitar carregar dados de todas as equipes
                print("⚠️ Nenhuma equipe válida fornecida - retornando lista vazia")
                query = None
                query_params_db = []
            
            try:
                if query is None:
                    pedidos_pendentes = []
                else:
                    pedidos_pendentes = executar_query(query, query_params_db)
                print(f"📊 Query executada. Resultado: {type(pedidos_pendentes)}")
                
                if pedidos_pendentes is not None:
                    filtro_msg = f" para equipe '{equipe_param}'" if equipe_param and equipe_param != 'SEM_EQUIPE' else " (todas as equipes)"
                    print(f"✅ Encontrados {len(pedidos_pendentes)} pedidos MARMITEX pendentes{filtro_msg}")
                    
                    # Formatar dados para o frontend
                    pendencias_formatadas = []
                    for pedido in pedidos_pendentes:
                        equipe_pedido = pedido.get('LIDER', 'N/A')  # Usar LIDER que contém o nome da equipe
                        aferiu_status = pedido.get('AFERIU_TEMPERATURA', 'NULL')
                        print(f"   📋 Pedido ID {pedido['ID']}: {pedido['TIPO_REFEICAO']} - {pedido.get('DATA_RETIRADA', 'N/A')} - Equipe: {equipe_pedido} - Status: {aferiu_status}")
                        
                        # Converter DATA_RETIRADA para string se for datetime
                        data_retirada = pedido.get("DATA_RETIRADA")
                        if data_retirada:
                            data_retirada_str = data_retirada.strftime('%d/%m/%Y') if hasattr(data_retirada, 'strftime') else str(data_retirada)
                        else:
                            data_retirada_str = "N/A"
                        
                        # Tratar valores nulos/None com segurança
                        total_pagar = pedido.get("TOTAL_PAGAR")
                        if total_pagar is None or total_pagar == "":
                            total_pagar = 0.0
                        else:
                            try:
                                total_pagar = float(total_pagar)
                            except (ValueError, TypeError):
                                total_pagar = 0.0
                        
                        total_colab = pedido.get("TOTAL_COLABORADORES")
                        if total_colab is None or total_colab == "":
                            total_colab = 1
                        else:
                            try:
                                total_colab = int(total_colab)
                            except (ValueError, TypeError):
                                total_colab = 1

                        pendencia = {
                            "id": int(pedido["ID"]),  # ID real do banco como inteiro
                            "mealName": str(pedido.get("TIPO_REFEICAO", "N/A")),
                            "date": data_retirada_str,
                            "employees": f"{total_colab} pessoas",
                            "supplier": str(pedido.get("FORNECEDOR", "N/A")),
                            "city": "N/A",  # Campo não disponível na tabela atual
                            "requestor": str(pedido.get("NOME_LIDER", "N/A")),
                            "farm": "N/A",  # Campo não disponível na tabela atual
                            "phase": "Retirada",
                            "valor_total": total_pagar
                        }
                        pendencias_formatadas.append(pendencia)
                    
                    print(f"📤 Enviando {len(pendencias_formatadas)} pendências formatadas")
                    
                    filtro_msg_response = f" para equipe '{equipe_param}'" if equipe_param and equipe_param != 'SEM_EQUIPE' else ""
                    
                    response = {
                        "error": False,
                        "total": len(pendencias_formatadas),
                        "pendencias": pendencias_formatadas,
                        "message": f"Encontrados {len(pendencias_formatadas)} pedidos MARMITEX pendentes de aferição{filtro_msg_response}",
                        "equipe_filtro": equipe_param or "todas"
                    }
                else:
                    print("❌ Query retornou None - erro na conexão ou execução")
                    response = {
                        "error": True,
                        "message": "Erro ao executar query no banco de dados"
                    }
                    
            except Exception as e:
                print(f"❌ Erro ao buscar pedidos pendentes: {e}")
                response = {
                    "error": True,
                    "message": f"Erro ao buscar pedidos pendentes: {str(e)}"
                }
                
        elif path == '/api/ultimo-pedido':
            # Buscar último pedido da equipe para repetir
            try:
                equipe_param, erro_escopo = self._equipe_ativa(query_params)

                print(f"🔍 Buscando último pedido da equipe: {equipe_param}")

                if not equipe_param or equipe_param == 'SEM_EQUIPE':
                    response = {
                        "error": True,
                        "message": erro_escopo or "Equipe não informada"
                    }
                else:
                    # Query para buscar todos os pedidos de ONTEM da equipe (pode ser até 3)
                    # Usar DATA_RETIRADA como referência para "ontem"
                    brasilia_tz = pytz.timezone('America/Sao_Paulo')
                    hoje = datetime.now(brasilia_tz).date()
                    
                    from datetime import timedelta
                    ontem = hoje - timedelta(days=1)
                    
                    print(f"📅 Buscando pedidos de ONTEM: {ontem.strftime('%Y-%m-%d')}")
                    
                    query = """
                    SELECT 
                        ID, DATA_RETIRADA, DATA_ENVIO1, PROJETO, COORDENADOR, SUPERVISOR, 
                        LIDER, NOME_LIDER, FAZENDA, TIPO_REFEICAO, 
                        FORNECEDOR, VALOR_PAGO, 
                        TOTAL_COLABORADORES, A_CONTRATAR, 
                        PAGCORP, HOSPEDADO, VALOR_DIARIA, FECHAMENTO
                    FROM PEDIDOS 
                    WHERE LIDER = %s 
                      AND CAST(DATA_RETIRADA AS DATE) = %s
                    ORDER BY DATA_ENVIO1 DESC, ID DESC
                    """
                    
                    resultado = executar_query(query, [equipe_param, ontem])
                    
                    if resultado and len(resultado) > 0:
                        print(f"✅ Encontrados {len(resultado)} pedidos de ontem para {equipe_param}")
                        
                        # Criar lista com todos os pedidos
                        pedidos_lista = []
                        for pedido in resultado:
                            # Formatar data para exibição
                            data_original = pedido.get('DATA_RETIRADA')
                            if data_original:
                                data_original_str = data_original.strftime('%d/%m/%Y') if hasattr(data_original, 'strftime') else str(data_original)
                            else:
                                data_original_str = "N/A"
                            
                            pedido_formatado = {
                                "id": pedido['ID'],
                                "data_retirada_original": data_original_str,
                                "projeto": pedido.get('PROJETO', ''),
                                "coordenador": pedido.get('COORDENADOR', ''),
                                "supervisor": pedido.get('SUPERVISOR', ''),
                                "lider": pedido.get('LIDER', ''),
                                "nome_lider": pedido.get('NOME_LIDER', ''),
                                "fazenda": pedido.get('FAZENDA', ''),
                                "tipo_refeicao": pedido.get('TIPO_REFEICAO', ''),
                                "cidade": "",  # Campo não existe na tabela
                                "fornecedor": pedido.get('FORNECEDOR', ''),
                                "valor_pago": float(pedido.get('VALOR_PAGO') or 0),
                                "colaboradores_nomes": "",  # Campo não existe na tabela
                                "total_colaboradores": int(pedido.get('TOTAL_COLABORADORES') or 0),
                                "a_contratar": int(pedido.get('A_CONTRATAR') or 0),
                                "responsavel_cartao": "",  # Campo não existe na tabela
                                "pagcorp": pedido.get('PAGCORP', ''),
                                "hospedado": pedido.get('HOSPEDADO', ''),
                                "nome_hotel": "",  # Campo não existe na tabela
                                "valor_diaria": float(pedido.get('VALOR_DIARIA') or 0),
                                "fechamento": pedido.get('FECHAMENTO', '')
                            }
                            pedidos_lista.append(pedido_formatado)
                            
                            print(f"   📋 Pedido ID {pedido['ID']}: {pedido.get('TIPO_REFEICAO', 'N/A')} - {pedido.get('FORNECEDOR', 'N/A')}")
                        
                        response = {
                            "error": False,
                            "pedidos": pedidos_lista,  # Array com todos os pedidos
                            "total": len(pedidos_lista),
                            "data_original": ontem.strftime('%d/%m/%Y'),
                            "message": f"Encontrados {len(pedidos_lista)} pedidos de ontem ({ontem.strftime('%d/%m/%Y')}) para a equipe {equipe_param}"
                        }
                        
                    else:
                        response = {
                            "error": True,
                            "message": f"Nenhum pedido encontrado para a equipe {equipe_param}"
                        }
                        
            except Exception as e:
                print(f"❌ Erro ao buscar último pedido: {e}")
                response = {
                    "error": True,
                    "message": f"Erro ao buscar último pedido: {str(e)}"
                }
        else:
            response = {"error": True, "message": "Endpoint não encontrado"}
        
        self.wfile.write(json.dumps(response, ensure_ascii=False, default=decimal_default).encode('utf-8'))

    def _read_full_body(self):
        """Lê o corpo completo do POST de forma robusta (loop até receber tudo)"""
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            return b''

        body = b''
        remaining = content_length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            body += chunk
            remaining -= len(chunk)

        if len(body) != content_length:
            print(f"⚠️ Body incompleto: esperado {content_length} bytes, recebido {len(body)} bytes")

        return body

    def _send_json_headers(self):
        """Envia headers padrão para resposta JSON"""
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Equipe')
        self.end_headers()

    def do_POST(self):
        # Parse da URL
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path

        # Ler body ANTES de enviar response headers (evita truncamento)
        try:
            post_body = self._read_full_body()
        except Exception as e:
            print(f"❌ Erro ao ler body do POST {path}: {e}")
            self._enviar_json({"error": True, "message": f"Erro ao ler dados: {str(e)}"}, status=400)
            return

        # 🔒 PORTÃO: mesma regra do GET — só as rotas públicas passam sem token
        if not self._exigir_autenticacao(path):
            return

        # ==================================================================
        # AUTENTICAÇÃO — delegada à IAM Larsil
        # Este sistema não guarda senha nem tabela de usuário.
        # ==================================================================
        if path == '/api/auth/login':
            # Este endpoint responde com o STATUS HTTP correto, não com 200 +
            # {error:true}: a tela de login decide pelo response.ok, e um 200
            # em credencial errada deixaria a pessoa "entrar" sem token.
            try:
                dados_login = json.loads(post_body.decode('utf-8')) if post_body else {}
                login = str(dados_login.get('login') or dados_login.get('username') or '').strip()
                senha = dados_login.get('senha') or dados_login.get('password') or ''

                if not login or not senha:
                    self._enviar_json(
                        {"error": True, "message": "Usuário e senha são obrigatórios"},
                        status=400
                    )
                    return

                resposta_iam = requests.post(
                    f'{IAM_URL}/api/auth/login',
                    headers={'Content-Type': 'application/json'},
                    json={'login': login, 'senha': senha},
                    timeout=15
                )

                corpo = {}
                try:
                    corpo = resposta_iam.json()
                except Exception:
                    pass

                if not resposta_iam.ok:
                    # 403 + INATIVO: a conta existe, a TI desativou.
                    # A mensagem da IAM é para o usuário ler.
                    if resposta_iam.status_code == 403:
                        self._enviar_json({
                            "error": True,
                            "message": corpo.get('erro', 'Conta desativada.'),
                            "motivo": corpo.get('motivo')
                        }, status=403)
                    else:
                        self._enviar_json({
                            "error": True,
                            "message": corpo.get('erro', 'Usuário ou senha inválidos.')
                        }, status=401)
                    return

                usuario = corpo.get('usuario') or {}
                permissoes = usuario.get('permissoes') or []

                if IAM_EXIGIR_ACESSO and not usuario.get('admin') and not usuario.get('global') \
                        and PERMISSAO_ACESSO not in permissoes:
                    self._enviar_json({
                        "error": True,
                        "message": "Você não tem acesso ao sistema de refeições. Peça liberação à TI."
                    }, status=403)
                    return

                # Registra no perfil da pessoa que ela entrou NESTE sistema.
                # Fire-and-forget: se a IAM não responder, o login não pode falhar por isso.
                def _registrar_acesso(tok):
                    try:
                        requests.post(
                            f'{IAM_URL}/api/auth/acesso',
                            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {tok}'},
                            json={'sistema': IAM_SISTEMA},
                            timeout=8
                        )
                    except Exception:
                        pass

                threading.Thread(target=_registrar_acesso, args=(corpo.get('token'),), daemon=True).start()

                self._enviar_json({
                    "error": False,
                    "token": corpo.get('token'),
                    "senha_provisoria": bool(corpo.get('senha_provisoria')),
                    "user": {
                        "login": usuario.get('login'),
                        "nome": usuario.get('nome'),
                        "cpf": usuario.get('cpf'),
                        "email": usuario.get('email'),
                        "telefone": usuario.get('telefone'),
                        "admin": bool(usuario.get('admin')),
                        "papeis": usuario.get('papeis') or [],
                        "permissoes": permissoes,
                        "escopos": usuario.get('escopos') or [],
                        "global": bool(usuario.get('global'))
                    }
                })

            except requests.exceptions.RequestException as e:
                print(f"❌ IAM indisponível no login: {e}", flush=True)
                self._enviar_json({
                    "error": True,
                    "message": "Não foi possível falar com o servidor de identidade (IAM)."
                }, status=502)
            except Exception as e:
                import traceback
                print(f"❌ Erro no login: {type(e).__name__}: {e}", flush=True)
                print(traceback.format_exc(), flush=True)
                self._enviar_json({"error": True, "message": "Erro ao processar login."}, status=500)
            return

        elif path == '/api/auth/onboarding':
            # Primeiro acesso: troca a senha provisória (INTEGRACAO.md §4).
            # Status HTTP real, mesma razão do login.
            try:
                dados_ob = json.loads(post_body.decode('utf-8')) if post_body else {}
                nova_senha = dados_ob.get('novaSenha')

                if not nova_senha:
                    self._enviar_json({"error": True, "message": "Nova senha é obrigatória"}, status=400)
                    return
                else:
                    resposta_iam = requests.post(
                        f'{IAM_URL}/api/auth/onboarding',
                        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {self.token}'},
                        json={
                            'novaSenha': nova_senha,
                            'telefone': dados_ob.get('telefone'),
                            'email': dados_ob.get('email')
                        },
                        timeout=15
                    )

                    corpo = {}
                    try:
                        corpo = resposta_iam.json()
                    except Exception:
                        pass

                    if resposta_iam.ok:
                        # A senha mudou: o acesso em cache não vale mais
                        invalidar_cache_token(self.token)
                        self._enviar_json({"error": False, "ok": True})
                    else:
                        self._enviar_json(
                            {"error": True, "message": corpo.get('erro', 'Não foi possível concluir.')},
                            status=resposta_iam.status_code if resposta_iam.status_code >= 400 else 400
                        )

            except requests.exceptions.RequestException as e:
                print(f"❌ IAM indisponível no onboarding: {e}")
                self._enviar_json({"error": True, "message": "Falha ao falar com a IAM"}, status=502)
            except Exception as e:
                print(f"❌ Erro no onboarding: {e}")
                self._enviar_json({"error": True, "message": "Erro ao processar primeiro acesso."}, status=500)
            return

        elif path == '/api/pedido-aprovacao':
            # Dispara o pedido de aval no WhatsApp de quem controla o saldo.
            # Chamado logo depois de gravar os pedidos, com os IDs gerados.
            try:
                dados = json.loads(post_body.decode('utf-8')) if post_body else {}
                ids = dados.get('pedido_ids') or []
                resumo = dados.get('resumo') or {}

                # Cada forma de pagamento tem seu grupo no Telegram: PAGCORP
                # repoe saldo de cartao e vira deposito; Fechamento e pago no
                # fim do mes. Trocar o grupo e mandar dinheiro para o lugar
                # errado, entao o app diz explicitamente qual e.
                fluxo = (FLUXO_FECHAMENTO
                         if str(dados.get('tipo') or '').upper().startswith('FECH')
                         else FLUXO_PAGCORP)

                if not ids:
                    self._enviar_json({"error": True, "message": "Nenhum pedido informado"}, status=400)
                    return

                # 🔒 Os pedidos precisam ser da equipe de quem está pedindo.
                # Sem esta conferência, bastaria trocar os IDs no navegador
                # para colocar pedido de outra equipe na fila de aprovação.
                try:
                    ids = [int(i) for i in ids]
                except (TypeError, ValueError):
                    self._enviar_json({"error": True, "message": "IDs inválidos"}, status=400)
                    return

                marcadores = ', '.join(['%s'] * len(ids))
                donos = executar_query(
                    f"SELECT ID, LIDER FROM PEDIDOS WHERE ID IN ({marcadores})", ids) or []

                encontrados = {int(d['ID']): d.get('LIDER') for d in donos}
                fora = [i for i in ids
                        if i not in encontrados
                        or not equipe_permitida(self.usuario, encontrados[i])]

                if fora:
                    print(f'🚫 {self.usuario.get("login")} tentou aprovar pedidos fora do escopo: {fora}',
                          flush=True)
                    self._enviar_json({
                        "error": True,
                        "message": "Há pedidos fora do seu escopo de acesso"
                    }, status=403)
                    return

                if not telegram_configurado():
                    print('ℹ️ Telegram não configurado — pedido salvo sem aprovação automática', flush=True)
                    self._enviar_json({
                        "error": False,
                        "enviado": False,
                        "message": "Pedido salvo. Aprovação automática desligada."
                    })
                    return

                # Uma referência curta para casar a resposta do WhatsApp com
                # os pedidos. O menor ID basta: os demais vêm do mesmo envio.
                referencia = str(min(int(i) for i in ids))

                linhas = [
                    f"👤 *Solicitante:* {resumo.get('solicitante', '—')}",
                    f"🏢 *Projeto/Equipe:* {resumo.get('projeto', '—')} / {resumo.get('equipe', '—')}",
                    f"💳 *PAGCORP:* {resumo.get('pagcorp', '—')}",
                    f"📅 *Data:* {resumo.get('data', '—')}",
                    f"📍 *Cidade:* {resumo.get('cidade', '—')}",
                    '',
                    f"🍴 *Refeições:* {resumo.get('refeicoes', '—')}",
                    f"👥 *Pessoas:* {resumo.get('pessoas', '—')}",
                    f"💰 *Total:* R$ {resumo.get('total', '0,00')}",
                ]

                if resumo.get('motivo'):
                    linhas += ['', f"📝 *Motivo:* {resumo['motivo']}"]

                # A decisão vai pelo Telegram (botões + resposta sem webhook);
                # o aviso de status continua no WhatsApp, mais adiante.
                ok, detalhe = telegram_pedir_aprovacao(resumo, ids, fluxo)

                # Falha aqui e' silenciosa pro usuario (o app engole o erro),
                # entao ela precisa gritar no log — foi so' com log que deu
                # pra descobrir por que a Elaine nao recebia certos pedidos.
                if not ok:
                    print(f'🚨 APROVACAO NAO SAIU para {ids} '
                          f'(equipe {resumo.get("equipe")}, {resumo.get("solicitante")}): '
                          f'{detalhe} — o worker de resgate tenta de novo em ate 5 min',
                          flush=True)

                # Marca como pendente para o webhook saber o que atualizar
                if ok:
                    for pid in ids:
                        executar_query(
                            "UPDATE PEDIDOS SET APROVADO = %s, APROVADO_POR = %s WHERE ID = %s",
                            ['AGUARDANDO', NOME_APROVADOR, int(pid)])

                    # Telefone de quem pediu vem da IAM AGORA, com a pessoa
                    # autenticada — não do corpo do webhook lá na frente.
                    telefone = telefone_do_solicitante(self.usuario)
                    registrar_solicitante(referencia, {
                        'login': self.usuario.get('login'),
                        'nome': self.usuario.get('nome'),
                        'telefone': telefone,
                        'pedidos': ids,
                        'equipe': resumo.get('equipe', ''),
                    })

                    # Avisa quem pediu que o pedido está aguardando aprovação —
                    # o mesmo canal (WhatsApp) que depois traz o aprovado/reprovado.
                    #
                    # Em thread separada: isto é fora do caminho crítico (o
                    # pedido já está salvo e já foi pra aprovação), e a Z-API
                    # às vezes demora — foi essa espera, somada à do Telegram
                    # acima, que deixava "enviar pedido" parecendo travado por
                    # quase um minuto.
                    if telefone:
                        aviso_pendente = (
                            '⏳ *Pedido AGUARDANDO aprovação*\n\n'
                            f"📅 {resumo.get('data', '—')}\n"
                            f"🍴 {resumo.get('refeicoes', '—')}\n"
                            f"💰 R$ {resumo.get('total', '0,00')}\n\n"
                            f'_Enviado para {NOME_APROVADOR}_'
                        )

                        def _avisar_pendente(tel=telefone, msg=aviso_pendente):
                            okp, _ = zapi_enviar_texto(tel, msg)
                            print(f'📤 Aviso de pendente ao solicitante: {"enviado" if okp else "falhou"}', flush=True)

                        threading.Thread(target=_avisar_pendente, daemon=True).start()
                    else:
                        print(f'ℹ️ {self.usuario.get("login")} sem telefone na IAM — aviso de pendente não enviado', flush=True)

                self._enviar_json({
                    "error": False,
                    "enviado": bool(ok),
                    "referencia": referencia,
                    "aprovador": NOME_APROVADOR,
                    "message": ('Enviado para aprovação de ' + NOME_APROVADOR) if ok
                               else f'Não foi possível avisar o aprovador: {detalhe}'
                })

            except Exception as e:
                print(f'❌ Erro ao enviar aprovação: {e}', flush=True)
                self._enviar_json({"error": True, "message": str(e)}, status=500)
            return

        elif path == '/api/deposito-marcar':
            # Financeiro confirmando que o depósito saiu. Some da fila.
            if not tem_permissao(self.usuario, f'{IAM_NAMESPACE}.tela:/deposito'):
                self._enviar_json({"error": True, "message": "Sem permissão para o financeiro"}, status=403)
                return

            try:
                dados = json.loads(post_body.decode('utf-8')) if post_body else {}
                ids = dados.get('pedido_ids') or []
                try:
                    ids = [int(i) for i in ids]
                except (TypeError, ValueError):
                    self._enviar_json({"error": True, "message": "IDs inválidos"}, status=400)
                    return

                if not ids:
                    self._enviar_json({"error": True, "message": "Nenhum pedido informado"}, status=400)
                    return

                # 'DEPOSITADO', não 'SIM': é o que o sistema antigo grava nesta
                # coluna (milhares de linhas). Gravar outra palavra deixaria
                # estes pedidos invisíveis para os relatórios já existentes.
                marcadores = ', '.join(['%s'] * len(ids))
                afetados = executar_query(
                    f"UPDATE PEDIDOS SET DEPOSITADO = 'DEPOSITADO' "
                    f"WHERE ID IN ({marcadores}) AND APROVADO = 'APROVADO'",
                    ids)

                print(f'💰 {self.usuario.get("login")} marcou {afetados} pedido(s) como depositado: {ids}',
                      flush=True)
                self._enviar_json({"error": False, "afetados": afetados})

            except Exception as e:
                print(f'❌ Erro ao marcar depósito: {e}', flush=True)
                self._enviar_json({"error": True, "message": str(e)}, status=500)
            return

        elif path == '/api/webhook/zapi':
            # Resposta do aprovador. Aceita o clique no botão e também o texto
            # "APROVAR 123" — nem toda conexão do WhatsApp entrega botões.
            #
            # Esta rota é pública (a Z-API não tem token da IAM), então ela se
            # defende sozinha: segredo na URL + remetente conferido.
            try:
                # 🔒 1. Segredo compartilhado, comparado em tempo constante
                if not ZAPI_WEBHOOK_SEGREDO:
                    print('🚫 Webhook chamado mas ZAPI_WEBHOOK_SEGREDO não está definido', flush=True)
                    self._enviar_json({"error": True, "message": "Webhook desativado"}, status=503)
                    return

                enviado = urllib.parse.parse_qs(parsed_path.query).get('segredo', [''])[0]
                if not hmac.compare_digest(enviado, ZAPI_WEBHOOK_SEGREDO):
                    print('🚫 Webhook com segredo inválido — ignorado', flush=True)
                    self._enviar_json({"error": True, "message": "Não autorizado"}, status=401)
                    return

                evento = json.loads(post_body.decode('utf-8')) if post_body else {}

                # 🔒 2. A decisão só vale se veio do número do aprovador
                remetente = _so_digitos(evento.get('phone') or evento.get('participantPhone') or '')
                esperado = _so_digitos(WHATSAPP_APROVADOR)
                if not remetente or remetente[-11:] != esperado[-11:]:
                    print(f'🚫 Decisão de remetente não autorizado ({remetente[:6]}…) — ignorada', flush=True)
                    self._enviar_json({"error": False, "ignorado": True})
                    return
                print(f'📨 Webhook Z-API: {json.dumps(evento, ensure_ascii=False)[:400]}', flush=True)

                # O id do botão vem em formatos diferentes conforme o tipo
                # Cada formato de botão devolve o id em um lugar diferente;
                # varremos todos os conhecidos antes de cair no texto livre.
                resposta = ''
                for bloco_nome, chave in (
                    ('buttonsResponseMessage', 'buttonId'),
                    ('buttonsResponseMessage', 'message'),
                    ('listResponseMessage', 'selectedRowId'),
                    ('buttonReply', 'id'),
                    ('hydratedButton', 'id'),
                    ('interactiveResponseMessage', 'id'),
                ):
                    bloco = evento.get(bloco_nome) or {}
                    if isinstance(bloco, dict) and bloco.get(chave):
                        resposta = str(bloco[chave])
                        break

                # Texto livre, quando os botões não estão disponíveis
                if not resposta:
                    texto = ((evento.get('text') or {}).get('message')
                             or evento.get('message') or '')
                    resposta = str(texto).strip()

                bruto = resposta.upper().replace(':', ' ')
                decisao = ('APROVADO' if 'APROVAR' in bruto
                           else 'REPROVADO' if 'REPROVAR' in bruto else None)
                referencia = ''.join(c for c in bruto if c.isdigit())

                if not decisao or not referencia:
                    print(f'ℹ️ Webhook ignorado (sem decisão clara): {resposta[:80]}', flush=True)
                    self._enviar_json({"error": False, "ignorado": True})
                    return

                # A referência é o menor ID do envio; os irmãos são os pedidos
                # do mesmo solicitante, mesma data, criados na mesma leva.
                base = executar_query(
                    "SELECT LIDER, DATA_RETIRADA, NOME_LIDER FROM PEDIDOS WHERE ID = %s",
                    [int(referencia)])

                if not base:
                    print(f'⚠️ Pedido {referencia} não encontrado', flush=True)
                    self._enviar_json({"error": False, "ignorado": True})
                    return

                b = base[0]
                afetados = executar_query(
                    "UPDATE PEDIDOS SET APROVADO = %s WHERE LIDER = %s "
                    "AND CAST(DATA_RETIRADA AS DATE) = CAST(%s AS DATE) "
                    "AND APROVADO = 'AGUARDANDO'",
                    [decisao, b['LIDER'], b['DATA_RETIRADA']])

                print(f'✅ {decisao}: {afetados} pedido(s) da equipe {b["LIDER"]}', flush=True)

                # Devolutiva para quem pediu, no telefone que a IAM tem
                aviso = ('✅ *Pedido APROVADO*' if decisao == 'APROVADO'
                         else '❌ *Pedido REPROVADO*')
                aviso += (f"\n\n📅 {b['DATA_RETIRADA']}"
                          f"\n👥 Equipe {b['LIDER']}"
                          f"\n\n_Resposta de {NOME_APROVADOR}_")

                # 🔒 3. O destino sai do que registramos no envio, nunca do
                # corpo do webhook — senão qualquer payload viraria disparo de
                # WhatsApp para um número escolhido por quem chamou.
                solicitante = buscar_solicitante(referencia) or {}
                destino = _so_digitos(solicitante.get('telefone') or '')

                if destino:
                    zapi_enviar_texto(destino, aviso)
                    print(f'📤 Devolutiva enviada a {solicitante.get("login", "?")}', flush=True)
                else:
                    print(f'ℹ️ Sem telefone cadastrado para {solicitante.get("login", "solicitante")} '
                          f'— devolutiva não enviada', flush=True)

                self._enviar_json({"error": False, "decisao": decisao, "pedidos": afetados})

            except Exception as e:
                print(f'❌ Erro no webhook: {e}', flush=True)
                # 200 mesmo com erro: a Z-API reenvia em loop se receber 500
                self._enviar_json({"error": False, "tratado": False})
            return

        elif path == '/api/pagcorp-cadastrar':
            # Cadastra um cartão que ainda não está na base, para o líder não
            # digitar o número toda vez. Em PAGCORP_CAD: CONTA é o número,
            # LIDER o titular e CC o projeto/centro de custo.
            try:
                d = json.loads(post_body.decode('utf-8')) if post_body else {}

                conta = ''.join(c for c in str(d.get('conta') or '') if c.isdigit())
                titular = str(d.get('titular') or '').strip().upper()
                projeto = str(d.get('projeto') or '').strip().upper()

                if len(conta) < 5:
                    self._enviar_json({"error": True, "message": "Número do cartão inválido"}, status=400)
                    return
                if len(titular) < 5:
                    self._enviar_json({"error": True, "message": "Informe o nome completo do titular"}, status=400)
                    return
                if not projeto:
                    self._enviar_json({"error": True, "message": "Informe o projeto"}, status=400)
                    return

                # Já existe? Devolve o que está lá em vez de duplicar — dois
                # cartões com o mesmo número quebrariam a busca por titular.
                existe = executar_query(
                    "SELECT ID, CONTA, CC, LIDER FROM PAGCORP_CAD WHERE CONTA = %s", [conta])
                if existe:
                    self._enviar_json({
                        "error": False,
                        "novo": False,
                        "cartao": existe[0],
                        "message": f"Este cartão já está cadastrado para {existe[0]['LIDER']}."
                    })
                    return

                r = executar_query(
                    "INSERT INTO PAGCORP_CAD (CONTA, CC, LIDER) VALUES (%s, %s, %s)",
                    [conta, projeto, titular])

                if not r:
                    self._enviar_json({"error": True, "message": "Não foi possível salvar o cartão"}, status=500)
                    return

                print(f'💳 PAGCORP cadastrado por {self.usuario.get("login")}: '
                      f'{conta} / {titular} / {projeto}', flush=True)

                self._enviar_json({
                    "error": False,
                    "novo": True,
                    "cartao": {"CONTA": conta, "CC": projeto, "LIDER": titular},
                    "message": "Cartão cadastrado."
                })

            except Exception as e:
                print(f'❌ Erro ao cadastrar PAGCORP: {e}', flush=True)
                self._enviar_json({"error": True, "message": str(e)}, status=500)
            return

        elif path == '/api/auth/logout':
            # O token vive no navegador; aqui só derrubamos o cache do resolve
            token = self._token_do_header()
            if token:
                invalidar_cache_token(token)
            self._enviar_json({"error": False, "ok": True})
            return

        # Permissão de tela ANTES dos cabeçalhos: depois do 200 já enviado,
        # um 403 viraria uma segunda resposta dentro do corpo da primeira.
        if path == '/api/salvar-pedido' and not tela_permitida(self.usuario, '/pedido'):
            self._enviar_json(
                {"error": True, "message": "Você não tem acesso a lançar pedidos"}, status=403)
            return

        # Demais rotas: headers agora
        self._send_json_headers()

        if path == '/api/salvar-pedido':
            # Usar body já lido
            try:
                print(f"📏 Body recebido: {len(post_body)} bytes")

                if len(post_body) == 0:
                    print("❌ Erro: Body está vazio")
                    response = {"error": True, "message": "Dados vazios recebidos"}
                    self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
                    return

                print(f"📦 Dados brutos recebidos ({len(post_body)} bytes): {post_body[:200]}...")

                # Decodificar dados
                post_data_str = post_body.decode('utf-8')
                print(f"📝 String decodificada: {post_data_str[:200]}...")

                if not post_data_str.strip():
                    print("❌ Erro: String decodificada está vazia")
                    response = {"error": True, "message": "Dados decodificados estão vazios"}
                    self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
                    return

                # Parse JSON
                pedido_data = json.loads(post_data_str)
                print(f"✅ JSON parsed com sucesso")
                
            except json.JSONDecodeError as e:
                print(f"❌ Erro ao fazer parse do JSON: {e}")
                print(f"❌ Dados problemáticos: {post_body[:500]}")
                response = {"error": True, "message": f"Erro no formato JSON: {str(e)}"}
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
                return
            except Exception as e:
                print(f"❌ Erro geral no processamento: {e}")
                response = {"error": True, "message": f"Erro no servidor: {str(e)}"}
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
                return
            
            try:
                print(f"📋 Dados do pedido: {pedido_data}")

                # Antes, TODO salvamento checava a estrutura inteira da tabela
                # (INFORMATION_SCHEMA + tamanho de coluna) antes de inserir —
                # duas consultas e dezenas de linhas de log a mais em cada
                # pedido, pra confirmar uma coisa que a coluna AFERIU_TEMPERATURA
                # já tem há muito tempo (NVARCHAR(50), criada e do tamanho certo).
                # Migração de schema é coisa de rodar uma vez, não a cada POST.

                # Query COMPLETA com todos os campos disponíveis + APROVADO_POR + AFERIU_TEMPERATURA
                # ✅ FECHAMENTO removido - será preenchido pela TRIGGER do SQL
                query = """
                INSERT INTO PEDIDOS (
                    DATA_RETIRADA, DATA_ENVIO1, PROJETO, COORDENADOR, SUPERVISOR, 
                    LIDER, NOME_LIDER, FAZENDA, TIPO_REFEICAO, CIDADE_PRESTACAO_DO_SERVICO,
                    FORNECEDOR, VALOR_PAGO, COLABORADORES, TOTAL_COLABORADORES, A_CONTRATAR,
                    RESPONSAVEL_PELO_CARTAO, PAGCORP, HOSPEDADO, NOME_DO_HOTEL, VALOR_DIARIA,
                    TOTAL_PAGAR, APROVADO_POR, OBSERVACOES, AFERIU_TEMPERATURA
                ) VALUES (%s, DATEADD(hour, -6, GETUTCDATE()), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                
                # Extrair TODOS os dados do pedido com MAPEAMENTO CORRETO
                data_retirada = pedido_data.get('data_retirada')
                projeto = pedido_data.get('projeto', '')
                coordenador = pedido_data.get('coordenador', '')
                supervisor = pedido_data.get('supervisor', '')
                
                # LIDER = EQUIPE ATIVA (ex: 700AA).
                # Vem do escopo do token, não do corpo do POST: senão bastaria
                # editar o JSON no navegador para lançar pedido em outra equipe.
                equipe_validada, erro_escopo = self._equipe_ativa()
                if not equipe_validada:
                    pedida = str(pedido_data.get('equipe') or '').strip().upper()
                    if pedida and equipe_permitida(self.usuario, pedida):
                        equipe_validada = pedida

                if not equipe_validada:
                    response = {
                        "error": True,
                        "message": erro_escopo or "Equipe não informada ou fora do seu escopo"
                    }
                    self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
                    return

                lider = equipe_validada

                # NOME_LIDER = Nome do líder da equipe (do organograma).
                # Sem isso, cai para o nome de quem está logado na IAM.
                nome_lider = (pedido_data.get('nome_lider_organograma')
                              or pedido_data.get('solicitante')
                              or self.usuario.get('nome')
                              or 'N/A')
                
                # FAZENDA = APENAS o que o usuário digitou no campo
                fazenda = pedido_data.get('fazenda_digitada', '').strip()
                
                tipo_refeicao = pedido_data.get('tipo_refeicao', 'N/A')
                cidade = pedido_data.get('cidade_prestacao_servico', '')
                fornecedor = pedido_data.get('fornecedor', 'N/A')
                valor_pago = float(pedido_data.get('valor_pago', 0))
                
                # COLABORADORES = Limpar ícones, manter só texto
                colaboradores_nomes = pedido_data.get('colaboradores_nomes_limpos', '')
                
                # Limpeza adicional de caracteres Unicode problemáticos
                # (re, base64 e threading vêm do topo do módulo — reimportar
                #  aqui tornaria o nome local para o do_POST inteiro)
                if colaboradores_nomes:
                    # Remover surrogates e caracteres problemáticos
                    colaboradores_nomes = re.sub(r'[\uD800-\uDFFF]', '', colaboradores_nomes)
                    # Remover emojis e símbolos
                    colaboradores_nomes = re.sub(r'[^\x00-\x7F\u00C0-\u017F\u0020-\u007E]', '', colaboradores_nomes)
                    # Limpar espaços extras
                    colaboradores_nomes = re.sub(r'\s+', ' ', colaboradores_nomes).strip()
                
                total_colaboradores = int(pedido_data.get('total_colaboradores', 1))
                a_contratar = int(pedido_data.get('a_contratar', 0))
                responsavel_cartao = pedido_data.get('responsavel_cartao', '')
                
                # PAGCORP = Número digitado pelo usuário
                pagcorp = pedido_data.get('pagcorp_numero', '')
                
                # HOSPEDAGEM = Dados corretos do formulário
                hospedado = pedido_data.get('hospedado_real', 'NÃO')
                nome_hotel = pedido_data.get('nome_hotel_real', '')
                valor_diaria = float(pedido_data.get('valor_diaria_real', 0))
                
                # APROVADO_POR = Texto fixo
                aprovado_por = 'ELAINE KLUG'
                
                # ✅ FECHAMENTO removido - será preenchido pela TRIGGER do SQL
                
                # 🎯 CAPTURAR AFERIU_TEMPERATURA DO FRONTEND
                aferiu_temperatura_frontend = pedido_data.get('aferiu_temperatura', '')
                
                observacoes = pedido_data.get('observacoes', '')
                
                # Calcular total a pagar: APENAS VALOR_PAGO × TOTAL_COLABORADORES (SEM DIÁRIA)
                total_pessoas = total_colaboradores  # Já inclui selecionados + a_contratar + outros
                total_refeicao = valor_pago * total_pessoas
                # NÃO INCLUIR valor da diária no total_pagar
                total_pagar = total_refeicao
                
                print(f"💰 Cálculo CORRIGIDO:")
                print(f"   Total colaboradores (já incluindo tudo): {total_colaboradores}")
                print(f"   A contratar (não soma mais): {a_contratar}")
                print(f"   Total pessoas: {total_pessoas}")
                print(f"   Refeição: R$ {valor_pago} x {total_pessoas} pessoas = R$ {total_refeicao}")
                print(f"   HOSPEDADO: {hospedado}")
                print(f"   Hotel: R$ {valor_diaria} (NÃO incluído no total)")
                print(f"   TOTAL FINAL: R$ {total_pagar} (apenas refeições)")
                print(f"🔧 DADOS CORRIGIDOS:")
                print(f"   LIDER (equipe): {lider}")
                print(f"   NOME_LIDER (do organograma): {nome_lider}")
                print(f"   FAZENDA: {fazenda}")
                print(f"   PAGCORP: {pagcorp}")
                print(f"   RESPONSÁVEL CARTÃO: {responsavel_cartao}")
                print(f"   HOSPEDADO: {hospedado}")
                print(f"   NOME HOTEL: {nome_hotel}")
                print(f"   VALOR DIÁRIA: R$ {valor_diaria}")
                # ✅ FECHAMENTO removido - será preenchido pela TRIGGER do SQL
                
                resultado = executar_query(query, [
                    data_retirada, projeto, coordenador, supervisor, lider, nome_lider,
                    fazenda, tipo_refeicao, cidade, fornecedor, valor_pago, 
                    colaboradores_nomes, total_colaboradores, a_contratar,
                    responsavel_cartao, pagcorp, hospedado, nome_hotel, valor_diaria,
                    total_pagar, aprovado_por, observacoes, aferiu_temperatura_frontend
                    # ✅ fechamento removido - será preenchido pela TRIGGER
                ])
                
                if resultado is not None and isinstance(resultado, dict) and 'inserted_id' in resultado:
                    # Sucesso - retornar o ID real do banco
                    pedido_id_real = resultado['inserted_id']
                    print(f"✅ Pedido salvo com ID real: {pedido_id_real}")
                    
                    # ✅ AFERIU_TEMPERATURA JÁ FOI INSERIDO DIRETAMENTE NA QUERY PRINCIPAL
                    # ✅ AFERIU_TEMPERATURA JÁ FOI INSERIDO DIRETAMENTE NA QUERY PRINCIPAL
                    
                    response = {
                        "error": False,
                        "message": "Pedido salvo com sucesso!",
                        "pedido_id": pedido_id_real,
                        "tipo_refeicao": tipo_refeicao,
                        "total_pagar": total_pagar,
                        "aferiu_temperatura": aferiu_temperatura_frontend
                    }
                else:
                    print(f"❌ Falha ao inserir - resultado: {resultado}")
                    response = {
                        "error": True,
                        "message": "Erro ao salvar pedido no banco de dados",
                        "debug": str(resultado)
                    }
                    
            except Exception as e:
                print(f"❌ Erro detalhado: {e}")
                response = {
                    "error": True,
                    "message": f"Erro ao processar pedido: {str(e)}"
                }
                
        elif path == '/upload-blob':
            # Endpoint para upload de imagens do problema para Azure Blob
            print("📸 Recebendo upload de imagem para blob...")
            try:
                # Ler o conteúdo como multipart/form-data manualmente
                import tempfile
                
                # Usar body já lido
                raw_data = post_body
                
                # Procurar pelo boundary no Content-Type
                content_type = self.headers.get('Content-Type', '')
                if 'boundary=' not in content_type:
                    response = {
                        "error": True,
                        "message": "Content-Type boundary não encontrado"
                    }
                else:
                    boundary = content_type.split('boundary=')[1].strip()
                    boundary_bytes = ('--' + boundary).encode()
                    
                    # Dividir os dados pelo boundary
                    parts = raw_data.split(boundary_bytes)
                    
                    file_data = None
                    filename = None
                    
                    for part in parts:
                        if b'Content-Disposition' in part and b'filename=' in part:
                            # Extrair o nome do arquivo
                            lines = part.split(b'\r\n')
                            for line in lines:
                                if b'filename=' in line:
                                    # Extrair filename
                                    filename_part = line.decode().split('filename=')[1]
                                    filename = filename_part.strip('"').strip()
                                    break
                            
                            # Encontrar onde começam os dados do arquivo (após \r\n\r\n)
                            data_start = part.find(b'\r\n\r\n')
                            if data_start != -1:
                                file_data = part[data_start + 4:]  # +4 para pular \r\n\r\n
                                # Remover possível trailing boundary
                                if file_data.endswith(b'\r\n'):
                                    file_data = file_data[:-2]
                                break
                    
                    if file_data and filename:
                        print(f"📤 Upload recebido: {filename} ({len(file_data)} bytes)")
                        
                        # Converter para base64 para usar a função existente
                        file_base64 = base64.b64encode(file_data).decode('utf-8')
                        
                        # Fazer upload para blob usando função existente
                        blob_url, erro_blob = upload_imagem_blob(file_base64, filename)

                        if blob_url:
                            response = {
                                "error": False,
                                "message": "Upload realizado com sucesso",
                                "url": blob_url,
                                "filename": filename
                            }
                            print(f"✅ Upload concluído: {blob_url}", flush=True)
                        else:
                            response = {
                                "error": True,
                                "message": f"Não foi possível enviar a imagem: {erro_blob}"
                            }
                            print(f"❌ Upload recusado: {erro_blob}", flush=True)
                    else:
                        response = {
                            "error": True,
                            "message": "Arquivo ou nome não encontrado nos dados"
                        }
                            
            except Exception as e:
                print(f"❌ Erro no endpoint de upload: {e}")
                response = {
                    "error": True,
                    "message": f"Erro no upload: {str(e)}"
                }
                
        elif path == '/api/aferição-temperatura' or path == '/api/afericao-temperatura':
            # Endpoint para aferição de temperatura com imagens (suporte a URLs com e sem acentos)
            try:
                print(f"📏 Afericao body: {len(post_body)} bytes")
                aferição_data = json.loads(post_body.decode('utf-8'))
                
                pedido_id = aferição_data['pedido_id']
                temperatura_retirada = aferição_data['temperatura_retirada']
                temperatura_consumo = aferição_data['temperatura_consumo']
                hora_retirada = aferição_data.get('hora_retirada')
                hora_consumo = aferição_data.get('hora_consumo')
                img_retirada_base64 = aferição_data.get('img_retirada')
                img_consumo_base64 = aferição_data.get('img_consumo')
                observacoes = aferição_data.get('observacoes', '')
                
                print(f"️ Salvando temperaturas - Pedido: {pedido_id}, Retirada: {temperatura_retirada}°C, Consumo: {temperatura_consumo}°C")

                # 🔒 O pedido precisa ser de uma equipe dentro do escopo de quem
                # está aferindo. Sem isso, um ID chutado alteraria pedido alheio.
                dono = executar_query("SELECT LIDER FROM PEDIDOS WHERE ID = %s", [pedido_id])
                if not dono:
                    response = {"error": True, "message": f"Pedido {pedido_id} não encontrado"}
                    self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
                    return

                if not equipe_permitida(self.usuario, dono[0].get('LIDER')):
                    print(f"🚫 Pedido {pedido_id} (equipe {dono[0].get('LIDER')}) fora do escopo de {self.usuario.get('login')}")
                    response = {"error": True, "message": "Este pedido não é de uma equipe do seu escopo"}
                    self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
                    return


                # As colunas de temperatura já existem há muito tempo — checar
                # isso a cada aferição custava mais uma consulta por nada.

                # Atualizar temperaturas no banco
                query_temp = """
                UPDATE PEDIDOS 
                SET TEMPERATURA_RETIRADA = %s, 
                    TEMPERATURA_CONSUMO = %s,
                    HORA_RETIRADA = %s,
                    HORA_CONSUMO = %s,
                    OBSERVACOES_TEMP = %s
                WHERE ID = %s
                """
                
                # Processar horas
                from datetime import datetime, date
                hora_retirada_dt = None
                hora_consumo_dt = None
                
                # Buscar data do pedido
                query_data = "SELECT DATA_RETIRADA FROM PEDIDOS WHERE ID = %s"
                resultado_data = executar_query(query_data, [pedido_id])
                
                if resultado_data and len(resultado_data) > 0:
                    data_retirada_pedido = resultado_data[0]['DATA_RETIRADA']
                    if hasattr(data_retirada_pedido, 'date'):
                        data_retirada_pedido = data_retirada_pedido.date()
                else:
                    data_retirada_pedido = date.today()
                
                # Converter horas para datetime
                if hora_retirada:
                    try:
                        hora_obj = datetime.strptime(hora_retirada, '%H:%M').time()
                        hora_retirada_dt = datetime.combine(data_retirada_pedido, hora_obj)
                    except:
                        pass
                
                if hora_consumo:
                    try:
                        hora_obj = datetime.strptime(hora_consumo, '%H:%M').time()
                        hora_consumo_dt = datetime.combine(data_retirada_pedido, hora_obj)
                    except:
                        pass
                
                resultado_temp = executar_query(query_temp, [
                    temperatura_retirada,
                    temperatura_consumo,
                    hora_retirada_dt,
                    hora_consumo_dt,
                    observacoes,
                    pedido_id
                ])
                
                print(f"✅ Temperaturas salvas: {resultado_temp} linhas afetadas")
                
                # 🔥 ATUALIZAR CAMPO AFERIU_TEMPERATURA PARA "SIM"
                query_status = "UPDATE PEDIDOS SET AFERIU_TEMPERATURA = 'SIM' WHERE ID = %s"
                resultado_status = executar_query(query_status, [pedido_id])
                print(f"✅ AFERIU_TEMPERATURA atualizado para 'SIM': {resultado_status} linhas afetadas")
                
                # Upload das imagens em background.
                #
                # NÃO reimportar threading aqui: um `import` dentro da função
                # torna o nome local para o do_POST INTEIRO, e a thread do
                # registro de acesso no login (mais acima) quebrava com
                # UnboundLocalError. O módulo já vem importado no topo.
                def upload_async(pid, img_ret, img_con):
                    """
                    Sobe as duas fotos e grava as URLs. O que falhar vai para a
                    fila de reenvio — nada de foto sumir em silêncio.
                    """
                    try:
                        updates, params = [], []

                        for campo, imagem, rotulo in (
                            ('IMG_RETIRADA', img_ret, 'retirada'),
                            ('IMG_CONSUMO', img_con, 'consumo'),
                        ):
                            if not imagem:
                                continue

                            url, erro = upload_imagem_blob(imagem, f'{rotulo}_pedido_{pid}.jpg')
                            if url:
                                updates.append(f'{campo} = %s')
                                params.append(url)
                            else:
                                print(f'⚠️ {rotulo} do pedido {pid} não subiu ({erro})', flush=True)
                                enfileirar_blob(pid, campo, imagem)

                        if updates:
                            params.append(pid)
                            resultado_img = executar_query(
                                f"UPDATE PEDIDOS SET {', '.join(updates)} WHERE ID = %s", params)
                            print(f'✅ {len(updates)} URL(s) gravada(s) no pedido {pid}: {resultado_img}', flush=True)

                    except Exception as e:
                        print(f'❌ Erro no upload assíncrono (pedido {pid}): {e}', flush=True)

                # Iniciar upload em thread separada - passar dados como argumentos
                if img_retirada_base64 or img_consumo_base64:
                    upload_thread = threading.Thread(
                        target=upload_async,
                        args=(pedido_id, img_retirada_base64, img_consumo_base64)
                    )
                    upload_thread.daemon = True
                    upload_thread.start()
                
                # Resposta imediata
                if resultado_temp is not None and resultado_temp > 0:
                    response = {
                        "error": False,
                        "message": f"✅ Temperaturas salvas instantaneamente! Upload das imagens em andamento...",
                        "pedido_id": pedido_id,
                        "temperaturas": {
                            "retirada": temperatura_retirada,
                            "consumo": temperatura_consumo
                        },
                        "status_upload": "em_andamento",
                        "urls_imagens": {
                            "retirada": "upload_iniciado",
                            "consumo": "upload_iniciado"
                        }
                    }
                else:
                    response = {
                        "error": True,
                        "message": f"❌ Erro ao salvar temperaturas no banco (ID {pedido_id})"
                    }
                    
            except json.JSONDecodeError as e:
                print(f"❌ Erro JSON na aferição: {e}")
                print(f"❌ Body recebido: {len(post_body)} bytes, primeiros 300: {post_body[:300]}")
                response = {
                    "error": True,
                    "message": f"Erro no formato JSON da aferição: {str(e)}"
                }
            except Exception as e:
                print(f"❌ Erro ao processar aferição: {e}")
                response = {
                    "error": True,
                    "message": f"Erro ao processar aferição: {str(e)}"
                }
        else:
            response = {"error": True, "message": "Endpoint POST não encontrado"}
        
        self.wfile.write(json.dumps(response, ensure_ascii=False, default=decimal_default).encode('utf-8'))

    def do_OPTIONS(self):
        # Responder ao preflight CORS
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

def main():
    import os
    import sys

    # O console do Windows abre em cp1252 e estoura em qualquer emoji dos logs
    # (no Railway, que é Linux/UTF-8, isso nunca apareceu). Sem esta linha, o
    # servidor morre no primeiro print ao rodar localmente.
    # line_buffering: sem isso, com a saída redirecionada para arquivo, os
    # print() ficam no buffer e o log do servidor não mostra nada do que
    # aconteceu — foi assim que a falha de upload passou despercebida.
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        except Exception:
            pass

    # Railway fornece a porta via variável de ambiente PORT
    port = int(os.environ.get('PORT', 8082))
    
    print(f"🐍 Servidor Python iniciado em: http://localhost:{port}")
    print(f"🔐 Identidade: IAM Larsil ({IAM_URL}) — sistema '{IAM_SISTEMA}'")
    print(f"   Exigir '{PERMISSAO_ACESSO}': {'SIM' if IAM_EXIGIR_ACESSO else 'não (transição)'}")
    print(f"📷 Fotos de perfil: FOTO_PERFIL / SUPERVISOR_FOTOS → fallback {PCP_URL}/api/foto")
    print(f"🔧 Entradas:")
    print(f"   - http://localhost:{port}/login.html")
    print(f"   - http://localhost:{port}/sistema-pedidos.html")
    print(f"   - http://localhost:{port}/health (health check)")
    print(f"🌐 CONECTANDO NO AZURE SQL REAL!")
    print(f"❌ Para parar: Ctrl+C")
    print("=" * 60)
    sys.stdout.flush()  # Forçar output imediato para logs do Railway

    # IPv6 que resolve mas não conecta faz cada chamada externa esperar o
    # timeout antes de tentar IPv4. Conferir uma vez aqui evita pagar isso
    # em toda mensagem do Telegram e da Z-API.
    if _ipv6_utilizavel():
        print("🌍 IPv6 disponível", flush=True)
    else:
        _preferir_ipv4()
        print("🌍 IPv6 sem resposta — usando IPv4 nas chamadas externas", flush=True)

    # Credencial do blob: conferir no boot é o que evita descobrir um SAS
    # vencido semanas depois, com as fotos das aferições perdidas no caminho.
    blob_ok, blob_msg = diagnosticar_sas()
    print(f"{'✅' if blob_ok else '🚨'} Azure Blob: {blob_msg}", flush=True)
    if not blob_ok:
        print('   As fotos vão para a fila de reenvio até a credencial ser corrigida.', flush=True)

    # Escuta as decisões de aprovação (long-polling, sem webhook)
    threading.Thread(target=_worker_telegram, daemon=True).start()

    # Pesca as aprovações que não saíram no momento do pedido
    threading.Thread(target=_worker_resgate_aprovacoes, daemon=True).start()

    # Reprocessa o que ficou na fila de envios anteriores
    threading.Thread(target=processar_fila_blob, daemon=True).start()
    threading.Thread(target=_worker_fila_blob, daemon=True).start()

    # Auto-registro do sistema + telas na IAM, em segundo plano: se a IAM
    # estiver fora do ar, o servidor sobe do mesmo jeito.
    threading.Thread(target=registrar_sistema_na_iam, daemon=True).start()

    try:
        # Permitir reuso do endereço para evitar "Address already in use"
        socketserver.ThreadingTCPServer.allow_reuse_address = True

        # Escutar em IPv6 E IPv4 (dual-stack).
        #
        # Só IPv4 era a causa de CADA requisição do app demorar ~2 segundos:
        # "localhost" resolve para ::1 (IPv6) ANTES de 127.0.0.1, então o
        # navegador tentava IPv6, esperava o timeout, e só então caía no
        # IPv4. Medido: 2,05s por localhost contra 0,005s por 127.0.0.1 —
        # 400x. Com dezenas de requisições por tela, era isso que fazia
        # "enviar pedido" levar meio minuto.
        class ServidorDualStack(socketserver.ThreadingTCPServer):
            address_family = socket.AF_INET6
            daemon_threads = True          # não segura o desligamento
            allow_reuse_address = True

        try:
            # bind_and_activate=False para desligar o V6ONLY ANTES do bind:
            # no Windows, mudar isso com o socket já ligado dá WinError 10022.
            servidor = ServidorDualStack(("::", port), RefeicaoHandler,
                                         bind_and_activate=False)
            # V6ONLY desligado = o mesmo socket atende IPv4 também
            servidor.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            servidor.server_bind()
            servidor.server_activate()
            familia = "IPv6+IPv4"
        except OSError:
            # Ambiente sem IPv6: segue só com IPv4, como antes
            servidor = socketserver.ThreadingTCPServer(("", port), RefeicaoHandler)
            familia = "IPv4"

        with servidor as httpd:
            print(f"✅ Servidor escutando na porta {port} ({familia})")
            sys.stdout.flush()
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n🛑 Servidor parado.")
    except Exception as e:
        print(f"❌ ERRO FATAL ao iniciar servidor: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()