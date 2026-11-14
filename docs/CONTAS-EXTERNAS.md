# Contas e chaves externas — o que é preciso e quando

Nenhuma conta externa é necessária para a Etapa 1. Nada foi criado nem configurado com chaves reais.
Cada uma destas só será usada depois de autorização explícita da Ana.

| Serviço | Para quê | Etapa | Variáveis | Notas |
|---|---|---|---|---|
| **Browserbase** | Chromium remoto isolado por tarefa | 2 | `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` | Na Etapa 2 dá para começar com Playwright local; Browserbase para produção/escala. |
| **Armazenamento S3-compatível** (AWS S3, Cloudflare R2, Supabase Storage…) | screenshots, downloads, uploads | 2 (screenshots) / 5 | `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Em desenvolvimento pode usar-se MinIO local. |
| **OpenAI** | modelo com tool calling (AI Core) | 3 | `OPENAI_API_KEY`, `OPENAI_MODEL` | Precisa de faturação ativa na conta OpenAI. |
| **Google Cloud (OAuth)** | conectores Gmail e Google Drive | 4 | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (ver docs/CONECTORES.md) | Ecrã de consentimento OAuth; escopos Gmail sensíveis exigem verificação da Google para uso público. |
| **GitHub OAuth App** | conector GitHub | 4 | `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | Ver docs/CONECTORES.md. |
| **Shopify (app personalizada da loja)** | conector Shopify | 4 | nenhuma no servidor | Cada utilizador cola o domínio da loja e o token de Admin API em /conectores. |
| **Temporal** (Temporal Cloud ou servidor próprio) | execução durável, retentativas, agendamentos | 5 | `TEMPORAL_ADDRESS` (+ certificados se Cloud) | Em desenvolvimento: `temporal server start-dev` local, sem conta. |
| **SMTP / serviço de email** (Resend, Postmark, SES…) | emails reais de verificação e recuperação | antes de produção (6/7) | `EMAIL_BACKEND=smtp`, `SMTP_*` | Desnecessário com `AUTH_PROVIDER=supabase` (o Supabase envia), mas recomendado domínio próprio. |
| **Supabase** | PostgreSQL gerido + Auth + RLS em produção | 6/7 (pode antecipar-se) | `DATABASE_URL`, `AUTH_PROVIDER=supabase`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_JWT_SECRET` | As migrações já são compatíveis (papel `authenticated`). |
| **Vercel** | alojar o frontend | 7 | `API_URL` (no build) | Só o frontend; API e workers ficam em infraestrutura própria. |
| **Alojamento de contentores** (Fly.io, Render, Railway, VPS…) | API, workers, Redis | 7 | — | Escolha a decidir com a Ana. |
| **Coletor OpenTelemetry** (Grafana Cloud, Honeycomb…) | traces e métricas | 6/7 | `OTEL_EXPORTER_OTLP_ENDPOINT` | Opcional. |

Segredos gerados localmente (não são contas): `JWT_SECRET` e `CREDENTIALS_ENCRYPTION_KEY` — os
comandos para os gerar estão no `.env.example`.
