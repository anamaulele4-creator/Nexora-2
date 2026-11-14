"""Catálogo curado de modelos para o motor local (Ollama).

Tamanhos: soma das camadas do manifesto em registry.ollama.ai (quantização por defeito,
Q4_K_M na maioria), consultados em 2026-11-14 — são aproximados e podem mudar.
RAM: estimativa prática para CPU com contexto de 4096 (modelo + cache KV + margem). Com
contexto maior a RAM sobe. Licenças conforme a página de cada modelo em ollama.com/library.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CatalogModel:
    name: str
    description: str
    size_gb: float
    ram_gb: float
    capabilities: tuple[str, ...]
    license: str
    recommended: bool = False

    def as_dict(self) -> dict:
        d = asdict(self)
        d["capabilities"] = list(self.capabilities)
        return d


CATALOG: tuple[CatalogModel, ...] = (
    CatalogModel("qwen3:0.6b", "Qwen3 minúsculo: testes e respostas muito curtas.", 0.52, 1.5,
                 ("chat", "thinking", "tools"), "Apache-2.0"),
    CatalogModel("gemma3:1b", "Gemma 3 da Google, 1B: rápido em CPU, bom em português simples.", 0.81, 2.0,
                 ("chat",), "Gemma Terms of Use"),
    CatalogModel("deepseek-r1:1.5b", "DeepSeek-R1 destilado 1.5B: raciocínio passo a passo (lento a responder).",
                 1.12, 2.5, ("chat", "thinking"), "MIT"),
    CatalogModel("llama3.2:1b", "Llama 3.2 1B da Meta: leve, multilingue.", 1.32, 2.5,
                 ("chat", "tools"), "Llama 3.2 Community License"),
    CatalogModel("qwen3:1.7b", "Qwen3 1.7B: melhor equilíbrio qualidade/velocidade em 4 GB de RAM.", 1.36, 3.0,
                 ("chat", "thinking", "tools"), "Apache-2.0", recommended=True),
    CatalogModel("llama3.2:3b", "Llama 3.2 3B: mais capaz, ainda cabe em 4–8 GB.", 2.02, 4.0,
                 ("chat", "tools"), "Llama 3.2 Community License"),
    CatalogModel("phi4-mini:latest", "Phi-4 mini (3.8B) da Microsoft: bom em raciocínio e código.", 2.49, 5.0,
                 ("chat", "tools"), "MIT"),
    CatalogModel("qwen3:4b", "Qwen3 4B: boa qualidade geral para servidores de 8 GB.", 2.5, 5.0,
                 ("chat", "thinking", "tools"), "Apache-2.0", recommended=True),
    CatalogModel("gemma3:4b", "Gemma 3 4B: texto e imagem (visão).", 3.35, 6.0,
                 ("chat", "vision"), "Gemma Terms of Use"),
    CatalogModel("mistral:7b", "Mistral 7B: generalista clássico.", 4.37, 8.0,
                 ("chat", "tools"), "Apache-2.0"),
    CatalogModel("qwen2.5-coder:7b", "Qwen2.5 Coder 7B: especializado em programação.", 4.68, 8.0,
                 ("chat", "code", "tools"), "Apache-2.0"),
    CatalogModel("llama3.1:8b", "Llama 3.1 8B: generalista forte, precisa de 8+ GB.", 4.92, 8.5,
                 ("chat", "tools"), "Llama 3.1 Community License"),
    CatalogModel("qwen3:8b", "Qwen3 8B: muito bom em português; servidor de 16 GB recomendado.", 5.23, 9.0,
                 ("chat", "thinking", "tools"), "Apache-2.0", recommended=True),
    CatalogModel("gemma3:12b", "Gemma 3 12B: texto e imagem; precisa de 16 GB.", 8.16, 13.0,
                 ("chat", "vision"), "Gemma Terms of Use"),
    CatalogModel("qwen3:14b", "Qwen3 14B: o maior razoável em CPU com 16 GB (lento).", 9.28, 15.0,
                 ("chat", "thinking", "tools"), "Apache-2.0"),
    CatalogModel("nomic-embed-text:latest", "Embeddings de texto (para a memória/pesquisa numa fase futura).",
                 0.27, 1.0, ("embedding",), "Apache-2.0"),
)

_BY_NAME = {m.name: m for m in CATALOG}


def canonical(name: str) -> str:
    """'llama3.1' -> 'llama3.1:latest' (o Ollama usa a tag latest por defeito)."""
    return name if ":" in name else f"{name}:latest"


def find(name: str) -> CatalogModel | None:
    return _BY_NAME.get(name) or _BY_NAME.get(canonical(name))
