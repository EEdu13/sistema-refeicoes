// ========================================
// 🎯 MÓDULOS DO SISTEMA DE REFEIÇÕES
// ========================================

// ========================================
// 1. ESTADO GLOBAL UNIFICADO
// ========================================
const AppState = {
    // Autenticação
    user: {
        projeto: '',
        equipe: '',
        nome: '',
        lider: ''
    },
    
    // Pedido atual
    order: {
        selectedMeals: [],
        selectedEmployees: [],
        otherParticipants: [],
        naoContratadosCount: 0
    },
    
    // Temperatura
    temperature: {
        pending: [],
        current: {},
        images: {
            retirada: null,
            consumo: null,
            timestamps: { retirada: null, consumo: null }
        }
    },
    
    // Problema
    problem: {
        foto1: null,
        foto2: null
    },
    
    // Cache de dados
    cache: {
        fornecedores: [],
        colaboradores: [],
        organograma: [],
        pagcorp: []
    },
    
    // Sistema
    network: {
        isOnline: navigator.onLine
    }
};

// ========================================
// 2. GERENCIADOR DE FORNECEDORES
// ========================================
const SupplierManager = {
    /**
     * Obtém informações completas do fornecedor selecionado
     * @param {string} mealType - Tipo da refeição (cafe, almoco_marmitex, etc)
     * @returns {Object|null} Informações do fornecedor ou null
     */
    getInfo(mealType) {
        const select = document.querySelector(`select[data-meal="${mealType}"]`);
        const input = document.getElementById(`supplier-input-${mealType}`);
        
        if (!select) return null;
        
        const isCustom = select.value === '__custom__';
        const name = this.getName(mealType);
        
        return {
            isCustom,
            value: select.value,
            name,
            inCache: isCustom ? false : this.isInCache(name),
            cached: isCustom ? null : this.getFromCache(name)
        };
    },
    
    /**
     * Obtém o NOME COMPLETO do fornecedor (não o value do select)
     * @param {string} mealType - Tipo da refeição
     * @returns {string|null} Nome completo ou null
     */
    getName(mealType) {
        const select = document.querySelector(`select[data-meal="${mealType}"]`);
        const input = document.getElementById(`supplier-input-${mealType}`);
        
        if (!select) return null;
        
        // Se customizado, retorna o texto digitado
        if (select.value === '__custom__') {
            return input?.value?.trim() || null;
        }
        
        // Se selecionado da lista, retorna o TEXTO da opção
        const option = select.options[select.selectedIndex];
        return option?.text?.trim() || null;
    },
    
    /**
     * Verifica se o fornecedor está no cache
     * @param {string} name - Nome do fornecedor
     * @returns {boolean} True se está no cache
     */
    isInCache(name) {
        if (!name) return false;
        
        return AppState.cache.fornecedores.some(f => 
            f.FORNECEDOR && f.FORNECEDOR.trim() === name.trim()
        );
    },
    
    /**
     * Busca dados completos do fornecedor no cache
     * @param {string} name - Nome do fornecedor
     * @returns {Object|null} Dados do fornecedor ou null
     */
    getFromCache(name) {
        if (!name) return null;
        
        return AppState.cache.fornecedores.find(f => 
            f.FORNECEDOR && f.FORNECEDOR.trim() === name.trim()
        );
    },
    
    /**
     * 🎯 LÓGICA INTELIGENTE DO FECHAMENTO
     * Retorna "SIM" apenas se fornecedor foi SELECIONADO da lista E tem FECHAMENTO na tabela
     * @param {string} mealType - Tipo da refeição
     * @returns {string} "SIM" ou "" (vazio)
     */
    getFechamento(mealType) {
        const info = this.getInfo(mealType);
        
        if (!info) {
            console.log(`⚠️ Não foi possível obter info do fornecedor para ${mealType}`);
            return '';
        }
        
        // REGRA 1: Se customizado (digitado), NÃO tem fechamento
        if (info.isCustom) {
            console.log(`✏️ Fornecedor CUSTOMIZADO "${info.name}" → FECHAMENTO vazio`);
            return '';
        }
        
        // REGRA 2: Se não está no cache, NÃO tem fechamento
        if (!info.cached) {
            console.log(`❌ Fornecedor "${info.name}" não encontrado no cache → FECHAMENTO vazio`);
            return '';
        }
        
        // REGRA 3: Pegar FECHAMENTO do cache (pode estar vazio se não tiver na tabela)
        const fechamento = info.cached.FECHAMENTO || '';
        
        if (fechamento && fechamento.trim() !== '') {
            console.log(`✅ Fornecedor "${info.name}" DA LISTA com FECHAMENTO="${fechamento}"`);
        } else {
            console.log(`⚠️ Fornecedor "${info.name}" DA LISTA mas SEM FECHAMENTO na tabela`);
        }
        
        return fechamento;
    },
    
    /**
     * Obtém preço customizado ou do cache
     * @param {string} mealType - Tipo da refeição
     * @returns {number} Preço
     */
    getPrice(mealType) {
        const priceInput = document.getElementById(`price-${mealType}`);
        
        // Se tem preço digitado, usar esse
        if (priceInput?.value && priceInput.value.trim() !== '') {
            return parseFloat(priceInput.value) || 0;
        }
        
        // Se tem fornecedor selecionado com preço, usar do cache
        const info = this.getInfo(mealType);
        if (info?.cached?.VALOR) {
            return parseFloat(info.cached.VALOR) || 0;
        }
        
        return 0;
    }
};

