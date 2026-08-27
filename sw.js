// Service Worker para Background Sync e Notificações
console.log('🚀 Service Worker carregado');

// 🎯 CACHE PARA PERSISTÊNCIA DE LOGIN iOS PWA
let loginDataCache = null;

const CACHE_NAME = 'refeicoes-pwa-v7'; // 🌲 v5.0.1 — cache HTTP do CSS/JS parava de pegar edição nova
const urlsToCache = [
    '/login.html',
    '/escolher-equipe.html',
    '/primeiro-acesso.html',
    '/sistema-pedidos.html',
    '/assets/theme.css',
    '/assets/app.css',
    '/assets/login.css',
    '/assets/session.js',
    '/assets/larsil-logo.png',
    '/assets/larsil-marca.png',
    '/assets/icone-192.png',
    '/assets/icone-512.png',
    '/js/app-modules.js',
    '/manifest.json'
];

// Detectar se é iOS (sem usar navigator/window no SW)
const isIOS = false; // Simplificado para Service Worker
const isStandalone = false; // Simplificado para Service Worker

console.log('📱 Ambiente Service Worker:', { isIOS, isStandalone });

// Configurar notificações diárias às 19:30
function scheduleNotification() {
    const now = new Date();
    const targetTime = new Date();
    targetTime.setHours(19, 30, 0, 0); // 19:30 (7:30 PM)
    
    // Se já passou das 19:30 hoje, agendar para amanhã
    if (now > targetTime) {
        targetTime.setDate(targetTime.getDate() + 1);
    }
    
    const timeUntilNotification = targetTime.getTime() - now.getTime();
    
    console.log('⏰ Próxima notificação em:', new Date(targetTime).toLocaleString('pt-BR'));
    
    setTimeout(() => {
        showNotification();
        // Reagendar para o próximo dia
        scheduleNotification();
    }, timeUntilNotification);
}

// Mostrar notificação
function showNotification() {
    const options = {
        title: '🍽️ Hora do Pedido de Refeição!',
        body: 'Não esqueça de fazer seu pedido de refeição para amanhã! 😋',
        icon: '/assets/icone-192.png',
        badge: '/assets/icone-192.png',
        tag: 'meal-reminder',
        requireInteraction: true,
        actions: [
            {
                action: 'fazer-pedido',
                title: '🍽️ Fazer Pedido'
            },
            {
                action: 'lembrar-depois',
                title: '⏰ Lembrar em 30min'
            }
        ],
        data: {
            url: '/sistema-pedidos.html',
            timestamp: new Date().toISOString()
        }
    };
    
    self.registration.showNotification(options.title, options);
    console.log('🔔 Notificação de refeição enviada às', new Date().toLocaleString('pt-BR'));
}

// Iniciar agendamento quando o SW é ativado
self.addEventListener('activate', event => {
    console.log('⚡ Service Worker ativado');
    scheduleNotification(); // Iniciar notificações
    // ... resto do código de ativação
});

// Instalar Service Worker
self.addEventListener('install', event => {
    console.log('📦 Service Worker instalando...');
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => {
                console.log('💾 Cache criado');
                return cache.addAll(urlsToCache);
            })
            .catch(err => console.log('❌ Erro no cache:', err))
    );
    
    // Para iOS PWAs, aguardar ativação manual (não forçar)
    if (isIOS) {
        console.log('🍎 iOS detectado - Service Worker instalado (aguardando ativação)');
        // Removido self.skipWaiting() para evitar recargas automáticas
    }
});

// Ativar Service Worker
self.addEventListener('activate', event => {
    console.log('⚡ Service Worker ativado');
    event.waitUntil(
        caches.keys().then(cacheNames => {
            return Promise.all(
                cacheNames.map(cacheName => {
                    if (cacheName !== CACHE_NAME) {
                        console.log('🗑️ Removendo cache antigo:', cacheName);
                        return caches.delete(cacheName);
                    }
                })
            );
        }).then(() => {
            // Toma controle suave das páginas (sem forçar reload)
            console.log('🎯 Service Worker assumindo controle suave das páginas');
            return self.clients.claim();
        })
    );
});

