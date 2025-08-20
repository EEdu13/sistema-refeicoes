#!/bin/bash
echo "🔧 Instalando dependências..."

# Instalar dependências Python
pip install --no-cache-dir pyodbc requests Flask Flask-CORS pytz python-dotenv

echo "📦 Instalando drivers ODBC..."
# Instalar drivers ODBC
curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add -
curl https://packages.microsoft.com/config/ubuntu/20.04/prod.list > /etc/apt/sources.list.d/mssql-release.list
apt-get update
ACCEPT_EULA=Y apt-get install -y msodbcsql18
apt-get install -y unixodbc-dev

echo "🚀 Iniciando servidor..."
python server.py