// ========================================
// 3. GERENCIADOR DE CACHE INTELIGENTE
// ========================================
const CacheManager = {
    VERSION: '1.0.0',
    PREFIX: 'refeicoes_cache_',
    
    /**
     * Salva dados no cache (todas as camadas)
     * @param {string} key - Chave do cache
     * @param {any} value - Valor a ser salvo
     */
    set(key, value) {
        try {
            // 1. Memória (AppState)
            AppState.cache[key] = value;
            
            // 2. LocalStorage
            const cacheData = {
                value,
                timestamp: Date.now(),
                equipe: AppState.user.equipe,
                version: this.VERSION
            };
            localStorage.setItem(this.PREFIX + key, JSON.stringify(cacheData));
            
            // 3. SessionStorage (backup)
            sessionStorage.setItem(this.PREFIX + key + '_backup', JSON.stringify(cacheData));
            
            console.log(`💾 Cache salvo: ${key} (${Array.isArray(value) ? value.length : 'N/A'} itens)`);
        } catch (error) {
            console.error(`❌ Erro ao salvar cache ${key}:`, error);
        }
    },
    
    /**
     * Obtém dados do cache (tenta todas as camadas)
     * @param {string} key - Chave do cache
     * @returns {any|null} Dados ou null
     */
    get(key) {
        try {
            // 1. Tentar memória primeiro (mais rápido)
            if (AppState.cache[key] && Array.isArray(AppState.cache[key]) && AppState.cache[key].length > 0) {
                console.log(`⚡ Cache encontrado na memória: ${key}`);
                return AppState.cache[key];
            }
            
            // 2. Tentar localStorage
            const cached = localStorage.getItem(this.PREFIX + key);
            if (cached) {
                const data = JSON.parse(cached);
                
                // Verificar se é da equipe correta
                if (data.equipe === AppState.user.equipe) {
                    console.log(`📦 Cache encontrado no localStorage: ${key}`);
                    AppState.cache[key] = data.value;
                    return data.value;
                } else {
                    console.log(`⚠️ Cache de equipe diferente ignorado: ${key} (${data.equipe} vs ${AppState.user.equipe})`);
                }
            }
            
            // 3. Tentar sessionStorage (último recurso)
            const sessionCached = sessionStorage.getItem(this.PREFIX + key + '_backup');
            if (sessionCached) {
                const data = JSON.parse(sessionCached);
                console.log(`💼 Cache encontrado no sessionStorage: ${key}`);
                AppState.cache[key] = data.value;
                return data.value;
            }
            
            console.log(`❌ Cache não encontrado: ${key}`);
            return null;
        } catch (error) {
            console.error(`❌ Erro ao ler cache ${key}:`, error);
            return null;
        }
    },
    
    /**
     * Limpa cache específico ou tudo
     * @param {string|null} key - Chave específica ou null para limpar tudo
     */
    clear(key = null) {
        if (key) {
            // Limpar chave específica
            delete AppState.cache[key];
            localStorage.removeItem(this.PREFIX + key);
            sessionStorage.removeItem(this.PREFIX + key + '_backup');
            console.log(`🗑️ Cache limpo: ${key}`);
        } else {
            // Limpar tudo
            AppState.cache = {
                fornecedores: [],
                colaboradores: [],
                organograma: [],
                pagcorp: []
            };
            
            // Limpar localStorage (apenas chaves do sistema)
            Object.keys(localStorage).forEach(k => {
                if (k.startsWith(this.PREFIX)) {
                    localStorage.removeItem(k);
                }
            });
            
            // Limpar sessionStorage
            Object.keys(sessionStorage).forEach(k => {
                if (k.startsWith(this.PREFIX)) {
                    sessionStorage.removeItem(k);
                }
            });
            
            console.log('🗑️ Todo o cache foi limpo');
        }
    },
    
    /**
     * Verifica se cache está válido (não expirado)
     * @param {string} key - Chave do cache
     * @param {number} maxAge - Idade máxima em ms (padrão: 24h)
     * @returns {boolean} True se válido
     */
    isValid(key, maxAge = 24 * 60 * 60 * 1000) {
        try {
            const cached = localStorage.getItem(this.PREFIX + key);
            if (!cached) return false;
            
            const data = JSON.parse(cached);
            const age = Date.now() - data.timestamp;
            
            return age < maxAge;
        } catch {
            return false;
        }
    }
};