// ==========================================================================
// INTERCEPTAÇÃO DE REQUESTS
//
// A estratégia antiga era cache-first para TUDO. Era ela que deixava o app
// preso numa versão antiga — e foi por causa disso que existiu aquele
// bloqueador de "ATUALIZAÇÃO OBRIGATÓRIA" na tela.
//
// Agora:
//   • HTML, CSS e JS  -> rede primeiro, cache como rede de segurança offline
//   • imagens/fontes  -> cache primeiro (não mudam e pesam)
//   • /api            -> nunca cacheia (dado de pedido não pode vir velho)
// ==========================================================================
const EXTENSOES_ESTATICAS = /\.(png|jpg|jpeg|svg|gif|webp|woff2?|ttf|ico)$/i;

self.addEventListener('fetch', event => {
    const req = event.request;

    // Só GET entra em cache; POST de pedido/aferição jamais.
    if (req.method !== 'GET') return;

    const url = new URL(req.url);

    // Outra origem (fontes do Google, EmailJS, fotos do PCP): deixa passar
    if (url.origin !== self.location.origin) return;

    // API sempre da rede
    if (url.pathname.startsWith('/api/')) return;

    // Estáticos pesados: cache primeiro
    if (EXTENSOES_ESTATICAS.test(url.pathname)) {
        event.respondWith(
            caches.match(req).then(cacheado => cacheado || fetch(req).then(resposta => {
                if (resposta.ok) {
                    const copia = resposta.clone();
                    caches.open(CACHE_NAME).then(c => c.put(req, copia));
                }
                return resposta;
            }))
        );
        return;
    }

    // Código e páginas: rede primeiro, com o cache guardando a versão nova
    event.respondWith(
        fetch(req)
            .then(resposta => {
                if (resposta.ok) {
                    const copia = resposta.clone();
                    caches.open(CACHE_NAME).then(c => c.put(req, copia));
                }
                return resposta;
            })
            .catch(() => caches.match(req).then(cacheado => {
                if (cacheado) return cacheado;
                // Navegação offline sem cache da rota: cai no app
                if (req.mode === 'navigate') return caches.match('/sistema-pedidos.html');
                return new Response('', { status: 504, statusText: 'Offline' });
            }))
    );
});

// 🔄 BACKGROUND SYNC - Esta é a mágica!
self.addEventListener('sync', event => {
    console.log('🔄 Background Sync triggered:', event.tag);
    
    if (event.tag === 'database-sync') {
        event.waitUntil(processDatabaseQueueBackground());
    } else if (event.tag === 'temperatura-sync') {
        event.waitUntil(processTemperaturaQueueBackground());
    }
});

// Função para processar fila em background
async function processDatabaseQueueBackground() {
    console.log('💾 Processando fila do banco em background...');
    
    try {
        // Buscar dados da fila no IndexedDB/localStorage
        const databaseQueue = await getDatabaseQueue();
        
        if (databaseQueue.length === 0) {
            console.log('✅ Fila vazia - nada para processar');
            return;
        }
        
        console.log(`📤 Processando ${databaseQueue.length} pedidos em background`);
        
        let successCount = 0;
        let errorCount = 0;
        
        for (let i = 0; i < databaseQueue.length; i++) {
            const item = databaseQueue[i];
            
            try {
                // Determinar URL base dinamicamente
                const baseUrl = await getServerBaseUrl();
                
                const response = await fetch(`${baseUrl}/api/salvar-pedido`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(item.data)
                });
                
                const result = await response.json();
                
                if (result.error) {
                    console.error(`❌ Erro ao enviar pedido da fila:`, result.message);
                    errorCount++;
                } else {
                    console.log(`✅ Pedido enviado em background: ${item.data.tipo_refeicao}`);
                    successCount++;
                }
                
            } catch (error) {
                console.error(`❌ Erro de conexão ao enviar pedido:`, error);
                errorCount++;
            }
        }
        
        // Limpar fila apenas se todos foram processados com sucesso
        if (errorCount === 0) {
            await clearDatabaseQueue();
            console.log('🗑️ Fila limpa após sucesso em background');
            
            // Mostrar notificação de sucesso
            if (successCount > 0) {
                self.registration.showNotification('✅ Pedidos Enviados', {
                    body: `${successCount} pedido(s) foram enviados automaticamente!`,
                    icon: '/assets/icone-192.png',
                    badge: '/assets/icone-192.png',
                    tag: 'pedidos-enviados'
                });
            }
        } else {
            console.log(`⚠️ ${errorCount} pedidos permaneceram na fila`);
        }
        
    } catch (error) {
        console.error('❌ Erro no background sync:', error);
    }
}

