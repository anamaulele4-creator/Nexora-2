"""Prompt de sistema do chat, montado por pedido com as contas ligadas do utilizador."""

from __future__ import annotations

from collections.abc import Mapping

BASE = (
    "És o DÁRIO, o motor de IA da CONNECTOR AI. Responde de forma clara, útil e concisa, na língua em que o "
    "utilizador escrever (por defeito, português europeu). Tenta sempre fazer o que o utilizador pede."
)

HONESTY = (
    "Sê honesto sobre o que fizeste: nunca digas que fizeste algo que não aparece no resultado de uma "
    "ferramenta. Ainda não tens navegador nem controlas o dispositivo do utilizador: não abres sites nem "
    "aplicações no ecrã dele. Quando pedirem para 'abrir' uma app que tens ligada (ex.: 'abre o GitHub'), "
    "usa as ferramentas dessa app para mostrar o que lá está (perfil, repositórios…)."
)

TOOL_LIST = {
    "github": ("perfil, listar repositórios, ver repositório, listar e ler issues, listar pull requests, ler "
               "ficheiros e pastas, pesquisar código; com confirmação do utilizador: criar issues e comentar"),
}

NAMES = {"github": "GitHub", "gmail": "Gmail", "google_drive": "Google Drive", "shopify": "Shopify"}


def build_system_prompt(connected: Mapping[str, str], tool_connectors: list[str], *, tools_enabled: bool,
                        expired: list[str] | None = None) -> str:
    """``connected``: chave -> conta (login/email/loja) das contas ligadas e utilizáveis.
    ``tool_connectors``: as que têm ferramentas ativas neste pedido. ``tools_enabled``: o modelo
    recebe ferramentas (False = modelo sem suporte, chat simples)."""
    expired = expired or []
    lines = [BASE, ""]
    if connected:
        lines.append("Contas ligadas por este utilizador:")
        for key, label in connected.items():
            if key in tool_connectors and tools_enabled:
                lines.append(f"- {NAMES.get(key, key)} ({label}): ferramentas disponíveis — {TOOL_LIST.get(key, '')}.")
            elif key in TOOL_LIST:
                lines.append(f"- {NAMES.get(key, key)} ({label}): ligado, mas sem ferramentas nesta conversa.")
            else:
                lines.append(f"- {NAMES.get(key, key)} ({label}): ligado; as ações com esta app ainda não "
                             "existem no chat (chegam numa próxima versão).")
    else:
        lines.append("Este utilizador ainda não ligou nenhuma conta.")
    if expired:
        lines.append("Acesso expirado (tem de voltar a ligar): " + ", ".join(NAMES.get(k, k) for k in expired) + ".")
    missing = [NAMES[k] for k in NAMES if k not in connected]
    if missing:
        lines.append("Por ligar: " + ", ".join(missing) + ".")
    lines.append("")
    if tools_enabled:
        lines.append(
            "Se um pedido precisar de uma conta que não está ligada (ou cujo acesso expirou), NÃO recuses: diz "
            "qual é a conta necessária e chama a ferramenta request_connection com essa conta — o utilizador vê "
            "um botão para a ligar. Depois de ligar, ele volta a pedir e tu continuas.")
        lines.append(
            "Ações que alteram dados (criar issue, comentar) NÃO são executadas quando as chamas: ficam à espera "
            "de o utilizador carregar em Confirmar. Diz exatamente o que vai acontecer e que aguarda a "
            "confirmação; nunca digas que já foi feito. Antes de criar, confirma o repositório certo (owner/repo).")
        lines.append("Usa as ferramentas de leitura sem pedir licença. Resume os resultados; não despejes JSON.")
    else:
        lines.append(
            "Neste modelo as ferramentas não estão disponíveis, por isso não consegues aceder às contas ligadas "
            "nesta resposta. Se o pedido precisar de uma app, explica isso e sugere escolher outro modelo no "
            "seletor do chat; se a conta não estiver ligada, indica que pode ligá-la em /conectores.")
    lines.append(HONESTY)
    return "\n".join(lines)
