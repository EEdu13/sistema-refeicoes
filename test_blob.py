#!/usr/bin/env python3
"""
Teste de conectividade com Azure Blob Storage
"""
import os
import requests
from dotenv import load_dotenv
import base64

# Carregar variáveis de ambiente
load_dotenv()

def test_azure_blob_config():
    """Testa se as configurações do Azure Blob estão corretas"""
    
    print("🔧 Testando configuração do Azure Blob Storage...")
    
    # Verificar variáveis de ambiente
    account = os.getenv('AZURE_STORAGE_ACCOUNT')
    container = os.getenv('AZURE_STORAGE_CONTAINER')
    sas_token = os.getenv('AZURE_SAS_TOKEN')
    
    print(f"📦 Account: {account}")
    print(f"📁 Container: {container}")
    print(f"🔑 SAS Token: {sas_token[:50]}...")
    
    if not all([account, container, sas_token]):
        print("❌ Configurações incompletas!")
        return False
    
    # Testar conectividade
    base_url = f"https://{account}.blob.core.windows.net/{container}"
    test_url = f"{base_url}?{sas_token}&restype=container&comp=list"
    
    print(f"🌐 Testando conectividade: {base_url}")
    
    try:
        response = requests.get(test_url, timeout=10)
        print(f"📡 Status: {response.status_code}")
        print(f"📄 Response: {response.text[:200]}...")
        
        if response.status_code == 200:
            print("✅ Conectividade OK!")
            return True
        else:
            print(f"❌ Erro de conectividade: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Erro de conexão: {e}")
        return False

def test_upload_small_file():
    """Testa upload de um arquivo pequeno"""
    
    print("\n🔧 Testando upload de arquivo...")
    
    account = os.getenv('AZURE_STORAGE_ACCOUNT')
    container = os.getenv('AZURE_STORAGE_CONTAINER')
    sas_token = os.getenv('AZURE_SAS_TOKEN')
    
    # Criar uma imagem base64 pequena (1x1 pixel PNG)
    tiny_png_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChAI9jINJtQAAAABJRU5ErkJggg=="
    
    filename = "test_image.png"
    blob_url = f"https://{account}.blob.core.windows.net/{container}/{filename}"
    
    headers = {
        'x-ms-blob-type': 'BlockBlob',
        'Content-Type': 'image/png'
    }
    
    # Converter base64 para bytes
    image_data = base64.b64decode(tiny_png_base64)
    
    upload_url = f"{blob_url}?{sas_token}"
    
    print(f"📤 Fazendo upload para: {blob_url}")
    
    try:
        response = requests.put(upload_url, data=image_data, headers=headers, timeout=30)
        
        print(f"📡 Status do upload: {response.status_code}")
        print(f"📄 Headers: {dict(response.headers)}")
        print(f"📄 Response: {response.text}")
        
        if response.status_code in [200, 201]:
            print("✅ Upload realizado com sucesso!")
            print(f"🔗 URL da imagem: {blob_url}")
            return blob_url
        else:
            print(f"❌ Erro no upload: {response.status_code}")
            return None
            
    except Exception as e:
        print(f"❌ Erro durante upload: {e}")
        return None

if __name__ == "__main__":
    print("🚀 Iniciando testes do Azure Blob Storage...")
    
    # Teste 1: Configuração
    config_ok = test_azure_blob_config()
    
    if config_ok:
        # Teste 2: Upload
        test_upload_small_file()
    
    print("\n🏁 Testes finalizados!")
