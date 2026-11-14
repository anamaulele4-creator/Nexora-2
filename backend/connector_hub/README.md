# connector_hub
- `registry.py`: registo central de ferramentas (a única fonte do que o modelo pode chamar) e `CONNECTION_SPECS`, como cada conector liga a conta (OAuth GitHub/Google ou API key no Shopify).
- `oauth.py`: fluxo OAuth 2.0 (state de uso único no Redis, PKCE na Google), identidade da conta, renovação e revogação; verificação do Shopify.
- `vault.py`: ligações (`connected_apps`) e tokens cifrados (`encrypted_credentials`, AES-256-GCM com o `CredentialCipher`).
Ver docs/CONECTORES.md.
