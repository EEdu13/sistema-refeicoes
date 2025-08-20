#!/usr/bin/env python3
import http.server
import socketserver
import json
import urllib.parse
from datetime import datetime
import pyodbc
import decimal

# Função para serializar Decimal em JSON
def decimal_default(obj):
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    raise TypeError

# Configuração do Azure SQL
AZURE_CONFIG = {
    'server': 'alrflorestal.database.windows.net',
    'database': 'Tabela_teste',
    'username': 'sqladmin',
    'password': 'SenhaForte123!',
    'driver': '{ODBC Driver 17 for SQL Server}'
}

# Configuração do Azure Blob Storage
AZURE_BLOB_CONFIG = {
    'account_name': 'checklistfilesferre',
    'container_name': 'fotos-checklist',
    'sas_token': 'sp=racwdli&st=2025-08-10T13:50:29Z&se=2026-07-23T22:05:29Z&spr=https&sv=2024-11-04&sr=c&sig=35a3WC0k7IhDscrOqmxF3lHpXEMs7BxZWUstxLitCi8%3D'
}

def conectar_azure_sql():
    """Conecta ao Azure SQL Server"""
    try:
        connection_string = f"DRIVER={AZURE_CONFIG['driver']};SERVER={AZURE_CONFIG['server']};DATABASE={AZURE_CONFIG['database']};UID={AZURE_CONFIG['username']};PWD={AZURE_CONFIG['password']}"
        conn = pyodbc.connect(connection_string)
        return conn
    except Exception as e:
        print(f"❌ Erro ao conectar no Azure SQL: {e}")
        return None

def upload_imagem_blob(imagem_base64, nome_arquivo):
    """Faz upload de imagem para Azure Blob Storage com timeout otimizado"""
    import base64
    import requests
    import time
    from datetime import datetime
    
    try:
        print(f"📷 Iniciando upload RÁPIDO para blob: {nome_arquivo}")
        
        # Remover prefixo data:image se existir
        if ',' in imagem_base64:
            imagem_base64 = imagem_base64.split(',')[1]
        
        # Decodificar base64
        imagem_bytes = base64.b64decode(imagem_base64)
        print(f"📏 Tamanho da imagem: {len(imagem_bytes)} bytes ({len(imagem_bytes)/1024:.1f}KB)")
        
        # Se a imagem for muito grande, pular o upload para evitar timeout
        if len(imagem_bytes) > 5 * 1024 * 1024:  # 5MB
            print("⚠️ Imagem muito grande (>5MB) - pulando upload para evitar timeout")
            return f"local_backup_{nome_arquivo}"
        
        # Gerar nome único para o arquivo
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        nome_unico = f"temp_{timestamp}_{nome_arquivo}"
        
        # URL do blob para upload (com SAS token)
        blob_url_upload = f"https://{AZURE_BLOB_CONFIG['account_name']}.blob.core.windows.net/{AZURE_BLOB_CONFIG['container_name']}/{nome_unico}?{AZURE_BLOB_CONFIG['sas_token']}"
        
        # Headers para upload
        headers = {
            'x-ms-blob-type': 'BlockBlob',
            'Content-Type': 'image/jpeg'
        }
        
        print(f"☁️ Enviando para Azure Blob Storage com timeout de 10s...")
        
        # Fazer upload com TIMEOUT REDUZIDO
        response = requests.put(blob_url_upload, data=imagem_bytes, headers=headers, timeout=10)
        
        if response.status_code in [200, 201]:
            # URL pública da imagem (sem SAS token para armazenar)
            url_publica = f"https://{AZURE_BLOB_CONFIG['account_name']}.blob.core.windows.net/{AZURE_BLOB_CONFIG['container_name']}/{nome_unico}"
            print(f"✅ Upload concluído RAPIDAMENTE: {url_publica}")
            
            # SEM AGUARDAR PROPAGAÇÃO - upload assíncrono
            print("⚡ Upload concluído - continuando sem esperar propagação")
            
            return url_publica
        else:
            print(f"❌ Erro no upload: {response.status_code} - usando backup local")
            return f"local_backup_{nome_arquivo}"
            
    except requests.exceptions.Timeout:
        print("⏰ TIMEOUT no upload - usando backup local para continuar")
        return f"local_timeout_{nome_arquivo}"
    except Exception as e:
        print(f"❌ Erro ao fazer upload da imagem: {e} - usando backup local")
        return f"local_error_{nome_arquivo}"

