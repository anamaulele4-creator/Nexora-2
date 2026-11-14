"""Model Manager: escolhe o modelo pela tarefa e pelo que está instalado, e recomenda modelos pelo hardware.

Pedidos com ``model`` igual a ``dario`` / ``dario:auto`` são encaminhados automaticamente;
``dario:code``, ``dario:reasoning`` e ``dario:general`` forçam o tipo de tarefa.
Qualquer outro nome é passado tal como está ao Ollama.
"""

from __future__ import annotations

import re

AUTO_NAMES = {"dario", "dario:auto", "dario:latest"}
TASK_NAMES = {"dario:code": "code", "dario:reasoning": "reasoning", "dario:general": "general"}

_CODE = re.compile(
    r"```|\b(c[oó]digo|code|python|javascript|typescript|sql|bash|fun[cç][aã]o|function|bug|erro de|"
    r"stack ?trace|compil|refactor|api|endpoint|regex|docker|git)\b",
    re.IGNORECASE,
)
_REASONING = re.compile(
    r"\b(planeia|plano|plan|passo a passo|step by step|raciocina|prova|demonstra|analisa|compara|"
    r"estrat[eé]gia|porqu[eê]|why)\b",
    re.IGNORECASE,
)

# Tamanho aproximado em disco/VRAM (quantização q4 por defeito do Ollama), em GB
CATALOG: list[dict] = [
    {"name": "qwen3:4b", "size_gb": 2.6, "tasks": ["general"]},
    {"name": "qwen3:8b", "size_gb": 5.2, "tasks": ["general", "reasoning", "code"]},
    {"name": "qwen3:14b", "size_gb": 9.3, "tasks": ["reasoning", "general"]},
    {"name": "qwen2.5-coder:7b", "size_gb": 4.7, "tasks": ["code"]},
    {"name": "qwen2.5-coder:14b", "size_gb": 9.0, "tasks": ["code"]},
    {"name": "llama3.1:8b", "size_gb": 4.9, "tasks": ["general"]},
    {"name": "mistral:7b", "size_gb": 4.4, "tasks": ["general"]},
    {"name": "qwen3:32b", "size_gb": 20.0, "tasks": ["reasoning", "general"]},
    {"name": "nomic-embed-text", "size_gb": 0.3, "tasks": ["embedding"]},
]


def classify(messages: list[dict]) -> str:
    """Tipo de tarefa a partir da última mensagem do utilizador."""
    text = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            content = m.get("content")
            text = content if isinstance(content, str) else " ".join(
                p.get("text", "") for p in content or [] if isinstance(p, dict))
            break
    if _CODE.search(text):
        return "code"
    if _REASONING.search(text) and len(text) > 200:
        return "reasoning"
    return "general"


def _base(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


def pick(task: str, installed: list[str], preferences: dict[str, list[str]]) -> str | None:
    have = {_base(n) for n in installed}
    for candidate in preferences.get(task, []) + preferences.get("general", []):
        if _base(candidate) in have:
            return candidate
    chat = [n for n in installed if "embed" not in n]
    return chat[0] if chat else None


def resolve(requested: str | None, messages: list[dict], installed: list[str],
            preferences: dict[str, list[str]]) -> tuple[str | None, str | None]:
    """Devolve (modelo a usar, tarefa) — tarefa é None quando o modelo foi pedido explicitamente."""
    name = (requested or "").strip().lower()
    if name in AUTO_NAMES:
        task = classify(messages)
    elif name in TASK_NAMES:
        task = TASK_NAMES[name]
    else:
        return requested, None
    return pick(task, installed, preferences), task


def recommend(vram_gb: float | None, ram_gb: float | None) -> dict:
    """Modelos que cabem: na VRAM (com ~15% de folga para o contexto) ou, sem GPU, na RAM (metade)."""
    if vram_gb:
        budget, where = vram_gb * 0.85, "gpu"
    elif ram_gb:
        budget, where = ram_gb * 0.5, "cpu"
    else:
        budget, where = 0.0, "desconhecido"
    fits = [m for m in CATALOG if m["size_gb"] <= budget]
    if not vram_gb:
        profile = "inicial"
    elif vram_gb < 24:
        profile = "intermédia"
    elif vram_gb < 48:
        profile = "recomendada"
    else:
        profile = "avançada"
    return {
        "profile": profile,
        "runs_on": where,
        "budget_gb": round(budget, 1),
        "fits": [m["name"] for m in fits],
        "too_big": [m["name"] for m in CATALOG if m["size_gb"] > budget],
        "start_with": [n for n in ("qwen3:8b", "qwen2.5-coder:7b", "nomic-embed-text")
                       if n in {m["name"] for m in fits}],
    }