// Função para processar fila de temperatura em background
async function processTemperaturaQueueBackground() {
    console.log('🌡️ Processando fila de temperatura em background...');
    
    try {
        // Buscar dados da fila de temperatura
        const temperaturaQueue = await getTemperaturaQueue();
        
        if (temperaturaQueue.length === 0) {
            console.log('✅ Fila de temperatura vazia - nada para processar');
            return;
        }
        
        console.log(`📤 Processando ${temperaturaQueue.length} aferição(ões) de temperatura em background`);
        
        let successCount = 0;
        let errorCount = 0;
        
        for (let i = 0; i < temperaturaQueue.length; i++) {
            const item = temperaturaQueue[i];
            
            try {
                // Determinar URL base dinamicamente
                const baseUrl = await getServerBaseUrl();
                
                const response = await fetch(`${baseUrl}/api/afericao-temperatura`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(item)
                });
                
                const result = await response.json();
                
                if (result.error) {
                    console.error(`❌ Erro ao enviar aferição da fila:`, result.message);
                    errorCount++;
                } else {
                    console.log(`✅ Aferição enviada em background: Pedido ${item.pedido_id}`);
                    successCount++;
                }
                
            } catch (error) {
                console.error(`❌ Erro de conexão ao enviar aferição:`, error);
                errorCount++;
            }
        }
        
        // Limpar fila apenas se todos foram processados com sucesso
        if (errorCount === 0) {
            await clearTemperaturaQueue();
            console.log('🗑️ Fila de temperatura limpa após sucesso em background');
            
            // Mostrar notificação de sucesso
            if (successCount > 0) {
                self.registration.showNotification('🌡️ Aferições Enviadas', {
                    body: `${successCount} aferição(ões) de temperatura foram enviadas automaticamente!`,
                    icon: '/assets/icone-192.png',
                    badge: '/assets/icone-192.png',
                    tag: 'temperaturas-enviadas'
                });
            }
        } else {
            console.log(`⚠️ ${errorCount} aferições permaneceram na fila`);
        }
        
    } catch (error) {
        console.error('❌ Erro no background sync de temperatura:', error);
    }
}

// Função para obter fila de temperatura
async function getTemperaturaQueue() {
    // Usar dados sincronizados pela página principal
    return self.temperaturaQueueData || [];
}

// Função para limpar fila de temperatura
async function clearTemperaturaQueue() {
    // Comunicar com página principal para limpar
    const clients = await self.clients.matchAll();
    clients.forEach(client => {
        client.postMessage({
            type: 'CLEAR_TEMPERATURA_QUEUE'
        });
    });
    
    // Limpar dados locais também
    self.temperaturaQueueData = [];
}

// Função para obter fila do localStorage via message
async function getDatabaseQueue() {
    // Usar dados sincronizados pela página principal
    return self.databaseQueueData || [];
}

// Função para limpar fila
async function clearDatabaseQueue() {
    // Comunicar com página principal para limpar
    const clients = await self.clients.matchAll();
    clients.forEach(client => {
        client.postMessage({
            type: 'CLEAR_DATABASE_QUEUE'
        });
    });
    
    // Limpar dados locais também
    self.databaseQueueData = [];
}

// Função para obter URL base do servidor
async function getServerBaseUrl() {
    // Verificar se é localhost ou ngrok
    const hostname = self.location.hostname;
    
    if (hostname === 'localhost' || hostname === '127.0.0.1') {
        return 'http://localhost:8082';
    }
    
    // Para ngrok ou outros, usar a origem atual
    return self.location.origin;
}

