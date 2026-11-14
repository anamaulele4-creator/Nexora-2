# ai_core
**Já existe (Admin e Chat):** clientes HTTP sem SDK para Gemini (`v1beta/models/{model}:generateContent`) e
OpenAI (`chat/completions`) em `providers.py`; cifra/máscara das chaves em `keys.py`; `router.py` escolhe o
primeiro fornecedor ativo com chave pela ordem `AI_PROVIDER_ORDER` e tenta o seguinte se falhar. As chaves
são configuradas em `/admin` (tabela `ai_providers`). Ver `docs/ADMIN-E-CHAT.md`.

**Etapa 3 (por fazer):** classificador de intenção, planner, seletor de ferramentas, gestor de contexto,
classificador de risco, verificador de resultados e recuperação. Só poderá chamar ferramentas do
`connector_hub.registry.ToolRegistry`.
