/* ==========================================================================
   session.js — Sessão compartilhada entre as páginas do Sistema de Refeições.

   A identidade vem da IAM Larsil (INTEGRACAO.md do projeto iam_larsil): o
   login acontece uma vez em login.html, que fala com o nosso backend, que por
   sua vez delega à IAM. O token da IAM acompanha toda chamada /api.

   Nenhuma página guarda credencial. Nenhuma tela decide sozinha o que a
   pessoa pode ver: quem manda são as permissões e os ESCOPOS do token.

   Duas particularidades deste sistema, que não existem no Justificativas:

   1. EQUIPE ATIVA — o app inteiro (colaboradores, fornecedores, pendências de
      temperatura) é filtrado por uma equipe. Ela sai dos escopos do token, não
      de um campo digitado. Quem tem uma equipe só entra direto; quem tem mais
      escolhe na tela de equipes e pode trocar depois.

   2. MODO CAMPO — é um PWA usado em fazenda, sem sinal. Se o token vencer
      offline, a sessão NÃO é descartada: o app continua utilizável e os
      pedidos vão para a fila local. Ao voltar a internet, pedimos a senha
      antes de sincronizar. Perder um lançamento no mato é pior que manter uma
      sessão vencida num aparelho que já está na mão da pessoa.

   A foto de perfil NÃO vem da IAM — quem resolve é o Painel PCP, por nome.
   ========================================================================== */