// Escutar mensagens da página principal
self.addEventListener('message', event => {
    if (event.data.type === 'UPDATE_DATABASE_QUEUE') {
        // Atualizar dados da fila localmente no SW
        self.databaseQueueData = event.data.queue;
        console.log('📝 Fila de pedidos atualizada no SW:', self.databaseQueueData?.length || 0);
    } else if (event.data.type === 'UPDATE_TEMPERATURA_QUEUE') {
        // Atualizar dados da fila de temperatura localmente no SW
        self.temperaturaQueueData = event.data.queue;
        console.log('🌡️ Fila de temperatura atualizada no SW:', self.temperaturaQueueData?.length || 0);
    } else if (event.data.type === 'CLEAR_TEMPERATURA_QUEUE') {
        // Limpar fila de temperatura
        self.temperaturaQueueData = [];
        console.log('🗑️ Fila de temperatura limpa no SW');
    } else if (event.data.type === 'FORCE_BACKGROUND_SYNC') {
        // Forçar processamento imediato da fila
        console.log('🚀 Background sync forçado pelo app');
        processDatabaseQueueBackground();
        processTemperaturaQueueBackground();
    } else if (event.data.type === 'KEEP_ALIVE') {
        // Manter Service Worker ativo
        console.log('💓 Service Worker mantido vivo:', new Date().toLocaleTimeString());
    } else if (event.data.type === 'SAVE_LOGIN') {
        // 🎯 SALVAR LOGIN NO SERVICE WORKER PARA iOS PWA
        loginDataCache = event.data.data;
        console.log('💾 Login salvo no Service Worker para persistência iOS:', loginDataCache);
    } else if (event.data.type === 'CLEAR_LOGIN') {
        // 🗑️ LIMPAR LOGIN DO SERVICE WORKER
        loginDataCache = null;
        console.log('🚪 Login removido do Service Worker');
    } else if (event.data.type === 'GET_LOGIN') {
        // 📤 RETORNAR LOGIN SALVO NO SERVICE WORKER
        event.ports[0].postMessage({
            type: 'LOGIN_DATA',
            data: loginDataCache
        });
        console.log('📤 Login enviado do Service Worker:', loginDataCache ? 'encontrado' : 'não encontrado');
    }
});

// Manter Service Worker ativo - eventos especiais
self.addEventListener('sync', event => {
    console.log('🔄 Evento sync recebido:', event.tag);
    if (event.tag === 'database-queue-sync') {
        event.waitUntil(processDatabaseQueueBackground());
    }
});

// Interceptar fechamento do app para forçar sync
self.addEventListener('beforeunload', event => {
    console.log('⚠️ App fechando - forçando sync');
    self.registration.sync.register('database-queue-sync');
});

// Periodic Background Sync (se suportado)
self.addEventListener('periodicsync', event => {
    if (event.tag === 'database-queue-periodic') {
        console.log('⏰ Sync periódico ativado');
        event.waitUntil(processDatabaseQueueBackground());
    }
});

// Para iOS PWAs: Eventos especiais para manter atividade
if (isIOS) {
    // Interceptar push notifications para iOS
    self.addEventListener('push', event => {
        console.log('🍎 Push recebido no iOS PWA');
        event.waitUntil(processDatabaseQueueBackground());
    });
    
    // Interceptar quando volta do background (iOS)
    self.addEventListener('focus', event => {
        console.log('🍎 iOS PWA voltou ao foco');
        processDatabaseQueueBackground();
    });
    
    // Timer interno para iOS (tentativa de manter ativo)
    setInterval(() => {
        const now = new Date();
        console.log('🍎 iOS SW Keep-alive:', now.toLocaleTimeString());
        
        // Verificar se há itens na fila e tentar processar
        if (self.databaseQueueData && self.databaseQueueData.length > 0) {
            console.log('🍎 iOS: Processando fila automaticamente');
            processDatabaseQueueBackground();
        }
    }, 60000); // A cada 1 minuto
}

// Handler para cliques nas notificações
self.addEventListener('notificationclick', event => {
    console.log('🔔 Notificação clicada:', event.action);
    
    event.notification.close();
    
    if (event.action === 'fazer-pedido') {
        // Abrir o sistema de pedidos
        event.waitUntil(
            clients.openWindow('/sistema-pedidos.html')
        );
    } else if (event.action === 'lembrar-depois') {
        // Reagendar notificação em 30 minutos
        setTimeout(() => {
            showNotification();
        }, 30 * 60 * 1000); // 30 minutos
        
        console.log('⏰ Notificação reagendada para 30 minutos');
    } else {
        // Clique no corpo da notificação - abrir o app
        event.waitUntil(
            clients.openWindow('/sistema-pedidos.html')
        );
    }
});

// Handler para fechar notificação
self.addEventListener('notificationclose', event => {
    console.log('🔔 Notificação fechada');
});

console.log('✅ Service Worker configurado com Background Sync e Notificações');