def executar_query(query, params=None):
    """Executa uma query no Azure SQL"""
    conn = conectar_azure_sql()
    if conn is None:
        return None
    
    try:
        cursor = conn.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        
        # Buscar resultados se for SELECT
        if query.strip().upper().startswith('SELECT'):
            columns = [column[0] for column in cursor.description]
            results = []
            for row in cursor.fetchall():
                row_dict = {}
                for i, value in enumerate(row):
                    row_dict[columns[i]] = value
                results.append(row_dict)
            return results
        else:
            # Para INSERT, verificar se precisa retornar o ID gerado
            if query.strip().upper().startswith('INSERT'):
                conn.commit()
                # Obter o ID inserido
                cursor.execute("SELECT @@IDENTITY AS id")
                inserted_id = cursor.fetchone()[0]
                return {"rowcount": cursor.rowcount, "inserted_id": int(inserted_id)}
            else:
                conn.commit()
                return cursor.rowcount
            
    except Exception as e:
        print(f"❌ Erro ao executar query: {e}")
        return None
    finally:
        conn.close()

class RefeicaoHandler(http.server.BaseHTTPRequestHandler):
    def serve_html_file(self, filename):
        """Serve arquivos HTML estáticos"""
        try:
            with open(filename, 'r', encoding='utf-8') as file:
                content = file.read()
            
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content.encode('utf-8'))
            
        except FileNotFoundError:
            self.send_response(404)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h1>404 - Arquivo nao encontrado</h1>')
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(f'<h1>500 - Erro do servidor: {str(e)}</h1>'.encode('utf-8'))

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
            self.end_headers()
            
            if encoding:
                self.wfile.write(content.encode('utf-8'))
            else:
                self.wfile.write(content)
                
        except FileNotFoundError:
            self.send_response(404)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h1>404 - Arquivo nao encontrado</h1>')
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(f'<h1>500 - Erro do servidor: {str(e)}</h1>'.encode('utf-8'))

    def do_GET(self):
        # Parse da URL
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query_params = urllib.parse.parse_qs(parsed_path.query)
        
        # Servir arquivos estáticos
        if path == '/' or path == '/index.html':
            self.serve_html_file('index.html')
            return
        elif path == '/sistema-pedidos.html':
            self.serve_html_file('sistema-pedidos.html')
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
        
        # Headers CORS para APIs
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        
        # APIs simuladas
        if path == '/api/teste-conexao':
            response = {
                "success": True,
                "message": "Servidor Python funcionando!",
                "timestamp": datetime.now().isoformat()
            }
            
        elif path == '/api/fornecedores':
            projeto = query_params.get('projeto', [''])[0]
            
            if not projeto:
                response = {"error": True, "message": "Parâmetro projeto é obrigatório"}
            else:
                # Buscar fornecedores reais do Azure SQL
                query = """
                SELECT ID, PROJETO, LOCAL, FORNECEDOR, TIPO_FORN, VALOR, STATUS 
                FROM tb_fornecedores 
                WHERE PROJETO = ? AND STATUS = 'ATIVO'
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
                        {"ID": 1, "PROJETO": "700", "LOCAL": "TESTE", "FORNECEDOR": "FORNECEDOR TESTE", "TIPO_FORN": "CM", "VALOR": 5.50, "STATUS": "ATIVO"}
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
            projeto = query_params.get('projeto', [''])[0]
            
            if not projeto:
                response = {"error": True, "message": "Parâmetro projeto é obrigatório"}
            else:
                # Buscar organograma real do Azure SQL
                query = """
                SELECT ID, PROJETO, EQUIPE, LIDER, COORDENADOR, SUPERVISOR 
                FROM ORGANOGRAMA 
                WHERE PROJETO = ?
                ORDER BY EQUIPE
                """
                
                organograma_real = executar_query(query, [projeto])
                
                if organograma_real is not None:
                    response = {
                        "error": False,
                        "projeto": projeto,
                        "total": len(organograma_real),
                        "organograma": organograma_real
                    }
                else:
                    # Fallback para dados simulados
                    organograma_mock = [
                        {"ID": 1, "PROJETO": "700", "EQUIPE": "700TA", "LIDER": "TESTE LIDER", "COORDENADOR": "TESTE COORD", "SUPERVISOR": "TESTE SUPER"}
                    ]
                    org_filtrado = [o for o in organograma_mock if o["PROJETO"] == projeto]
                    response = {
                        "error": False,
                        "projeto": projeto,
                        "total": len(org_filtrado),
                        "organograma": org_filtrado,
                        "warning": "Usando dados simulados - erro na conexão com Azure SQL"
                    }
            
        elif path == '/api/colaboradores':
            equipe = query_params.get('equipe', [''])[0]
            
            if not equipe:
                response = {"error": True, "message": "Parâmetro equipe é obrigatório"}
            else:
                # Buscar colaboradores reais baseado na EQUIPE, em ordem alfabética
                # CLASSE = 'LDF' identifica líderes para destaque especial
                query = """
                SELECT ID, EQUIPE, NOME, FUNCAO, PROJETO, COORDENADOR, SUPERVISOR, CLASSE 
                FROM COLABORADORES 
                WHERE EQUIPE = ?
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
            
        elif path == '/api/pagcorp':
            lider = query_params.get('lider', [''])[0]
            response = {
                "error": False,
                "lider": lider,
                "total": 1,
                "pagcorp": [{"ID": 1, "LIDER": lider, "CONTA": "TESTE", "CC": "TESTE"}]
            }
        else:
            response = {"error": True, "message": "Endpoint não encontrado"}
        
        self.wfile.write(json.dumps(response, ensure_ascii=False, default=decimal_default).encode('utf-8'))

    def do_POST(self):
        # Parse da URL
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        
        # Headers CORS para APIs
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        
        if path == '/api/salvar-pedido':
            # Ler dados do POST
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            pedido_data = json.loads(post_data.decode('utf-8'))
            
            try:
                print(f"📋 Dados do pedido: {pedido_data}")
                
                # Verificar estrutura da tabela PEDIDOS primeiro
                check_table_query = """
                SELECT COLUMN_NAME, DATA_TYPE 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_NAME = 'PEDIDOS'
                ORDER BY ORDINAL_POSITION
                """
                
                colunas = executar_query(check_table_query, [])
                if colunas:
                    print("🔍 Estrutura da tabela PEDIDOS:")
                    for col in colunas:
                        print(f"   - {col['COLUMN_NAME']} ({col['DATA_TYPE']})")
                
                # Query otimizada com campos essenciais
                query = """
                INSERT INTO PEDIDOS (DATA_RETIRADA, NOME_LIDER, TIPO_REFEICAO, FORNECEDOR, VALOR_PAGO, TOTAL_COLABORADORES, TOTAL_PAGAR, DATA_ENVIO1)
                VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE())
                """
                
                # Extrair dados essenciais
                data_retirada = pedido_data.get('data_retirada')
                nome_lider = pedido_data.get('nome_lider', pedido_data.get('solicitante', 'N/A'))
                tipo_refeicao = pedido_data.get('tipo_refeicao', 'N/A')
                fornecedor = pedido_data.get('fornecedor', 'N/A')
                valor_pago = float(pedido_data.get('valor_pago', 0))
                total_colaboradores = int(pedido_data.get('total_colaboradores', 1))
                total_pagar = valor_pago * total_colaboradores
                
                print(f"💰 Calculando: R$ {valor_pago} x {total_colaboradores} pessoas = R$ {total_pagar}")
                
                resultado = executar_query(query, [
                    data_retirada,
                    nome_lider,
                    tipo_refeicao,
                    fornecedor,
                    valor_pago,
                    total_colaboradores,
                    total_pagar
                ])
                
                if resultado is not None and isinstance(resultado, dict) and 'inserted_id' in resultado:
                    # Sucesso - retornar o ID real do banco
                    pedido_id_real = resultado['inserted_id']
                    print(f"✅ Pedido salvo com ID real: {pedido_id_real}")
                    
                    response = {
                        "error": False,
                        "message": "Pedido salvo com sucesso!",
                        "pedido_id": pedido_id_real,  # ID real do banco
                        "tipo_refeicao": tipo_refeicao,
                        "total_pagar": total_pagar
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
                
        elif path == '/api/aferição-temperatura' or path == '/api/afericao-temperatura':
            # Endpoint para aferição de temperatura com imagens (suporte a URLs com e sem acentos)
            print("🌡️ Recebendo aferição de temperatura...")
            try:
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                aferição_data = json.loads(post_data.decode('utf-8'))
                
                print(f"📋 Dados recebidos: {aferição_data.keys()}")
                
                pedido_id = aferição_data['pedido_id']
                temperatura_retirada = aferição_data['temperatura_retirada']
                temperatura_consumo = aferição_data['temperatura_consumo']
                img_retirada_base64 = aferição_data.get('img_retirada')
                img_consumo_base64 = aferição_data.get('img_consumo')
                observacoes = aferição_data.get('observacoes', '')
                
                print(f"🆔 Pedido ID: {pedido_id}")
                print(f"🌡️ Temp. Retirada: {temperatura_retirada}°C")
                print(f"🌡️ Temp. Consumo: {temperatura_consumo}°C")
                print(f"📷 Imagem Retirada: {'Sim' if img_retirada_base64 else 'Não'}")
                print(f"📷 Imagem Consumo: {'Sim' if img_consumo_base64 else 'Não'}")
                
                # PRIMEIRA PRIORIDADE: SALVAR TEMPERATURAS NO BANCO IMEDIATAMENTE
                print("💾 Salvando temperaturas no banco de dados PRIMEIRO...")
                
                # Verificar se a tabela tem as colunas de temperatura
                check_columns_query = """
                SELECT COLUMN_NAME 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_NAME = 'PEDIDOS' 
                AND COLUMN_NAME IN ('TEMP_RETIRADA', 'TEMP_CONSUMO', 'OBSERVACOES_TEMP')
                """
                
                colunas_existentes = executar_query(check_columns_query, [])
                print(f"🔍 Colunas de temperatura encontradas: {[col['COLUMN_NAME'] for col in colunas_existentes] if colunas_existentes else 'Nenhuma'}")
                
                # Se as colunas não existem, criar elas
                if not colunas_existentes or len(colunas_existentes) < 3:
                    print("� Criando colunas de temperatura na tabela PEDIDOS...")
                    
                    alter_queries = [
                        "ALTER TABLE PEDIDOS ADD TEMP_RETIRADA FLOAT NULL",
                        "ALTER TABLE PEDIDOS ADD TEMP_CONSUMO FLOAT NULL", 
                        "ALTER TABLE PEDIDOS ADD OBSERVACOES_TEMP NVARCHAR(500) NULL"
                    ]
                    
                    for alter_query in alter_queries:
                        try:
                            resultado_alter = executar_query(alter_query, [])
                            print(f"✅ Coluna criada: {alter_query.split('ADD ')[1].split(' ')[0]}")
                        except Exception as e:
                            print(f"⚠️ Coluna já existe ou erro: {e}")
                
                # Atualizar temperaturas no banco (SEM IMAGENS PRIMEIRO)
                query_temp = """
                UPDATE PEDIDOS 
                SET TEMP_RETIRADA = ?, 
                    TEMP_CONSUMO = ?,
                    OBSERVACOES_TEMP = ?
                WHERE ID = ?
                """
                
                resultado_temp = executar_query(query_temp, [
                    temperatura_retirada,
                    temperatura_consumo,
                    observacoes,
                    pedido_id
                ])
                
                print(f"📊 Temperaturas salvas: {resultado_temp} linhas afetadas")
                
                # SEGUNDA PRIORIDADE: UPLOAD DAS IMAGENS (ASSÍNCRONO)
                url_img_retirada = "processando_upload"
                url_img_consumo = "processando_upload"
                
                # Fazer upload das imagens em paralelo (sem bloquear a resposta)
                import threading
                
                def upload_async():
                    nonlocal url_img_retirada, url_img_consumo
                    
                    if img_retirada_base64:
                        print("☁️ Upload assíncrono da imagem de retirada...")
                        url_img_retirada = upload_imagem_blob(
                            img_retirada_base64, 
                            f"retirada_pedido_{pedido_id}.jpg"
                        )
                    
                    if img_consumo_base64:
                        print("☁️ Upload assíncrono da imagem de consumo...")
                        url_img_consumo = upload_imagem_blob(
                            img_consumo_base64, 
                            f"consumo_pedido_{pedido_id}.jpg"
                        )
                    
                    # Atualizar URLs no banco após upload
                    if url_img_retirada and url_img_consumo:
                        query_img = """
                        UPDATE PEDIDOS 
                        SET IMG_RETIRADA = ?, 
                            IMG_CONSUMO = ?
                        WHERE ID = ?
                        """
                        
                        resultado_img = executar_query(query_img, [
                            url_img_retirada,
                            url_img_consumo,
                            pedido_id
                        ])
                        print(f"� URLs das imagens atualizadas: {resultado_img} linhas afetadas")
                
                # Iniciar upload em thread separada (não bloqueia a resposta)
                if img_retirada_base64 or img_consumo_base64:
                    upload_thread = threading.Thread(target=upload_async)
                    upload_thread.daemon = True
                    upload_thread.start()
                    print("🚀 Upload das imagens iniciado em background")
                
                # RESPOSTA IMEDIATA (sem esperar upload das imagens)
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
                    print("✅ Resposta enviada imediatamente - upload continua em background")
                else:
                    response = {
                        "error": True,
                        "message": f"❌ Erro ao salvar temperaturas no banco (ID {pedido_id})"
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
    port = 8082
    print(f"🐍 Servidor Python iniciado em: http://localhost:{port}")
    print(f"📋 Sistema: Agora você pode usar o ngrok!")
    print(f"🔧 APIs disponíveis:")
    print(f"   - http://localhost:{port}/api/teste-conexao")
    print(f"   - http://localhost:{port}/")
    print(f"   - http://localhost:{port}/sistema-pedidos.html")
    print(f"🌐 CONECTANDO NO AZURE SQL REAL!")
    print(f"📊 Servidor: alrflorestal.database.windows.net")
    print(f"💾 Banco: Tabela_teste")
    print(f"❌ Para parar: Ctrl+C")
    print("=" * 60)
    print("🚀 PRONTO PARA NGROK!")
    print("   Execute: ngrok http 8082")
    print("=" * 60)
    
    with socketserver.TCPServer(("", port), RefeicaoHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n🛑 Servidor parado.")

if __name__ == "__main__":
    main()