(function (global) {
    'use strict';

    var API_BASE_URL = (function () {
        var host = global.location.hostname;
        if (!host || host === 'localhost' || host === '127.0.0.1') {
            // Servidor Python local do projeto
            return global.location.port ? global.location.origin : 'http://localhost:8082';
        }
        return global.location.origin;
    })();

    var TOKEN_KEY = 'token';              // JWT da IAM
    var USER_KEY = 'larsil_user';         // identidade + permissões (cache local)
    var EQUIPE_KEY = 'equipe_logada';     // equipe ativa (mantém o nome antigo: o app todo já lê essa chave)
    var FOTO_KEY = 'larsil_foto_base';    // URL do resolvedor de fotos (PCP)
    var COOKIE_DIAS = 30;

    var redirecionando = false;

    // ======================================================================
    // PERSISTÊNCIA EM CAMADAS
    //
    // O iOS limpa o localStorage de PWA com alguma agressividade quando o
    // aparelho fica sem espaço ou o app passa muito tempo fechado. Gravar nas
    // três camadas custa quase nada e evita o líder ter que logar de novo no
    // meio do talhão.
    // ======================================================================

    function gravar(chave, valor) {
        try { localStorage.setItem(chave, valor); } catch (e) { /* noop */ }
        try { sessionStorage.setItem(chave + '_backup', valor); } catch (e) { /* noop */ }
        try {
            var exp = new Date(Date.now() + COOKIE_DIAS * 864e5).toUTCString();
            document.cookie = chave + '=' + encodeURIComponent(valor) +
                '; expires=' + exp + '; path=/; SameSite=Lax';
        } catch (e) { /* noop */ }
    }

    function lerCookie(chave) {
        try {
            var alvo = chave + '=';
            var partes = document.cookie.split(';');
            for (var i = 0; i < partes.length; i++) {
                var p = partes[i].trim();
                if (p.indexOf(alvo) === 0) return decodeURIComponent(p.substring(alvo.length));
            }
        } catch (e) { /* noop */ }
        return null;
    }

    function ler(chave) {
        var v = null;
        try { v = localStorage.getItem(chave); } catch (e) { /* noop */ }
        if (v) return v;
        try { v = sessionStorage.getItem(chave + '_backup'); } catch (e) { /* noop */ }
        if (v) { gravar(chave, v); return v; }   // reidrata as outras camadas
        v = lerCookie(chave);
        if (v) { gravar(chave, v); return v; }
        return null;
    }

    function apagar(chave) {
        try { localStorage.removeItem(chave); } catch (e) { /* noop */ }
        try { sessionStorage.removeItem(chave + '_backup'); } catch (e) { /* noop */ }
        try { document.cookie = chave + '=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;'; } catch (e) { /* noop */ }
    }

    function limpar() {
        apagar(TOKEN_KEY);
        apagar(USER_KEY);
        apagar(EQUIPE_KEY);
    }

    function irParaLogin(motivo) {
        if (redirecionando) return;
        redirecionando = true;
        if (motivo) console.warn('🔒 ' + motivo);
        limpar();
        global.location.href = '/login.html';
    }

    // ======================================================================
    // TOKEN E IDENTIDADE
    // ======================================================================

    function getJwt() { return ler(TOKEN_KEY); }

    function getUsuario() {
        try { return JSON.parse(ler(USER_KEY) || 'null'); }
        catch (e) { return null; }
    }

    /** Lê o `exp` do JWT sem validar assinatura — só para não usar token vencido. */
    function expiraEm() {
        var t = getJwt();
        if (!t) return 0;
        try {
            var payload = JSON.parse(atob(t.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
            return (payload.exp || 0) * 1000;
        } catch (e) { return 0; }
    }

    /** Token presente e dentro da validade. */
    function sessaoValida() {
        var exp = expiraEm();
        return !!getJwt() && (exp === 0 || Date.now() < exp);
    }

    function estaOnline() { return global.navigator.onLine !== false; }

    /**
     * MODO CAMPO: a sessão serve para abrir o app?
     * Offline, um token vencido ainda serve — o que ele não faz é chamar a API.
     */
    function sessaoUsavel() {
        if (sessaoValida()) return true;
        return !!getJwt() && !estaOnline();
    }

    /** Está online com token vencido: precisa da senha antes de sincronizar. */
    function precisaReautenticar() {
        return !!getJwt() && !sessaoValida() && estaOnline();
    }

    function salvarSessao(dados) {
        if (dados && dados.token) gravar(TOKEN_KEY, dados.token);
        if (dados && dados.user) gravar(USER_KEY, JSON.stringify(dados.user));
    }

    /**
     * Regrava a sessão em todas as camadas.
     *
     * Chamado quando o app vai para segundo plano ou está sendo fechado. No
     * iOS, o localStorage de PWA é o primeiro a ser descartado sob pressão de
     * memória; renovar o cookie e o sessionStorage nesse momento é o que evita
     * o líder abrir o app no dia seguinte e cair na tela de login.
     */
    function reforcar() {
        var token = getJwt();
        if (!token) return false;
        gravar(TOKEN_KEY, token);

        var user = ler(USER_KEY);
        if (user) gravar(USER_KEY, user);

        var equipe = ler(EQUIPE_KEY);
        if (equipe) gravar(EQUIPE_KEY, equipe);

        return true;
    }

    /**
     * Exige sessão no carregamento da página.
     * Offline com token vencido passa (modo campo); sem token nenhum, não.
     */
    function requireAuth() {
        if (!sessaoUsavel()) { irParaLogin('Sem sessão válida.'); return false; }
        return true;
    }

    // ======================================================================
    // PERMISSÕES E ESCOPOS
    // ======================================================================

    var SISTEMA = 'refeicoes';

    function permissoes() {
        var u = getUsuario();
        return (u && u.permissoes) || [];
    }

    // ======================================================================
    // PERMISSÕES EM TEMPO QUASE REAL
    //
    // As permissões vinham congeladas: eram gravadas no login e só mudavam
    // com logout/login. Liberar ou negar uma tela na IAM não valia nada até
    // a pessoa sair e entrar de novo — ruim justamente quando se quer TIRAR
    // um acesso. Agora o app reconfere no servidor de tempos em tempos e
    // quando volta ao primeiro plano, e avisa quem quiser repintar a tela.
    // ======================================================================

    var TTL_PERMISSOES = 45000;      // ms entre reconferências
    var _ultimaVerificacao = 0;
    var _verificando = null;
    var _aoMudarPermissoes = [];

    /** Registra quem deve ser avisado quando as permissões mudarem. */
    function aoMudarPermissoes(fn) {
        if (typeof fn === 'function') _aoMudarPermissoes.push(fn);
    }

    function _assinaturaPermissoes(u) {
        if (!u) return '';
        return [
            u.admin ? '1' : '0',
            u.global ? '1' : '0',
            (u.permissoes || []).slice().sort().join(','),
        ].join('|');
    }

    /**
     * Reconfere a identidade no servidor e atualiza o que está guardado.
     * `forcar` ignora o intervalo (use ao voltar para o app).
     */
    function revalidar(forcar) {
        if (!getJwt() || !estaOnline()) return Promise.resolve(false);

        var agora = Date.now();
        if (!forcar && (agora - _ultimaVerificacao) < TTL_PERMISSOES) {
            return Promise.resolve(false);
        }
        if (_verificando) return _verificando;      // uma de cada vez

        _ultimaVerificacao = agora;
        var antes = _assinaturaPermissoes(getUsuario());

        _verificando = apiFetch('/api/auth/verify')
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                if (!d || d.error || !d.user) return false;

                gravar(USER_KEY, JSON.stringify(d.user));

                var mudou = _assinaturaPermissoes(d.user) !== antes;
                if (mudou) {
                    _aoMudarPermissoes.forEach(function (fn) {
                        try { fn(d.user); } catch (e) { /* um ouvinte ruim não derruba os outros */ }
                    });
                }
                return mudou;
            })
            .catch(function () { return false; })
            .then(function (r) { _verificando = null; return r; });

        return _verificando;
    }

    /**
     * Só o admin de verdade passa por cima das permissões.
     *
     * 'global' NÃO entra aqui: ele diz de quais equipes a pessoa vê os
     * dados (ver verTudo), não quais telas ela usa. Enquanto os dois eram a
     * mesma coisa, quem era global — a gerência, por exemplo — via o app
     * inteiro mesmo com a IAM liberando uma única tela.
     */
    function ehAdmin() {
        var u = getUsuario();
        return !!(u && u.admin);
    }

    /** true se a pessoa pode executar a ação (ex.: 'refeicoes.pedido.criar'). */
    function pode(acao) {
        var u = getUsuario();
        if (!u) return false;
        if (ehAdmin()) return true;
        return permissoes().indexOf(acao) !== -1;
    }

    /** true se a pessoa pode ver a tela (ex.: temTela('/temperatura')). */
    function temTela(rota) {
        var u = getUsuario();
        if (!u) return false;
        if (ehAdmin()) return true;
        return permissoes().indexOf(SISTEMA + '.tela:' + rota) !== -1;
    }

    var EQUIPES_KEY = 'larsil_equipes';   // equipes resolvidas pelo servidor

    function escopos() {
        var u = getUsuario();
        return (u && u.escopos) || [];
    }

    /** Vê tudo? (admin, global, ou algum escopo GLOBAL) */
    function verTudo() {
        var u = getUsuario();
        if (!u) return false;
        if (u.admin || u.global) return true;
        return escopos().some(function (e) { return e.tipo === 'GLOBAL'; });
    }

    /**
     * Busca no servidor as equipes que esta pessoa opera e guarda no aparelho.
     *
     * Por que não resolvemos aqui: o escopo de liderança mais comum na Larsil
     * é COORDENADOR/SUPERVISOR, e o valor é o NOME da pessoa. Traduzir esse
     * nome em códigos de equipe exige o ORGANOGRAMA — quem faz isso é o
     * backend, em /api/minhas-equipes.
     *
     * O resultado fica em cache porque no campo o app abre sem sinal.
     */
    function carregarEquipes() {
        if (!getJwt() || !sessaoValida() || !estaOnline()) {
            return Promise.resolve(equipesDisponiveis());
        }

        return apiFetch('/api/minhas-equipes')
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                if (!d || d.error || !Array.isArray(d.equipes)) return equipesDisponiveis();

                var lista = d.equipes.map(function (e) {
                    return {
                        valor: String(e.equipe).toUpperCase(),
                        tipo: e.origem || 'EQUIPE',
                        projeto: e.projeto || '',
                        lider: e.lider || ''
                    };
                });

                try {
                    gravar(EQUIPES_KEY, JSON.stringify({
                        lista: lista,
                        verTudo: !!d.verTudo,
                        login: (getUsuario() || {}).login || ''
                    }));
                } catch (e) { /* noop */ }

                return lista;
            })
            .catch(function () { return equipesDisponiveis(); });
    }

    /**
     * Equipes que a pessoa pode operar. Lê o cache do servidor; se ainda não
     * houver, cai para os escopos diretos do token (EQUIPE/PROJETO).
     * Devolve [{ valor, tipo, projeto, lider }].
     */
    function equipesDisponiveis() {
        try {
            var bruto = ler(EQUIPES_KEY);
            if (bruto) {
                var dados = JSON.parse(bruto);
                var loginAtual = (getUsuario() || {}).login || '';
                // Cache de outra pessoa (aparelho compartilhado) não serve
                if (Array.isArray(dados.lista) && dados.login === loginAtual) {
                    return dados.lista;
                }
            }
        } catch (e) { /* cai no fallback */ }

        var vistos = {};
        var lista = [];
        escopos().forEach(function (e) {
            if (!e || !e.valor) return;
            if (e.tipo !== 'EQUIPE' && e.tipo !== 'PROJETO') return;
            var v = String(e.valor).trim().toUpperCase();
            if (!v || vistos[v]) return;
            vistos[v] = true;
            lista.push({ valor: v, tipo: e.tipo, projeto: '', lider: '' });
        });
        lista.sort(function (a, b) { return a.valor.localeCompare(b.valor); });
        return lista;
    }

    /** Dados extras da equipe ativa (projeto, líder), quando conhecidos. */
    function equipeInfo(codigo) {
        codigo = String(codigo || getEquipe()).toUpperCase();
        return equipesDisponiveis().find(function (e) { return e.valor === codigo; }) || null;
    }

    // ======================================================================
    // EQUIPE ATIVA
    // ======================================================================

    function getEquipe() { return ler(EQUIPE_KEY) || ''; }

    function setEquipe(equipe) {
        var v = String(equipe || '').trim().toUpperCase();
        if (!v) return '';
        gravar(EQUIPE_KEY, v);
        return v;
    }

    /**
     * Projeto da equipe ativa.
     * Prefere o valor que veio do ORGANOGRAMA; só cai para o prefixo numérico
     * do código quando a equipe ainda não foi resolvida pelo servidor.
     */
    function getProjeto() {
        var info = equipeInfo();
        if (info && info.projeto) return String(info.projeto);
        return getEquipe().replace(/[^0-9]/g, '');
    }

    /**
     * Resolve a equipe logo após o login.
     * Devolve a equipe escolhida, ou null quando a pessoa precisa escolher.
     */
    function resolverEquipe() {
        var atual = getEquipe();
        var disponiveis = equipesDisponiveis();

        // Quem vê tudo mantém o que já estava escolhido (ou escolhe na tela)
        if (verTudo()) return atual || null;

        // A equipe guardada saiu do escopo da pessoa (a TI mexeu): descarta.
        //
        // Só quando REALMENTE temos a lista. Lista vazia significa "ainda não
        // consultei o servidor" (primeiro load, ou offline) — descartar aí
        // expulsaria para a tela de escolha alguém que já tinha equipe.
        if (atual && disponiveis.length > 0 &&
            !disponiveis.some(function (e) { return e.valor === atual; })) {
            apagar(EQUIPE_KEY);
            atual = '';
        }

        if (atual) return atual;
        if (disponiveis.length === 1) return setEquipe(disponiveis[0].valor);
        return null;   // 0 = sem escopo (a TI precisa configurar) | 2+ = escolher
    }

    // ======================================================================
    // CHAMADAS À API
    // ======================================================================

    /** Erro emitido quando a chamada não pode sair do aparelho agora. */
    function ErroOffline(mensagem) {
        var e = new Error(mensagem || 'Sem conexão');
        e.offline = true;
        return e;
    }

    /**
     * Fetch para a nossa API, com o token e tratamento de sessão.
     * Nunca mandamos `equipe` na query: o backend lê o escopo do token.
     */
    function apiFetch(path, options) {
        options = options || {};
        var jwt = getJwt();

        if (!jwt) {
            irParaLogin('Sem token.');
            return Promise.reject(new Error('Não autenticado'));
        }

        // Token vencido: online manda reautenticar, offline vira erro de fila
        if (!sessaoValida()) {
            if (!estaOnline()) return Promise.reject(ErroOffline('Token vencido e sem conexão'));
            irParaLogin('Sessão expirada.');
            return Promise.reject(new Error('Sessão expirada'));
        }

        var headers = Object.assign(
            { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + jwt },
            options.headers || {}
        );

        var equipe = getEquipe();
        if (equipe) headers['X-Equipe'] = equipe;   // qual equipe, dentro do escopo, está ativa

        var url = path.indexOf('http') === 0 ? path : API_BASE_URL + path;

        return fetch(url, Object.assign({}, options, { headers: headers }))
            .then(function (response) {
                if (response.status === 401) irParaLogin('Sessão expirada.');
                if (response.status === 403) {
                    // 403 de SESSÃO (conta desativada) derruba; 403 de AÇÃO não.
                    response.clone().json().then(function (b) {
                        if (b && b.motivo === 'INATIVO') {
                            irParaLogin('Conta desativada.');
                        }
                    }).catch(function () { /* noop */ });
                }
                return response;
            });
    }

    // ======================================================================
    // FOTOS (resolvidas pelo Painel PCP, por nome)
    // ======================================================================

    var fotoBase = '';
    try { fotoBase = localStorage.getItem(FOTO_KEY) || ''; } catch (e) { /* noop */ }

    /** Busca a URL do resolvedor de fotos uma vez e memoriza. */
    function carregarConfig() {
        return fetch(API_BASE_URL + '/api/config')
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (c) {
                if (c && c.fotoBaseUrl) {
                    fotoBase = c.fotoBaseUrl;
                    try { localStorage.setItem(FOTO_KEY, fotoBase); } catch (e) { /* noop */ }
                }
                return c || {};
            })
            .catch(function () { return {}; });
    }

    function fotoUrl(nome) {
        if (!fotoBase || !nome) return '';
        return fotoBase + '/' + encodeURIComponent(String(nome).trim());
    }

    function iniciais(nome) {
        return String(nome || '').trim().split(/\s+/)
            .map(function (s) { return s[0]; }).slice(0, 2).join('').toUpperCase();
    }

    /** Monta um avatar (<img> com fallback para iniciais). */
    function avatar(nome, opts) {
        opts = opts || {};
        var tamanho = opts.size || 32;

        var box = document.createElement('div');
        box.className = 'avatar-foto' + (opts.className ? ' ' + opts.className : '');
        box.style.width = tamanho + 'px';
        box.style.height = tamanho + 'px';
        box.title = nome || '';

        var url = fotoUrl(nome);
        if (!url) {
            box.textContent = iniciais(nome);
            return box;
        }

        var img = document.createElement('img');
        img.alt = '';
        img.referrerPolicy = 'no-referrer';
        if (opts.lazy !== false) img.loading = 'lazy';
        img.src = url;
        img.onerror = function () {
            box.replaceChildren();
            box.textContent = iniciais(nome);
        };
        box.appendChild(img);

        if (opts.clicavel !== false) {
            box.classList.add('avatar-clicavel');
            box.addEventListener('click', function (e) {
                e.stopPropagation();
                abrirLightbox(nome);
            });
        }

        return box;
    }

    function abrirLightbox(nome) {
        var url = fotoUrl(nome);
        if (!url) return;

        var fundo = document.createElement('div');
        fundo.className = 'foto-lightbox';

        var caixa = document.createElement('div');
        caixa.className = 'foto-lightbox-caixa';

        var img = document.createElement('img');
        img.src = url;
        img.alt = nome;
        img.referrerPolicy = 'no-referrer';

        var legenda = document.createElement('p');
        legenda.className = 'foto-lightbox-nome';
        legenda.textContent = nome;

        caixa.append(img, legenda);
        fundo.appendChild(caixa);

        function fechar() {
            fundo.remove();
            document.removeEventListener('keydown', aoTeclar);
        }
        function aoTeclar(e) { if (e.key === 'Escape') fechar(); }

        fundo.addEventListener('click', fechar);
        document.addEventListener('keydown', aoTeclar);
        document.body.appendChild(fundo);
    }

    // ======================================================================
    // TOASTS — no lugar dos alert() bloqueantes
    // ======================================================================

    var ICONES = { ok: '✅', erro: '⚠️', aviso: '🔔', info: 'ℹ️' };

    function toast(mensagem, tipo, duracao) {
        tipo = tipo || 'info';

        var pilha = document.querySelector('.lar-toasts');
        if (!pilha) {
            pilha = document.createElement('div');
            pilha.className = 'lar-toasts';
            pilha.setAttribute('role', 'status');
            pilha.setAttribute('aria-live', 'polite');
            document.body.appendChild(pilha);
        }

        var el = document.createElement('div');
        el.className = 'lar-toast lar-toast-' + tipo;

        var icone = document.createElement('span');
        icone.className = 'lar-toast-icone';
        icone.textContent = ICONES[tipo] || ICONES.info;

        var texto = document.createElement('div');
        texto.style.flex = '1';
        // textContent: a mensagem pode conter nome de fornecedor vindo do banco
        texto.textContent = mensagem;

        el.append(icone, texto);
        pilha.appendChild(el);

        var ms = duracao || (tipo === 'erro' ? 6000 : 3600);
        var saida = setTimeout(function () { fechar(); }, ms);

        function fechar() {
            clearTimeout(saida);
            el.classList.add('lar-saindo');
            setTimeout(function () { el.remove(); }, 240);
        }
        el.addEventListener('click', fechar);

        return fechar;
    }

    function logout() {
        limpar();
        try { sessionStorage.clear(); } catch (e) { /* noop */ }
        global.location.href = '/login.html';
    }

    global.SESSION = {
        API_BASE_URL: API_BASE_URL,
        SISTEMA: SISTEMA,

        // token e identidade
        getJwt: getJwt,
        getUsuario: getUsuario,
        expiraEm: expiraEm,
        sessaoValida: sessaoValida,
        sessaoUsavel: sessaoUsavel,
        precisaReautenticar: precisaReautenticar,
        estaOnline: estaOnline,
        salvarSessao: salvarSessao,
        reforcar: reforcar,
        requireAuth: requireAuth,
        limpar: limpar,
        logout: logout,

        // permissões e escopo
        pode: pode,
        temTela: temTela,
        permissoes: permissoes,
        revalidar: revalidar,
        aoMudarPermissoes: aoMudarPermissoes,
        escopos: escopos,
        verTudo: verTudo,
        equipesDisponiveis: equipesDisponiveis,
        carregarEquipes: carregarEquipes,
        equipeInfo: equipeInfo,

        // equipe ativa
        getEquipe: getEquipe,
        setEquipe: setEquipe,
        getProjeto: getProjeto,
        resolverEquipe: resolverEquipe,

        // rede
        apiFetch: apiFetch,
        carregarConfig: carregarConfig,

        // fotos
        fotoUrl: fotoUrl,
        iniciais: iniciais,
        avatar: avatar,
        abrirLightbox: abrirLightbox,

        // avisos
        toast: toast
    };
})(window);
