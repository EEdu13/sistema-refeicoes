-- Script para corrigir o fuso horário da coluna Criado
-- Problema: DEFAULT (getdate()) está usando horário do servidor (UTC+3)
-- Solução: Alterar para horário de Brasília (UTC-3)

USE [Tabela_teste]
GO

-- 1. Remover o constraint DEFAULT atual da coluna Criado
DECLARE @constraint_name NVARCHAR(128)
SELECT @constraint_name = d.name
FROM sys.default_constraints d
INNER JOIN sys.columns c ON d.parent_column_id = c.column_id
INNER JOIN sys.tables t ON c.object_id = t.object_id
WHERE t.name = 'PEDIDOS' AND c.name = 'Criado'

IF @constraint_name IS NOT NULL
BEGIN
    EXEC('ALTER TABLE [dbo].[PEDIDOS] DROP CONSTRAINT [' + @constraint_name + ']')
    PRINT 'Constraint DEFAULT removido: ' + @constraint_name
END

-- 2. Adicionar novo DEFAULT com horário de Brasília
-- Usando DATEADD para subtrair 6 horas do UTC (para compensar o fuso do servidor)
ALTER TABLE [dbo].[PEDIDOS] 
ADD CONSTRAINT [DF_PEDIDOS_Criado_Brasilia] 
DEFAULT (DATEADD(hour, -6, GETUTCDATE())) FOR [Criado]

PRINT 'Novo DEFAULT aplicado: DATEADD(hour, -6, GETUTCDATE())'
PRINT 'Coluna Criado agora registrará horário de Brasília'

-- 3. Verificar se foi aplicado corretamente
SELECT 
    c.name AS coluna,
    d.name AS constraint_name,
    d.definition AS default_value
FROM sys.default_constraints d
INNER JOIN sys.columns c ON d.parent_column_id = c.column_id
INNER JOIN sys.tables t ON c.object_id = t.object_id
WHERE t.name = 'PEDIDOS' AND c.name = 'Criado'

PRINT 'Correção de fuso horário concluída!'
