#!/usr/bin/env python3
import requests
import json

# Dados de teste baseados no log do frontend
test_data = {
    "data_retirada": "2025-08-29",
    "nome_lider": "JEFFERSON APARECIDO ALVES DA SILVA",
    "tipo_refeicao": "CAFÉ DA MANHÃ",
    "fornecedor": "PANIFICADORA VITOR - MARIZA MARIA DO NASCIMENTO",
    "valor_pago": 10,
    "colaboradores_nomes": "ANTONIO PEREIRA DE SOUZA FILHO - OPERADOR MAQ. FLORESTAIS I, JEFFERSON APARECIDO ALVES DA SILVA - LIDER DE EQUIPE (Líder), LUCIANO LIMA BATISTA - OPERADOR MAQ. FLORESTAIS I",
    "total_colaboradores": 3,
    "a_contratar": 0,
    "responsavel_cartao": "JEFFERSON APARECIDO ALVES DA SILVA",
    "pagcorp_numero": "19844612",
    "hospedado_real": "NÃO",
    "nome_hotel_real": "",
    "valor_diaria_real": 0,
    "aferiu_temperatura": "NAO_NECESSITA",
    "projeto": "702",
    "equipe": "702AA",
    "coordenador": "TONIEL RODRIGUES",
    "supervisor": "MAURICIO SERPE",
    "nome_lider_organograma": "JEFFERSON APARECIDO ALVES DA SILVA",
    "fazenda_digitada": "EDEDE",
    "fechamento_fornecedor": "SIM",
    "cidade_prestacao_servico": "Araucária",
    "observacoes": ""
}

print("🔥 TESTE: Enviando dados para servidor local...")
print(f"📊 Dados: {json.dumps(test_data, indent=2, ensure_ascii=False)}")

try:
    response = requests.post(
        "http://localhost:8083/api/salvar-pedido",
        json=test_data,
        headers={"Content-Type": "application/json"},
        timeout=30
    )
    
    print(f"📡 Status: {response.status_code}")
    print(f"📥 Resposta: {response.text}")
    
except Exception as e:
    print(f"❌ Erro: {e}")
