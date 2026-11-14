A tabela da memória (`dario_memories`, `vector(768)` com índice HNSW) é criada pela própria DÁRIO API
no arranque (`app/memory.py`), por isso não há migrações manuais nesta fase.
Se mudares `DARIO_EMBED_MODEL` para um modelo com outra dimensão, ajusta `DARIO_EMBED_DIM` e recria a tabela.
