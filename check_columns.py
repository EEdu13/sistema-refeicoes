#!/usr/bin/env python3
import pyodbc

# Configuração do Azure SQL
AZURE_CONFIG = {
    'server': 'alrflorestal.database.windows.net',
    'database': 'Tabela_teste',
    'username': 'sqladmin',
    'password': 'SenhaForte123!',
    'driver': '{ODBC Driver 17 for SQL Server}'
}

def conectar_azure_sql():
    try:
        connection_string = f"DRIVER={AZURE_CONFIG['driver']};SERVER={AZURE_CONFIG['server']};DATABASE={AZURE_CONFIG['database']};UID={AZURE_CONFIG['username']};PWD={AZURE_CONFIG['password']}"
        conn = pyodbc.connect(connection_string)
        return conn
    except Exception as e:
        print(f'❌ Erro ao conectar no Azure SQL: {e}')
        return None

# Verificar estrutura da tabela PEDIDOS
conn = conectar_azure_sql()
if conn:
    cursor = conn.cursor()
    query = """
    SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, COLUMN_DEFAULT
    FROM INFORMATION_SCHEMA.COLUMNS 
    WHERE TABLE_NAME = 'PEDIDOS'
    ORDER BY ORDINAL_POSITION
    """
    cursor.execute(query)
    
    print('🔍 ESTRUTURA DA TABELA PEDIDOS:')
    print('=' * 80)
    print(f"{'COLUNA':<25} {'TIPO':<15} {'NULO':<10} {'TAMANHO':<10} {'PADRÃO'}")
    print('=' * 80)
    for row in cursor.fetchall():
        col_name = row[0]
        data_type = row[1]
        is_nullable = row[2]
        max_length = row[3] if row[3] else ''
        default_val = row[4] if row[4] else ''
        print(f'{col_name:<25} {data_type:<15} {is_nullable:<10} {str(max_length):<10} {default_val}')
    
    conn.close()
    print('=' * 80)
else:
    print('❌ Não foi possível conectar ao banco')
