# connectors/base (Etapa 1, testado)
- `tools.py` — contrato `ToolSpec`: nome, parâmetros, resposta, permissões, risco, aprovação, limite, repetição, timeout, verificação; exportação para o formato de ferramentas da OpenAI.
- `connector.py` — `BaseConnector` abstrato com connect/disconnect/execute/health_check; `execute` aplica permissões, aprovação humana e validação de esquemas antes e depois de cada chamada.
