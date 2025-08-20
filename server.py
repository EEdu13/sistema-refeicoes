#!/usr/bin/env python3
import http.server
import socketserver
import json
import urllib.parse
from datetime import datetime
import pytz
import pyodbc
import decimal
import os
from dotenv import load_dotenv

# Carregar variáveis de ambiente
load_dotenv()

# Função para serializar Decimal e datetime em JSON
def decimal_default(obj):
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    elif isinstance(obj, datetime):
        # Converter para horário de Brasília se necessário
        if obj.tzinfo is None:
            # Se datetime não tem timezone, assumir UTC e converter para Brasília
            utc_tz = pytz.UTC
            brasilia_tz = pytz.timezone('America/Sao_Paulo')
            obj = utc_tz.localize(obj).astimezone(brasilia_tz)
        return obj.isoformat()
    elif hasattr(obj, '__str__'):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

# Configuração do Azure SQL
AZURE_CONFIG = {
    'server': os.getenv('AZURE_SQL_SERVER'),
    'database': os.getenv('AZURE_SQL_DATABASE'),
    'username': os.getenv('AZURE_SQL_USERNAME'),
    'password': os.getenv('AZURE_SQL_PASSWORD'),
    'driver': '{ODBC Driver 17 for SQL Server}'
}

# Configuração do Azure Blob Storage
AZURE_BLOB_CONFIG = {
    'account_name': os.getenv('AZURE_BLOB_ACCOUNT'),
    'container_name': os.getenv('AZURE_BLOB_CONTAINER'),
    'sas_token': os.getenv('AZURE_BLOB_SAS_TOKEN')
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
        # Gerar timestamp brasileiro para o nome do arquivo
        brasilia_tz = pytz.timezone('America/Sao_Paulo')
        timestamp = datetime.now(brasilia_tz).strftime('%Y%m%d_%H%M%S')
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
        if path == '/api/config':
            # Endpoint para fornecer configurações do frontend
            response = {
                "EMAILJS_PUBLIC_KEY": os.getenv('EMAILJS_PUBLIC_KEY', ''),
                "EMAILJS_SERVICE_ID": os.getenv('EMAILJS_SERVICE_ID', ''),
                "EMAILJS_TEMPLATE_ID": os.getenv('EMAILJS_TEMPLATE_ID', '')
            }
            
        elif path == '/api/teste-conexao':
            response = {
                "success": True,
                "message": "Servidor Python funcionando!",
                "timestamp": datetime.now(pytz.timezone('America/Sao_Paulo')).isoformat()
            }
            
        elif path == '/api/fornecedores':
            projeto = query_params.get('projeto', [''])[0]
            
            if not projeto:
                response = {"error": True, "message": "Parâmetro projeto é obrigatório"}
            else:
                # Buscar fornecedores reais do Azure SQL
                query = """
                SELECT ID, PROJETO, LOCAL, FORNECEDOR, TIPO_FORN, VALOR, STATUS,
                       ISNULL(FECHAMENTO, '') as FECHAMENTO,
                       ISNULL(LOCAL, '') as FAZENDA
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
            print(f"🔍 Buscando PAGCORP para líder: {lider}")
            
            try:
                # Buscar dados reais na tabela PAGCORP_CAD
                query = "SELECT ID, CONTA, CC, LIDER FROM PAGCORP_CAD WHERE LIDER = ?"
                resultado = executar_query(query, [lider])
                
                if resultado and len(resultado) > 0:
                    print(f"✅ PAGCORP encontrado: {resultado}")
                    response = {
                        "error": False,
                        "lider": lider,
                        "total": len(resultado),
                        "pagcorp": resultado
                    }
                else:
                    print(f"❌ Nenhum PAGCORP encontrado para: {lider}")
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
            print("🔍 Buscando pedidos MARMITEX pendentes de temperatura no banco...")
            
            # 🎯 OBTER PARÂMETRO DE EQUIPE PARA FILTRAR
            parsed_url = urllib.parse.urlparse(self.path)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            equipe_param = query_params.get('equipe', [None])[0]
            
            print(f"👥 Filtrando por equipe: {equipe_param}")
            
            # Usar LIDER como critério de filtro se fornecido (LIDER contém o nome da equipe)
            if equipe_param and equipe_param != 'SEM_EQUIPE':
                query = """
                SELECT ID, DATA_RETIRADA, NOME_LIDER, TIPO_REFEICAO, FORNECEDOR, 
                       TOTAL_COLABORADORES, TOTAL_PAGAR, DATA_ENVIO1, LIDER,
                       TEMP_RETIRADA, TEMP_CONSUMO
                FROM PEDIDOS 
                WHERE (TIPO_REFEICAO LIKE '%MARMITEX%' OR TIPO_REFEICAO LIKE '%MARMITA%')
                  AND (TEMP_RETIRADA IS NULL OR TEMP_CONSUMO IS NULL)
                  AND LIDER = ?
                ORDER BY DATA_ENVIO1 DESC
                """
                query_params_db = [equipe_param]
                print(f"🔍 Query com filtro de equipe (campo LIDER): {equipe_param}")
            else:
                # Query sem filtro de equipe (comportamento original)
                query = """
                SELECT ID, DATA_RETIRADA, NOME_LIDER, TIPO_REFEICAO, FORNECEDOR, 
                       TOTAL_COLABORADORES, TOTAL_PAGAR, DATA_ENVIO1, LIDER,
                       TEMP_RETIRADA, TEMP_CONSUMO
                FROM PEDIDOS 
                WHERE (TIPO_REFEICAO LIKE '%MARMITEX%' OR TIPO_REFEICAO LIKE '%MARMITA%')
                  AND (TEMP_RETIRADA IS NULL OR TEMP_CONSUMO IS NULL)
                ORDER BY DATA_ENVIO1 DESC
                """
                query_params_db = []
                print("🔍 Query sem filtro de equipe (todas as equipes)")
            
            try:
                pedidos_pendentes = executar_query(query, query_params_db)
                print(f"📊 Query executada. Resultado: {type(pedidos_pendentes)}")
                
                if pedidos_pendentes is not None:
                    filtro_msg = f" para equipe '{equipe_param}'" if equipe_param and equipe_param != 'SEM_EQUIPE' else " (todas as equipes)"
                    print(f"✅ Encontrados {len(pedidos_pendentes)} pedidos MARMITEX pendentes{filtro_msg}")
                    
                    # Formatar dados para o frontend
                    pendencias_formatadas = []
                    for pedido in pedidos_pendentes:
                        equipe_pedido = pedido.get('LIDER', 'N/A')  # Usar LIDER que contém o nome da equipe
                        print(f"   📋 Pedido ID {pedido['ID']}: {pedido['TIPO_REFEICAO']} - {pedido.get('DATA_RETIRADA', 'N/A')} - Equipe: {equipe_pedido}")
                        
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
                # Obter parâmetros da query string
                query_components = urllib.parse.parse_qs(parsed_path.query)
                equipe_param = query_components.get('equipe', [None])[0]
                
                print(f"🔍 Buscando último pedido da equipe: {equipe_param}")
                
                if not equipe_param or equipe_param == 'SEM_EQUIPE':
                    response = {
                        "error": True,
                        "message": "Parâmetro 'equipe' é obrigatório"
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
                    WHERE LIDER = ? 
                      AND CAST(DATA_RETIRADA AS DATE) = ?
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
                
                # Query COMPLETA com todos os campos disponíveis + APROVADO_POR + FECHAMENTO
                query = """
                INSERT INTO PEDIDOS (
                    DATA_RETIRADA, DATA_ENVIO1, PROJETO, COORDENADOR, SUPERVISOR, 
                    LIDER, NOME_LIDER, FAZENDA, TIPO_REFEICAO, CIDADE_PRESTACAO_DO_SERVICO,
                    FORNECEDOR, VALOR_PAGO, COLABORADORES, TOTAL_COLABORADORES, A_CONTRATAR,
                    RESPONSAVEL_PELO_CARTAO, PAGCORP, HOSPEDADO, NOME_DO_HOTEL, VALOR_DIARIA,
                    TOTAL_PAGAR, APROVADO_POR, OBSERVACOES, FECHAMENTO
                ) VALUES (?, GETDATE(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                
                # Extrair TODOS os dados do pedido com MAPEAMENTO CORRETO
                data_retirada = pedido_data.get('data_retirada')
                projeto = pedido_data.get('projeto', '')
                coordenador = pedido_data.get('coordenador', '')
                supervisor = pedido_data.get('supervisor', '')
                
                # LIDER = EQUIPE digitada (ex: 700AA)
                lider = pedido_data.get('equipe', '')  # Equipe digitada
                
                # NOME_LIDER = Nome do líder da equipe (do organograma)
                nome_lider = pedido_data.get('nome_lider_organograma', pedido_data.get('solicitante', 'N/A'))
                
                # FAZENDA = APENAS o que o usuário digitou no campo
                fazenda = pedido_data.get('fazenda_digitada', '').strip()
                
                tipo_refeicao = pedido_data.get('tipo_refeicao', 'N/A')
                cidade = pedido_data.get('cidade_prestacao_servico', '')
                fornecedor = pedido_data.get('fornecedor', 'N/A')
                valor_pago = float(pedido_data.get('valor_pago', 0))
                
                # COLABORADORES = Limpar ícones, manter só texto
                colaboradores_nomes = pedido_data.get('colaboradores_nomes_limpos', '')
                
                # Limpeza adicional de caracteres Unicode problemáticos
                import re
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
                
                # FECHAMENTO = Da tabela de fornecedores
                fechamento = pedido_data.get('fechamento_fornecedor', '')
                
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
                print(f"   FECHAMENTO: {fechamento}")
                
                resultado = executar_query(query, [
                    data_retirada, projeto, coordenador, supervisor, lider, nome_lider,
                    fazenda, tipo_refeicao, cidade, fornecedor, valor_pago, 
                    colaboradores_nomes, total_colaboradores, a_contratar,
                    responsavel_cartao, pagcorp, hospedado, nome_hotel, valor_diaria,
                    total_pagar, aprovado_por, observacoes, fechamento
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
                
        elif path == '/upload-blob':
            # Endpoint para upload de imagens do problema para Azure Blob
            print("📸 Recebendo upload de imagem para blob...")
            try:
                # Ler o conteúdo como multipart/form-data manualmente
                import tempfile
                
                # Ler todos os dados do request
                content_length = int(self.headers.get('Content-Length', 0))
                raw_data = self.rfile.read(content_length)
                
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
                        import base64
                        file_base64 = base64.b64encode(file_data).decode('utf-8')
                        
                        # Fazer upload para blob usando função existente
                        blob_url = upload_imagem_blob(file_base64, filename)
                        
                        if blob_url and not blob_url.startswith('local_'):
                            response = {
                                "error": False,
                                "message": "Upload realizado com sucesso",
                                "url": blob_url,
                                "filename": filename
                            }
                            print(f"✅ Upload concluído: {blob_url}")
                        else:
                            response = {
                                "error": True,
                                "message": "Erro no upload para Azure Blob",
                                "fallback_url": blob_url
                            }
                            print(f"❌ Erro no upload, usando fallback: {blob_url}")
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
            print("🌡️ Recebendo aferição de temperatura...")
            try:
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                aferição_data = json.loads(post_data.decode('utf-8'))
                
                print(f"📋 Dados recebidos: {aferição_data.keys()}")
                
                pedido_id = aferição_data['pedido_id']
                temperatura_retirada = aferição_data['temperatura_retirada']
                temperatura_consumo = aferição_data['temperatura_consumo']
                hora_retirada = aferição_data.get('hora_retirada')  # NOVO: Hora da retirada
                hora_consumo = aferição_data.get('hora_consumo')    # NOVO: Hora do consumo
                img_retirada_base64 = aferição_data.get('img_retirada')
                img_consumo_base64 = aferição_data.get('img_consumo')
                observacoes = aferição_data.get('observacoes', '')
                
                print(f"🆔 Pedido ID: {pedido_id}")
                print(f"🌡️ Temp. Retirada: {temperatura_retirada}°C")
                print(f"🌡️ Temp. Consumo: {temperatura_consumo}°C")
                print(f"🕐 Hora Retirada: {hora_retirada}")      # NOVO LOG
                print(f"🕐 Hora Consumo: {hora_consumo}")        # NOVO LOG
                print(f"🔍 Tipo hora_retirada: {type(hora_retirada)}")  # DEBUG
                print(f"🔍 Tipo hora_consumo: {type(hora_consumo)}")    # DEBUG
                
                # Log detalhado das imagens
                if img_retirada_base64:
                    print(f"📷 Imagem Retirada: SIM ({len(img_retirada_base64)} chars)")
                    if img_retirada_base64.startswith('data:'):
                        print(f"   ✅ Formato correto: {img_retirada_base64[:50]}...")
                    else:
                        print(f"   ⚠️ Sem prefixo data: {img_retirada_base64[:50]}...")
                else:
                    print(f"📷 Imagem Retirada: NÃO ENVIADA")
                
                if img_consumo_base64:
                    print(f"📷 Imagem Consumo: SIM ({len(img_consumo_base64)} chars)")
                    if img_consumo_base64.startswith('data:'):
                        print(f"   ✅ Formato correto: {img_consumo_base64[:50]}...")
                    else:
                        print(f"   ⚠️ Sem prefixo data: {img_consumo_base64[:50]}...")
                else:
                    print(f"📷 Imagem Consumo: NÃO ENVIADA")
                
                # PRIMEIRA PRIORIDADE: SALVAR TEMPERATURAS NO BANCO IMEDIATAMENTE
                print("💾 Salvando temperaturas no banco de dados PRIMEIRO...")
                
                # Verificar se a tabela tem as colunas de temperatura
                check_columns_query = """
                SELECT COLUMN_NAME 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_NAME = 'PEDIDOS' 
                AND COLUMN_NAME IN ('TEMPERATURA_RETIRADA', 'TEMPERATURA_CONSUMO', 'OBSERVACOES_TEMP')
                """
                
                colunas_existentes = executar_query(check_columns_query, [])
                print(f"🔍 Colunas de temperatura encontradas: {[col['COLUMN_NAME'] for col in colunas_existentes] if colunas_existentes else 'Nenhuma'}")
                
                # Se as colunas não existem, criar elas
                if not colunas_existentes or len(colunas_existentes) < 3:
                    print("� Criando colunas de temperatura na tabela PEDIDOS...")
                    
                    alter_queries = [
                        "ALTER TABLE PEDIDOS ADD TEMPERATURA_RETIRADA FLOAT NULL",
                        "ALTER TABLE PEDIDOS ADD TEMPERATURA_CONSUMO FLOAT NULL", 
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
                SET TEMPERATURA_RETIRADA = ?, 
                    TEMPERATURA_CONSUMO = ?,
                    HORA_RETIRADA = ?,
                    HORA_CONSUMO = ?,
                    AFERIU_TEMPERATURA = 'SIM',
                    OBSERVACOES_TEMP = ?
                WHERE ID = ?
                """
                
                # Converter horas para formato datetime2 do SQL Server
                from datetime import datetime, date
                hora_retirada_dt = None
                hora_consumo_dt = None
                
                # BUSCAR A DATA DE RETIRADA DO PEDIDO NO BANCO
                query_data = "SELECT DATA_RETIRADA FROM PEDIDOS WHERE ID = ?"
                resultado_data = executar_query(query_data, [pedido_id])
                
                if resultado_data and len(resultado_data) > 0:
                    data_retirada_pedido = resultado_data[0]['DATA_RETIRADA']
                    print(f"📅 Data do pedido encontrada: {data_retirada_pedido}")
                    
                    # Se a data_retirada_pedido for datetime, extrair apenas a data
                    if hasattr(data_retirada_pedido, 'date'):
                        data_retirada_pedido = data_retirada_pedido.date()
                        print(f"📅 Data extraída: {data_retirada_pedido}")
                else:
                    # Fallback para data atual se não encontrar
                    data_retirada_pedido = date.today()
                    print(f"⚠️ Data do pedido não encontrada, usando data atual: {data_retirada_pedido}")
                
                if hora_retirada:
                    try:
                        # Combinar data do pedido + hora informada
                        hora_obj = datetime.strptime(hora_retirada, '%H:%M').time()
                        hora_retirada_dt = datetime.combine(data_retirada_pedido, hora_obj)
                        print(f"🕐 Hora Retirada convertida: {hora_retirada_dt}")
                    except Exception as e:
                        print(f"❌ Erro ao converter hora_retirada: {e}")
                
                if hora_consumo:
                    try:
                        # Combinar data do pedido + hora informada
                        hora_obj = datetime.strptime(hora_consumo, '%H:%M').time()
                        hora_consumo_dt = datetime.combine(data_retirada_pedido, hora_obj)
                        print(f"🕐 Hora Consumo convertida: {hora_consumo_dt}")
                    except Exception as e:
                        print(f"❌ Erro ao converter hora_consumo: {e}")
                
                resultado_temp = executar_query(query_temp, [
                    temperatura_retirada,
                    temperatura_consumo,
                    hora_retirada_dt,
                    hora_consumo_dt,
                    observacoes,
                    pedido_id
                ])
                
                print(f"📊 Temperaturas E HORAS salvas: {resultado_temp} linhas afetadas")
                
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
    import os
    
    # Railway fornece a porta via variável de ambiente PORT
    port = int(os.environ.get('PORT', 8082))
    
    print(f"🐍 Servidor Python iniciado em: http://localhost:{port}")
    print(f"📋 Sistema: Railway Deploy Ready!")
    print(f"🔧 APIs disponíveis:")
    print(f"   - http://localhost:{port}/api/teste-conexao")
    print(f"   - http://localhost:{port}/")
    print(f"   - http://localhost:{port}/sistema-pedidos.html")
    print(f"🌐 CONECTANDO NO AZURE SQL REAL!")
    print(f"📊 Servidor: alrflorestal.database.windows.net")
    print(f"💾 Banco: Tabela_teste")
    print(f"❌ Para parar: Ctrl+C")
    print("=" * 60)
    print("� RAILWAY READY!")
    print("   Deploy: git push origin main")
    print("=" * 60)
    
    with socketserver.TCPServer(("", port), RefeicaoHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n🛑 Servidor parado.")

if __name__ == "__main__":
    main()