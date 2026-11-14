-- Catálogo inicial de conectores. TODOS ficam com status 'planned':
-- nenhum conector é apresentado como funcional antes de ter testes (Etapa 4).
insert into public.connector_definitions (key, name, description, category, auth_type, status, planned_stage, scopes)
values
  ('browser_generic', 'Browser (generic)', 'Controlled remote browser for any website (Playwright/Browserbase).', 'browser', 'browser', 'planned', 2, '{}'),
  ('gmail',           'Gmail',             'Read, search and draft emails. Sending always requires approval.',  'communication', 'oauth2', 'planned', 4,
     '{https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.compose}'),
  ('google_drive',    'Google Drive',      'List, read and upload files.',                                       'storage', 'oauth2', 'planned', 4,
     '{https://www.googleapis.com/auth/drive.file}'),
  ('github',          'GitHub',            'Repositories, issues and pull requests.',                             'developer', 'oauth2', 'planned', 4, '{repo,read:user}'),
  ('shopify',         'Shopify',           'Products, orders and inventory of a store (shop domain + Admin API token).', 'commerce', 'api_key', 'planned', 4,
     '{read_products,read_orders}'),
  ('custom_api',      'Custom API',        'Any HTTP API with the user''s own key.',                             'developer', 'api_key', 'planned', 4, '{}')
on conflict (key) do update
  set name = excluded.name, description = excluded.description, category = excluded.category,
      auth_type = excluded.auth_type, planned_stage = excluded.planned_stage, scopes = excluded.scopes;
