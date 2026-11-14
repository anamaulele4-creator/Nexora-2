# connectors/github

`GitHubConnector` (`connector.py`) implementa `connectors.base.BaseConnector` sobre a API REST do GitHub,
com o token OAuth do próprio utilizador (uma instância por utilizador e pedido).

- Leitura (risco `low`, correm logo): `get_me`, `list_repos`, `get_repo`, `list_issues`, `get_issue`,
  `list_pull_requests`, `read_file`, `search_code`.
- Escrita (risco `high`, `requires_approval`): `create_issue`, `comment_issue` — `execute` recusa-as sem
  `approval_id`; no chat só correm depois de o utilizador carregar em **Confirmar**.

Ligação ao chat: `backend/connector_hub/agent_tools.py`. Testes: `tests/backend/test_github_connector.py`
(httpx.MockTransport) e `tests/backend/test_agent_tools.py`. Ver `docs/CONECTORES.md`.