// ========================================
// 4. CACHE DE ELEMENTOS DOM
// ========================================
const DOM = {
    // Inicializar cache de elementos
    init() {
        // A autenticação saiu daqui (é login.html); as telas abaixo são o app.
        this.screens = {
            main: document.getElementById('mainScreen'),
            pending: document.getElementById('pendingScreen'),
            historico: document.getElementById('historicoScreen'),
            deposito: document.getElementById('depositoScreen'),
            order: document.getElementById('orderScreen'),
            temperature: document.getElementById('temperatureScreen'),
            problem: document.getElementById('problemScreen')
        };
        
        this.inputs = {
            // Login
            equipe: document.getElementById('equipeInput'),
            
            // Pedido
            withdrawalDate: document.getElementById('withdrawalDateInput'),
            city: document.getElementById('serviceCityInput'),
            requestor: document.getElementById('requestorNameInput'),
            farm: document.getElementById('farmNameInput'),
            cardResponsible: document.getElementById('cardResponsibleInput'),
            pagcorp: document.getElementById('pagcorpInput'),
            hotelName: document.getElementById('hotelNameInput'),
            dailyRate: document.getElementById('dailyRateInput'),
            
            // Temperatura
            tempRetirada: document.getElementById('temperatureRetirada'),
            tempConsumo: document.getElementById('temperatureConsumo'),
            horaRetirada: document.getElementById('horaRetirada'),
            horaConsumo: document.getElementById('horaConsumo'),
            observacoesGerais: document.getElementById('observacoesGerais')
        };
        
        this.containers = {
            employeesList: document.getElementById('employees-list'),
            otherParticipants: document.getElementById('otherParticipants'),
            orderSummary: document.getElementById('orderSummary'),
            summaryContent: document.getElementById('summaryContent'),
            pendingList: document.getElementById('pendingList')
        };
        
        console.log('✅ Cache DOM inicializado');
    },
    
    /**
     * Mostra uma tela e esconde as demais.
     * Rola para o topo: sem isso a tela nova abre na altura em que a anterior
     * estava, e a pessoa acha que o app travou.
     */
    showScreen(screenName) {
        Object.values(this.screens).forEach(screen => {
            if (screen) screen.classList.add('hidden');
        });

        const alvo = this.screens[screenName];
        if (alvo) {
            alvo.classList.remove('hidden');
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        // Deixa a tela atual no <body> para o CSS poder enxugar o cabeçalho
        // onde ele não serve (ex.: pendências, que só precisa do filtro).
        document.body.classList.toggle('tela-pendencias', screenName === 'pending');
    }
};

// ========================================
// 5. UTILITÁRIOS
// ========================================
const Utils = {
    /**
     * Limpa caracteres Unicode problemáticos de strings
     * @param {string} text - Texto a ser limpo
     * @returns {string} Texto limpo
     */
    cleanUnicode(text) {
        if (!text) return '';
        
        return text
            // Remover emojis e ícones
            .replace(/[\u{1F000}-\u{1F6FF}]/gu, '')
            .replace(/[\uD800-\uDFFF]/g, '')
            // Remover caracteres não-ASCII exceto acentos
            .replace(/[^\x00-\x7F\u00C0-\u017F\u0020-\u007E]/g, '')
            // Limpar espaços extras
            .replace(/\s+/g, ' ')
            .trim();
    },
    
    /**
     * Formata data para exibição (DD/MM/YYYY)
     * @param {Date|string} date - Data a formatar
     * @returns {string} Data formatada
     */
    formatDate(date) {
        if (!date) return '';
        
        const d = typeof date === 'string' ? new Date(date) : date;
        const day = String(d.getDate()).padStart(2, '0');
        const month = String(d.getMonth() + 1).padStart(2, '0');
        const year = d.getFullYear();
        
        return `${day}/${month}/${year}`;
    },
    
    /**
     * Obtém data de amanhã no formato YYYY-MM-DD
     * @returns {string} Data de amanhã
     */
    getTomorrow() {
        const tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        return tomorrow.toISOString().split('T')[0];
    },
    
    /**
     * Debounce para otimizar eventos
     * @param {Function} func - Função a ser executada
     * @param {number} wait - Tempo de espera em ms
     * @returns {Function} Função com debounce
     */
    debounce(func, wait) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    }
};

// Exportar para uso global
window.AppState = AppState;
window.SupplierManager = SupplierManager;
window.CacheManager = CacheManager;
window.DOM = DOM;
window.Utils = Utils;

console.log('✅ Módulos do sistema carregados');
