import os
import json
import logging
import asyncio
import time
import re
import unicodedata
import random
from datetime import datetime

from fastapi import FastAPI, Request, Response
import queue

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters
)

from groq import Groq
import gspread
from ddgs import DDGS


# ============================================================
# CONFIGURAÇÃO
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("JARVIS_CORE")


# ============================================================
# CREDENCIAIS
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


# ============================================================
# ESTADO DO JARVIS
# ============================================================

app = FastAPI()
telegram_app = None

# Fila temporária de comandos locais.
fila_comandos = queue.Queue()

# Estado do agente local
AGENT_OFFLINE_AFTER = 30
agent_last_seen = 0.0

# URL pública do Render usada pelo webhook do Telegram.
RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://jarvis-cloud-api-qp03.onrender.com"
).rstrip("/")

# Token secreto opcional para validar os webhooks recebidos do Telegram.
TELEGRAM_WEBHOOK_SECRET = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET",
    ""
)

# Guarda ações críticas aguardando confirmação
pending_actions = {}

# Contexto temporário de conversa
user_context = {}

# Reservado para logs/futuras expansões
historico_logs = []

CONFIRMATION_TIMEOUT = 300


# ============================================================
# AÇÕES
# ============================================================

LOCAL_ACTIONS = {
    "open_app",
    "open_and_search",
    "send_whatsapp",
    "media_control",
    "set_volume",
    "lock_screen",
}

CRITICAL_ACTIONS = {
    "restart",
    "shutdown",
}

ACTIONS_REQUIRING_CONFIRMATION = {
    "restart",
    "shutdown",
}

SUPPORTED_INTENTS = LOCAL_ACTIONS | CRITICAL_ACTIONS | {
    "chat",
    "web_search",
    "unknown"
}


# ============================================================
# PERSONALIDADE DO J.A.R.V.I.S.
# ============================================================

PERSONALITY_RULES = """
Você é J.A.R.V.I.S., o assistente pessoal do Gustavo.

Sua personalidade deve ser:

- Inteligente e natural.
- Amigável e descontraída.
- Confiante, mas nunca arrogante.
- Útil e objetiva quando a pergunta for simples.
- Responda de forma curta e direta por padrão.
- Para perguntas simples, normalmente use 1 a 4 frases.
- Evite explicações longas quando uma resposta curta for suficiente.
- Não repita informações desnecessariamente.
- Só aprofunde quando Gustavo pedir ou quando a pergunta realmente exigir.
- Se Gustavo pedir "explica melhor", "detalha", "aprofundar" ou algo equivalente,
  aumente o nível de detalhe.
- Capaz de brincar e usar humor quando o contexto permitir.
- Pode usar algumas gírias e expressões informais naturalmente.
- Não deve parecer um robô extremamente formal.
- Não precisa chamar Gustavo de "Senhor" em toda resposta.
- Use "Gustavo" quando fizer sentido.
- Evite repetir frases como:
  "Estou à sua inteira disposição, Senhor."
  "Comando processado, Senhor."
  "Deseja que eu..."
- Não force gírias.
- Em assuntos sérios, técnicos ou importantes, priorize clareza.
- Não seja infantil.
- Não exagere em emojis.
- Não elogie Gustavo sem motivo.
- Pode demonstrar personalidade e senso de humor.
- Se Gustavo fizer uma piada, pode acompanhar a brincadeira.
- Se Gustavo estiver frustrado ou irritado, responda de maneira
  tranquila e compreensiva.
- Se Gustavo estiver falando normalmente, converse normalmente.

IMPORTANTE:

Você não deve fingir que realizou uma ação que não foi executada.

Nunca diga que abriu, fechou, alterou, enviou ou executou algo
se essa ação não tiver realmente sido enviada ao agente local
ou confirmada pelo sistema.

============================================================
IDIOMA
============================================================

Detecte automaticamente o idioma predominante usado por Gustavo.

Responda no MESMO idioma da mensagem dele.

Idiomas principais suportados:

- Português
- Inglês
- Francês
- Espanhol

Se Gustavo misturar idiomas, identifique o idioma predominante
da mensagem e responda nele.

Se ele mudar de idioma, acompanhe imediatamente.

Não traduza a mensagem dele sem que ele peça.

Essa regra também vale para pesquisas na internet e mensagens
automáticas de confirmação/cancelamento.
"""


# ============================================================
# DETECÇÃO DE IDIOMA
# ============================================================

def detectar_idioma(texto):

    if not texto:
        return "pt"

    texto_normalizado = normalizar_texto(texto)
    palavras = texto_normalizado.split()

    pontuacoes = {
        "pt": 0,
        "en": 0,
        "fr": 0,
        "es": 0
    }

    palavras_pt = {
        "que", "qual", "como", "porque", "por", "para",
        "voce", "eu", "meu", "minha", "isso", "esse",
        "essa", "onde", "quando", "tambem", "nao", "sim",
        "abre", "feche", "abrir", "fechar", "desliga",
        "desligar", "reinicia", "reiniciar", "computador",
        "pc", "musica", "gosto", "acho", "cara", "mano",
        "kkkk", "pode", "quero", "preciso"
    }

    palavras_en = {
        "what", "which", "how", "why", "where", "when",
        "who", "can", "could", "would", "should", "the",
        "this", "that", "you", "your", "i", "my", "me",
        "is", "are", "do", "does", "did", "open", "close",
        "turn", "off", "restart", "computer", "please",
        "want", "need", "music", "think", "bro", "yeah",
        "nope", "cancel"
    }

    palavras_fr = {
        "quelle", "quel", "quels", "quelles", "est",
        "sont", "comment", "pourquoi", "ou", "quand",
        "qui", "peux", "peut", "tu", "vous", "je",
        "mon", "ma", "mes", "ton", "ta", "tes", "le",
        "la", "les", "un", "une", "des", "dans", "avec",
        "pour", "mais", "oui", "non", "ouvre", "ouvrir",
        "ferme", "fermer", "eteins", "ordinateur",
        "musique", "merci", "francais", "explique",
        "laisse", "tomber", "annule", "annuler"
    }

    palavras_es = {
        "que", "cual", "como", "porque", "donde",
        "cuando", "quien", "yo", "tu", "usted", "mi",
        "mis", "tus", "el", "la", "los", "las", "un",
        "una", "para", "con", "pero", "si", "no", "abre",
        "abrir", "cierra", "cerrar", "apaga", "apagar",
        "reinicia", "reiniciar", "computadora",
        "ordenador", "musica", "explica", "gracias",
        "espanol"
    }

    for palavra in palavras:

        if palavra in palavras_pt:
            pontuacoes["pt"] += 1

        if palavra in palavras_en:
            pontuacoes["en"] += 1

        if palavra in palavras_fr:
            pontuacoes["fr"] += 1

        if palavra in palavras_es:
            pontuacoes["es"] += 1

    # Marcadores fortes de francês
    if any(
        marcador in texto.lower()
        for marcador in [
            "quelle est",
            "qu'est-ce",
            "est-ce que",
            "peux-tu",
            "peux tu",
            "pourquoi",
            "en français",
            "en francais"
        ]
    ):
        pontuacoes["fr"] += 4

    # Marcadores fortes de inglês
    if any(
        marcador in texto.lower()
        for marcador in [
            "what is",
            "what's",
            "how does",
            "how do",
            "can you",
            "in english"
        ]
    ):
        pontuacoes["en"] += 4

    # Marcadores fortes de espanhol
    if any(
        marcador in texto.lower()
        for marcador in [
            "¿",
            "¡",
            "cuál es",
            "como se",
            "por qué",
            "en español"
        ]
    ):
        pontuacoes["es"] += 4

    # Marcadores fortes de português
    if any(
        marcador in texto.lower()
        for marcador in [
            "qual é",
            "qual e",
            "como funciona",
            "por que",
            "me explica",
            "em português",
            "em portugues"
        ]
    ):
        pontuacoes["pt"] += 4

    idioma = max(
        pontuacoes,
        key=pontuacoes.get
    )

    if pontuacoes[idioma] == 0:
        return "pt"

    return idioma


# ============================================================
# RESPOSTAS AUTOMÁTICAS MULTILÍNGUES
# ============================================================

def resposta_confirmacao_pendente(
    acao,
    idioma
):

    if idioma == "fr":

        if acao == "restart":
            return (
                "⚠️ Tu as demandé de redémarrer "
                "l'ordinateur.\n\n"
                "Tu veux vraiment exécuter cette action ?"
            )

        return (
            "⚠️ Tu as demandé d'éteindre "
            "l'ordinateur.\n\n"
            "Tu veux vraiment exécuter cette action ?"
        )

    if idioma == "en":

        if acao == "restart":
            return (
                "⚠️ You asked me to restart "
                "the computer.\n\n"
                "Do you really want to execute this action?"
            )

        return (
            "⚠️ You asked me to shut down "
            "the computer.\n\n"
            "Do you really want to execute this action?"
        )

    if idioma == "es":

        if acao == "restart":
            return (
                "⚠️ Pediste reiniciar "
                "el ordenador.\n\n"
                "¿Realmente quieres ejecutar esta acción?"
            )

        return (
            "⚠️ Pediste apagar "
            "el ordenador.\n\n"
            "¿Realmente quieres ejecutar esta acción?"
        )

    if acao == "restart":
        return (
            "⚠️ Você pediu para reiniciar o computador.\n\n"
            "Quer mesmo executar essa ação?"
        )

    return (
        "⚠️ Você pediu para desligar o computador.\n\n"
        "Quer mesmo executar essa ação?"
    )


def resposta_cancelamento(idioma):

    respostas = {

        "pt": [
            "Beleza, operação cancelada.",
            "Fechou kkkkk, cancelei.",
            "Tranquilo, não vou executar.",
            "Tá cancelado. 😎"
        ],

        "en": [
            "Alright, operation cancelled.",
            "Got it, cancelled.",
            "No worries, I won't execute it.",
            "Cancelled. 😎"
        ],

        "fr": [
            "D'accord, opération annulée.",
            "Pas de souci, j'annule ça.",
            "C'est bon, je ne vais pas l'exécuter.",
            "Annulé. 😎"
        ],

        "es": [
            "Vale, operación cancelada.",
            "Entendido, lo cancelo.",
            "Tranqui, no voy a ejecutarlo.",
            "Cancelado. 😎"
        ]
    }

    return random.choice(
        respostas.get(
            idioma,
            respostas["pt"]
        )
    )


def resposta_confirmacao_sucesso(
    acoes,
    idioma
):

    nomes = ", ".join(
        acao.get("intent", "unknown")
        for acao in acoes
    )

    if idioma == "fr":
        return (
            f"C'est bon. Confirmation reçue. "
            f"Exécution : {nomes}."
        )

    if idioma == "en":
        return (
            f"Alright. Confirmation received. "
            f"Executing: {nomes}."
        )

    if idioma == "es":
        return (
            f"Listo. Confirmación recibida. "
            f"Ejecutando: {nomes}."
        )

    return (
        f"Fechou. Confirmado. "
        f"Executando: {nomes}."
    )


def resposta_confirmacao_invalida(idioma):

    if idioma == "fr":
        return (
            "J'ai besoin d'une confirmation claire. "
            "Tu peux dire « oui, exécute » pour confirmer "
            "ou « annule » pour arrêter."
        )

    if idioma == "en":
        return (
            "I need a clear confirmation. "
            "You can say 'yes, execute' to confirm "
            "or 'cancel' to stop."
        )

    if idioma == "es":
        return (
            "Necesito una confirmación clara. "
            "Puedes decir «sí, ejecuta» para confirmar "
            "o «cancela» para detenerlo."
        )

    return (
        "Preciso de uma confirmação clara. "
        "Pode dizer 'pode executar' para confirmar "
        "ou 'cancela' para abortar."
    )


def resposta_acao_local(
    intent,
    target,
    argumento,
    idioma
):

    if idioma == "fr":

        if intent == "lock_screen":
            return "🔒 Verrouillage de l'écran."

        if intent == "set_volume":
            return f"🔊 Volume réglé à {argumento}%."

        if intent == "media_control":

            respostas = {
                "play_pause": "⏯️ Lecture/pause.",
                "next": "⏭️ Passage au morceau suivant.",
                "prev": "⏮️ Retour au morceau précédent."
            }

            return respostas.get(
                target,
                "🎵 Commande multimédia envoyée."
            )

        return "C'est bon. Commande envoyée au PC."

    if idioma == "en":

        if intent == "lock_screen":
            return "🔒 Locking the screen."

        if intent == "set_volume":
            return f"🔊 Volume set to {argumento}%."

        if intent == "media_control":

            respostas = {
                "play_pause": "⏯️ Playing/pausing.",
                "next": "⏭️ Skipping to the next track.",
                "prev": "⏮️ Going back to the previous track."
            }

            return respostas.get(
                target,
                "🎵 Media command sent."
            )

        return "Done. Command sent to the PC."

    if idioma == "es":

        if intent == "lock_screen":
            return "🔒 Bloqueando la pantalla."

        if intent == "set_volume":
            return f"🔊 Volumen ajustado al {argumento}%."

        if intent == "media_control":

            respostas = {
                "play_pause": "⏯️ Reproduciendo/pausando.",
                "next": "⏭️ Pasando a la siguiente.",
                "prev": "⏮️ Volviendo a la anterior."
            }

            return respostas.get(
                target,
                "🎵 Comando multimedia enviado."
            )

        return "Listo. Comando enviado al PC."

    if intent == "lock_screen":
        return "🔒 Bloqueando a tela."

    if intent == "set_volume":
        return f"🔊 Volume ajustado para {argumento}%."

    if intent == "media_control":

        respostas = {
            "play_pause": "⏯️ Pausando/tocando.",
            "next": "⏭️ Pulando para a próxima.",
            "prev": "⏮️ Voltando para a anterior."
        }

        return respostas.get(
            target,
            "🎵 Controle de mídia enviado."
        )

    return "Fechou. Comando enviado para o PC."


# ============================================================
# LIMPAR RESPOSTAS DA IA
# ============================================================

def limpar_resposta_ia(texto):

    if not texto:
        return ""

    texto = str(texto)

    texto = re.sub(
        r"<think>.*?</think>",
        "",
        texto,
        flags=re.DOTALL | re.IGNORECASE
    )

    texto = re.sub(
        r"<analysis>.*?</analysis>",
        "",
        texto,
        flags=re.DOTALL | re.IGNORECASE
    )

    texto = re.sub(
        r"\n{3,}",
        "\n\n",
        texto
    )

    return texto.strip()


# ============================================================
# GOOGLE SHEETS — MEMÓRIA
# ============================================================

planilha_memoria = None

if GOOGLE_CREDENTIALS:

    try:

        creds_dict = json.loads(
            GOOGLE_CREDENTIALS
        )

        gc = gspread.service_account_from_dict(
            creds_dict
        )

        planilha_memoria = gc.open(
            "JARVIS_Memoria"
        ).sheet1

        logger.info(
            "Conectado à Memória Permanente "
            "(Google Sheets) com sucesso!"
        )

    except Exception as e:

        logger.error(
            f"Erro ao conectar no Google Sheets: {e}"
        )


def carregar_memoria():

    if not planilha_memoria:
        return "Nenhuma memória conectada."

    try:

        registros = planilha_memoria.get_all_records()

        if not registros:
            return "A memória ainda está vazia."

        linhas = []

        for linha in registros:

            categoria = linha.get(
                "Categoria",
                "Geral"
            )

            informacao = linha.get(
                "Informação",
                ""
            )

            linhas.append(
                f"- {categoria}: {informacao}"
            )

        return "\n".join(linhas)

    except Exception as e:

        logger.error(
            f"Erro ao ler memória: {e}"
        )

        return "Erro ao acessar memória."


def salvar_memoria(
    categoria,
    informacao
):

    if not planilha_memoria:
        return

    try:

        data_atual = datetime.now().strftime(
            "%d/%m/%Y %H:%M"
        )

        planilha_memoria.append_row(
            [
                data_atual,
                categoria,
                informacao
            ]
        )

        logger.info(
            f"Nova memória salva: "
            f"{categoria} - {informacao}"
        )

    except Exception as e:

        logger.error(
            f"Erro ao salvar memória: {e}"
        )


# ============================================================
# CONTEXTO DE CONVERSA
# ============================================================

def obter_contexto_conversa(
    chat_id
):

    historico = user_context.get(
        chat_id,
        []
    )

    if not historico:
        return "Nenhuma conversa anterior disponível."

    linhas = []

    for item in historico[-10:]:

        papel = item.get("role")

        conteudo = item.get(
            "content"
        )

        nome = (
            "Gustavo"
            if papel == "user"
            else "J.A.R.V.I.S."
        )

        linhas.append(
            f"{nome}: {conteudo}"
        )

    return "\n".join(linhas)


def adicionar_contexto(
    chat_id,
    role,
    content
):

    if chat_id not in user_context:
        user_context[chat_id] = []

    user_context[chat_id].append(
        {
            "role": role,
            "content": content
        }
    )

    user_context[chat_id] = (
        user_context[chat_id][-20:]
    )


# ============================================================
# NORMALIZAÇÃO
# ============================================================

def normalizar_texto(
    texto
):

    texto = texto.lower().strip()

    texto = unicodedata.normalize(
        "NFD",
        texto
    )

    texto = "".join(
        caractere
        for caractere in texto
        if unicodedata.category(caractere) != "Mn"
    )

    texto = re.sub(
        r"[^\w\s]",
        " ",
        texto
    )

    texto = re.sub(
        r"\s+",
        " ",
        texto
    )

    return texto.strip()


# ============================================================
# CONFIRMAÇÃO INTELIGENTE MULTILÍNGUE
# ============================================================

def resposta_e_confirmacao(
    texto
):

    texto_normalizado = normalizar_texto(
        texto
    )

    cancelamentos_exatos = {

        # Português
        "nao",
        "nao pode",
        "cancela",
        "cancelar",
        "cancele",
        "deixa",
        "deixa pra la",
        "esquece",
        "nao quero",
        "pare",
        "para",
        "abort",
        "aborta",
        "negativo",
        "nem pensar",
        "nem fodendo",
        "nem ferrando",
        "de jeito nenhum",
        "de forma nenhuma",
        "nem a pau",

        # Inglês
        "no",
        "nope",
        "nah",
        "cancel",
        "cancel it",
        "never mind",
        "forget it",
        "dont do it",
        "do not do it",
        "hell no",
        "no way",

        # Francês
        "non",
        "annule",
        "annuler",
        "annule ca",
        "laisse tomber",
        "pas question",
        "surtout pas",
        "oublie",
        "oublie ca",

        # Espanhol
        "no",
        "cancela",
        "cancelar",
        "cancelalo",
        "olvidalo",
        "deja eso",
        "ni de broma",
        "ni loco",
        "de ninguna manera"
    }

    if texto_normalizado in cancelamentos_exatos:
        return "cancelar"

    padroes_cancelamento = [
        r"\bnem fodendo\b",
        r"\bnem ferrando\b",
        r"\bnem pensar\b",
        r"\bde jeito nenhum\b",
        r"\bde forma nenhuma\b",
        r"\bnem a pau\b",
        r"\bcancela isso\b",
        r"\bcancela ai\b",
        r"\bnao faz isso\b",
        r"\bdeixa pra la\b",
        r"\besquece isso\b",

        r"\bhell no\b",
        r"\bno way\b",
        r"\bnever mind\b",
        r"\bforget it\b",
        r"\bcancel that\b",
        r"\bdont do that\b",
        r"\bdo not do that\b",

        r"\blaisse tomber\b",
        r"\bpas question\b",
        r"\bsurtout pas\b",
        r"\boublie ca\b",
        r"\bannule ca\b",

        r"\bni de broma\b",
        r"\bni loco\b",
        r"\bde ninguna manera\b",
        r"\bcancela eso\b",
        r"\bolvida eso\b"
    ]

    for padrao in padroes_cancelamento:

        if re.search(
            padrao,
            texto_normalizado,
            flags=re.IGNORECASE
        ):
            return "cancelar"

    # Evita que "no..." ou "não..." passe como confirmação
    if (
        texto_normalizado.startswith("nao ")
        or texto_normalizado.startswith("no ")
        or texto_normalizado.startswith("non ")
    ):
        return "cancelar"

    confirmacoes_exatas = {

        # Português
        "sim",
        "confirmo",
        "confirmado",
        "confirmar",
        "confirma",
        "pode",
        "pode executar",
        "pode confirmar",
        "confirma a acao",
        "autorizo",
        "autorizado",
        "pode mandar",
        "manda ver",
        "pode mandar ver",
        "executa",
        "execute",
        "vai",
        "pode fazer",
        "faz",
        "faca",
        "pode prosseguir",
        "prossiga",

        # Inglês
        "yes",
        "yeah",
        "yep",
        "confirm",
        "confirmed",
        "confirm it",
        "go ahead",
        "execute",
        "do it",
        "proceed",
        "you can do it",
        "yes please",

        # Francês
        "oui",
        "confirme",
        "confirmer",
        "confirme ca",
        "vas y",
        "execute",
        "exécute",
        "fais le",
        "fais ca",
        "continue",

        # Espanhol
        "si",
        "confirmo",
        "confirmar",
        "confirma",
        "hazlo",
        "ejecuta",
        "adelante",
        "continua",
        "procede"
    }

    if texto_normalizado in confirmacoes_exatas:
        return "confirmar"

    padroes_confirmacao = [
        r"^sim.*$",
        r"^pode .*execut",
        r"^pode .*fazer",
        r"^pode .*confirm",
        r"^confirma .*",
        r"^eu autorizo.*",
        r"^esta autorizado.*",
        r"^manda .*ver.*",
        r"^pode prosseguir.*",

        r"^yes.*$",
        r"^yeah.*$",
        r"^yep.*$",
        r"^go ahead.*$",
        r"^please execute.*$",
        r"^execute.*$",
        r"^do it.*$",
        r"^proceed.*$",

        r"^oui.*$",
        r"^vas y.*$",
        r"^exécute.*$",
        r"^fais.*$",
        r"^continue.*$",

        r"^si.*$",
        r"^adelante.*$",
        r"^ejecuta.*$",
        r"^hazlo.*$",
        r"^continua.*$",
        r"^procede.*$"
    ]

    for padrao in padroes_confirmacao:

        if re.search(
            padrao,
            texto_normalizado
        ):
            return "confirmar"

    return "indefinido"


# ============================================================
# NORMALIZAR AÇÕES RETORNADAS PELA IA
# ============================================================

def normalizar_acoes_resultado(
    resultado
):
    """
    Converte a resposta do modelo para o formato novo:

    {
        "actions": [
            {
                "intent": "...",
                "target": "...",
                "argumento": "..."
            }
        ]
    }

    Também mantém compatibilidade com o formato antigo,
    caso o modelo eventualmente devolva "intent"/"target"/"argumento".
    """

    acoes = resultado.get("actions")

    if isinstance(acoes, dict):
        acoes = [acoes]

    if not isinstance(acoes, list):

        intent_antiga = resultado.get(
            "intent"
        )

        if intent_antiga:
            acoes = [
                {
                    "intent": intent_antiga,
                    "target": resultado.get("target"),
                    "argumento": resultado.get("argumento")
                }
            ]

        else:
            acoes = []

    acoes_validas = []

    for acao in acoes:

        if not isinstance(acao, dict):
            continue

        intent = acao.get("intent")

        if not intent:
            continue

        intent = str(
            intent
        ).strip().lower()

        if intent not in SUPPORTED_INTENTS:
            continue

        acoes_validas.append(
            {
                "intent": intent,
                "target": acao.get("target"),
                "argumento": acao.get("argumento")
            }
        )

    return acoes_validas


# ============================================================
# ANÁLISE DE INTENÇÃO — SUPORTE A MÚLTIPLAS AÇÕES
# ============================================================

def analisar_intencao(
    texto_usuario,
    chat_id
):

    if not client:

        return {
            "actions": [],
            "new_fact": None
        }

    memoria_atual = carregar_memoria()

    contexto = obter_contexto_conversa(
        chat_id
    )

    prompt = f"""
{PERSONALITY_RULES}

Sua função é entender o que Gustavo quer fazer.

Uma única mensagem pode conter UMA ou VÁRIAS ações.

Exemplo:

"Abre o ChatGPT e o Spotify."

deve produzir duas ações.

Outro exemplo:

"Abre o YouTube e coloca o volume em 30."

deve produzir duas ações.

IMPORTANTE:
PRESERVE A ORDEM em que Gustavo pediu as ações.

============================================================
MEMÓRIA
============================================================

{memoria_atual}

============================================================
CONTEXTO
============================================================

{contexto}

============================================================
INTENÇÕES DISPONÍVEIS
============================================================

open_app
→ Abrir aplicativo ou site.

open_and_search
→ Abrir site/app e pesquisar.

send_whatsapp
→ Enviar WhatsApp.

media_control
→ Controlar mídia.

set_volume
→ Alterar volume.

restart
→ Reiniciar computador.

shutdown
→ Desligar computador.

lock_screen
→ Bloquear Windows.

chat
→ Conversar e responder perguntas gerais.

web_search
→ Pesquisar na internet.

unknown
→ Quando não houver intenção clara.

============================================================
REGRAS
============================================================

1. Uma mensagem pode conter múltiplas ações.

2. Retorne TODAS as ações detectadas no campo "actions".

3. Preserve a ordem das ações.

4. Perguntas gerais ou conversa:
use "chat".

5. Informação atual, preço, notícia, evento ou informação
mutável:
use "web_search".

6. Pedido explícito de pesquisa:
use "web_search".

7. restart e shutdown são ações críticas.

8. lock_screen não precisa de confirmação.

9. set_volume:
argumento de 0 a 100.

10. media_control:
target = play_pause, next ou prev.

11. open_app:
target identifica o aplicativo ou site.

12. open_and_search:
target = plataforma.
argumento = termo pesquisado.

13. Se Gustavo pedir para abrir um aplicativo e também
fornecer texto para escrever nele:
intent = "open_app"
target = aplicativo
argumento = texto exato solicitado.

14. Se Gustavo pedir para abrir o Bloco de Notas e escrever
alguma coisa, preserve exatamente o texto solicitado em
"argumento".

15. Se Gustavo pedir para abrir o Spotify:
intent = "open_app"
target = "spotify"
argumento = null.

16. Se Gustavo pedir para abrir uma playlist do Spotify
pelo nome, apelido ou descrição:
intent = "open_app"
target = "spotify"
argumento = nome ou apelido da playlist.

Exemplo:
"Abre minha playlist de romance"
→ target = "spotify"
→ argumento = "romance"

17. Se Gustavo fornecer uma URL ou URI de playlist do Spotify:
intent = "open_app"
target = "spotify"
argumento = URL ou URI exata.

18. Se Gustavo pedir várias ações locais,
retorne uma ação para cada uma.

19. Não combine várias ações dentro de um único objeto.

20. Para uma conversa normal:
retorne apenas uma ação "chat".

21. Para uma pesquisa:
retorne apenas uma ação "web_search",
a menos que Gustavo também tenha pedido explicitamente
outras ações separadas.

22. Se houver restart/shutdown junto com outras ações:
retorne todas elas normalmente.
O sistema externo cuidará da confirmação da ação crítica.

23. Só salve fatos úteis e relativamente permanentes.

============================================================
FORMATO JSON OBRIGATÓRIO
============================================================

{{
    "actions": [
        {{
            "intent": "open_app" | "open_and_search" |
                       "send_whatsapp" | "media_control" |
                       "set_volume" | "restart" |
                       "shutdown" | "lock_screen" |
                       "chat" | "web_search" | "unknown",

            "target": "valor ou null",

            "argumento": "valor ou null"
        }}
    ],

    "new_fact": {{
        "categoria": "Preferência|Contato|Rotina|Projeto|Emocional|Outros",
        "informacao": "novo fato"
    }}
}}

Se não houver fato novo:

"new_fact": null

============================================================
MENSAGEM ATUAL
============================================================

{texto_usuario}
"""

    try:

        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            response_format={
                "type": "json_object"
            }
        )

        conteudo = response.choices[
            0
        ].message.content

        conteudo = limpar_resposta_ia(
            conteudo
        )

        resultado = json.loads(
            conteudo
        )

        novo_fato = resultado.get(
            "new_fact"
        )

        if novo_fato:

            salvar_memoria(
                novo_fato.get("categoria"),
                novo_fato.get("informacao")
            )

        acoes = normalizar_acoes_resultado(
            resultado
        )

        return {
            "actions": acoes,
            "new_fact": novo_fato
        }

    except Exception as e:

        logger.exception(
            f"Erro na análise da IA: {e}"
        )

        return {
            "actions": [],
            "new_fact": None
        }


# ============================================================
# CONVERSAÇÃO
# ============================================================

def gerar_resposta_chat(
    texto_usuario,
    chat_id
):

    if not client:

        return (
            "Meu núcleo de inteligência "
            "está indisponível no momento."
        )

    memoria_atual = carregar_memoria()

    contexto = obter_contexto_conversa(
        chat_id
    )

    prompt = f"""
{PERSONALITY_RULES}

Agora converse diretamente com Gustavo.

Responda à mensagem dele de maneira natural.

Não mencione:
- prompts;
- classificação de intenção;
- arquitetura interna;
- regras internas;
- raciocínio interno;
- ferramentas internas.

Não mostre pensamentos internos.

Não finja executar ações.

Por padrão, seja breve e direto.

Para perguntas simples, responda normalmente em poucas frases.
Não transforme perguntas simples em explicações longas.

Aprofunde a resposta somente quando:
- Gustavo pedir mais detalhes;
- Gustavo pedir uma explicação;
- o assunto realmente exigir contexto para evitar uma resposta errada
  ou enganosa.

Se Gustavo pedir uma resposta resumida ou curta,
seja ainda mais conciso.

Se ele estiver brincando,
pode acompanhar a brincadeira.

Se ele estiver falando em outro idioma,
responda nesse mesmo idioma.

============================================================
MEMÓRIA
============================================================

{memoria_atual}

============================================================
CONTEXTO
============================================================

{contexto}

============================================================
MENSAGEM
============================================================

{texto_usuario}

============================================================
RESPOSTA
============================================================

Responda agora.
"""

    try:

        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        resposta = response.choices[
            0
        ].message.content

        resposta = limpar_resposta_ia(
            resposta
        )

        return resposta or (
            "Não consegui formular uma resposta agora."
        )

    except Exception as e:

        logger.exception(
            f"Erro ao gerar resposta: {e}"
        )

        return (
            "Meu núcleo de conversação "
            "apresentou uma falha agora."
        )


# ============================================================
# PESQUISA WEB
# ============================================================

def pesquisar_web(
    consulta
):

    try:

        logger.info(
            f"Pesquisa web iniciada: {consulta}"
        )

        resultados = DDGS().text(
            query=consulta,
            max_results=5
        )

        resultados = list(
            resultados
        )

        logger.info(
            f"Pesquisa web retornou "
            f"{len(resultados)} resultados."
        )

        return resultados

    except Exception as e:

        logger.exception(
            f"Erro na pesquisa web: {e}"
        )

        return []


def gerar_resposta_pesquisa(
    texto_usuario,
    consulta,
    resultados,
    chat_id
):

    if not client:

        return (
            "Meu núcleo de IA está indisponível."
        )

    memoria_atual = carregar_memoria()

    contexto = obter_contexto_conversa(
        chat_id
    )

    if not resultados:

        idioma = detectar_idioma(
            texto_usuario
        )

        mensagens = {

            "pt":
                "Não encontrei resultados confiáveis "
                "o suficiente para responder isso agora.",

            "en":
                "I couldn't find enough reliable results "
                "to answer that right now.",

            "fr":
                "Je n'ai pas trouvé suffisamment de résultats "
                "fiables pour répondre à cette question.",

            "es":
                "No encontré suficientes resultados confiables "
                "para responder a eso ahora."
        }

        return mensagens.get(
            idioma,
            mensagens["pt"]
        )

    contexto_busca = "\n".join(
        [
            (
                f"TÍTULO: {r.get('title', '')}\n"
                f"RESUMO: {r.get('body', '')}\n"
                f"URL: {r.get('href', '')}"
            )
            for r in resultados
        ]
    )

    prompt = f"""
{PERSONALITY_RULES}

Você acabou de realizar uma pesquisa na internet.

Responda à pergunta de Gustavo usando os resultados
abaixo como fonte principal.

Não invente informações que não estejam sustentadas
pelos resultados.

Se os resultados forem insuficientes,
seja transparente.

Não precisa dizer repetidamente
"Senhor Gustavo" ou "Senhor".

Mantenha o mesmo idioma usado por Gustavo na pergunta.

============================================================
MEMÓRIA
============================================================

{memoria_atual}

============================================================
CONTEXTO
============================================================

{contexto}

============================================================
CONSULTA
============================================================

{consulta}

============================================================
RESULTADOS
============================================================

{contexto_busca}

============================================================
PERGUNTA
============================================================

{texto_usuario}

============================================================
RESPOSTA
============================================================

Responda naturalmente.
"""

    try:

        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        resposta = response.choices[
            0
        ].message.content

        resposta = limpar_resposta_ia(
            resposta
        )

        return resposta or (
            "Encontrei resultados, "
            "mas não consegui montar uma resposta."
        )

    except Exception as e:

        logger.exception(
            f"Erro ao interpretar pesquisa: {e}"
        )

        return (
            "A pesquisa foi realizada, "
            "mas houve um erro ao analisar os resultados."
        )


# ============================================================
# ENVIAR COMANDO AO AGENTE LOCAL
# ============================================================

def agente_esta_online():
    return (
        agent_last_seen > 0
        and (time.time() - agent_last_seen) <= AGENT_OFFLINE_AFTER
    )


def enviar_para_agente(
    intent,
    target=None,
    argumento=None
):

    comando = {
        "acao": intent,
        "target": target,
        "argumento": argumento
    }

    if not agente_esta_online():
        logger.warning(
            "Comando local não enfileirado: agente local está offline."
        )
        return False

    fila_comandos.put(comando)

    logger.info(
        f"Comando enviado ao agente local: "
        f"{comando}"
    )

    return True


# ============================================================
# TELEGRAM — START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "J.A.R.V.I.S. online. 😎\n"
            "Manda aí, Gustavo."
        )
    )


# ============================================================
# TELEGRAM — MENSAGENS
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.text:
        return

    texto = update.message.text.strip()

    chat_id = update.effective_chat.id

    idioma = detectar_idioma(
        texto
    )

    logger.info(
        f"Mensagem recebida do Telegram: "
        f"{texto} | Idioma: {idioma}"
    )

    adicionar_contexto(
        chat_id,
        "user",
        texto
    )

    # ========================================================
    # CONFIRMAÇÃO PENDENTE
    # ========================================================

    if chat_id in pending_actions:

        pendente = pending_actions[
            chat_id
        ]

        criado_em = pendente.get(
            "criado_em",
            0
        )

        if (
            time.time() - criado_em
            > CONFIRMATION_TIMEOUT
        ):

            pending_actions.pop(
                chat_id,
                None
            )

            mensagens_expiracao = {

                "pt":
                    "Essa solicitação de confirmação expirou. "
                    "Se ainda quiser executar, manda o comando de novo.",

                "en":
                    "This confirmation request expired. "
                    "If you still want to execute it, send the command again.",

                "fr":
                    "Cette demande de confirmation a expiré. "
                    "Si tu veux toujours l'exécuter, renvoie la commande.",

                "es":
                    "Esta solicitud de confirmación expiró. "
                    "Si todavía quieres ejecutarla, envía el comando de nuevo."
            }

            resposta = mensagens_expiracao.get(
                idioma,
                mensagens_expiracao["pt"]
            )

            adicionar_contexto(
                chat_id,
                "assistant",
                resposta
            )

            await update.message.reply_text(
                resposta
            )

            return

        decisao = resposta_e_confirmacao(
            texto
        )

        # ----------------------------------------------------
        # CONFIRMAR
        # ----------------------------------------------------

        if decisao == "confirmar":

            acoes_confirmadas = pendente[
                "acoes"
            ]

            pending_actions.pop(
                chat_id,
                None
            )

            comandos_enviados = 0

            for acao in acoes_confirmadas:

                if enviar_para_agente(
                    acao["intent"],
                    acao.get("target"),
                    acao.get("argumento")
                ):
                    comandos_enviados += 1

            if comandos_enviados == len(acoes_confirmadas):
                resposta = resposta_confirmacao_sucesso(
                    acoes_confirmadas,
                    idioma
                )
            else:
                respostas_offline_confirmacao = {
                    "pt": "🔴 O PC ficou offline antes da execução. A ação não foi enviada.",
                    "en": "🔴 The PC went offline before execution. The action was not sent.",
                    "fr": "🔴 Le PC est passé hors ligne avant l’exécution. L’action n’a pas été envoyée.",
                    "es": "🔴 El PC quedó desconectado antes de la ejecución. La acción no fue enviada."
                }

                resposta = respostas_offline_confirmacao.get(
                    idioma,
                    respostas_offline_confirmacao["pt"]
                )

            adicionar_contexto(
                chat_id,
                "assistant",
                resposta
            )

            await update.message.reply_text(
                resposta
            )

            return

        # ----------------------------------------------------
        # CANCELAR
        # ----------------------------------------------------

        if decisao == "cancelar":

            pending_actions.pop(
                chat_id,
                None
            )

            resposta = resposta_cancelamento(
                idioma
            )

            adicionar_contexto(
                chat_id,
                "assistant",
                resposta
            )

            await update.message.reply_text(
                resposta
            )

            return

        # ----------------------------------------------------
        # INDEFINIDO
        # ----------------------------------------------------

        resposta = resposta_confirmacao_invalida(
            idioma
        )

        adicionar_contexto(
            chat_id,
            "assistant",
            resposta
        )

        await update.message.reply_text(
            resposta
        )

        return

    # ========================================================
    # ANALISAR INTENÇÃO
    # ========================================================

    analise = await asyncio.to_thread(
        analisar_intencao,
        texto,
        chat_id
    )

    acoes = analise.get(
        "actions",
        []
    )

    logger.info(
        f"Ações identificadas: {acoes}"
    )

    if not acoes:

        resposta = await asyncio.to_thread(
            gerar_resposta_chat,
            texto,
            chat_id
        )

        adicionar_contexto(
            chat_id,
            "assistant",
            resposta
        )

        await update.message.reply_text(
            resposta
        )

        return

    # Remove unknown em conjunto com ações válidas
    acoes = [
        acao
        for acao in acoes
        if acao.get("intent") != "unknown"
    ]

    if not acoes:

        resposta = await asyncio.to_thread(
            gerar_resposta_chat,
            texto,
            chat_id
        )

        adicionar_contexto(
            chat_id,
            "assistant",
            resposta
        )

        await update.message.reply_text(
            resposta
        )

        return

    # ========================================================
    # CONVERSA NORMAL
    # ========================================================
    #
    # Chat é tratado sozinho. Se vier misturado com ações
    # operacionais, usamos o comportamento mais seguro:
    # executamos as ações operacionais e ignoramos o chat
    # como ação, pois a própria resposta pode ser gerada
    # separadamente no futuro.
    # ========================================================

    acoes_chat = [
        acao
        for acao in acoes
        if acao.get("intent") == "chat"
    ]

    acoes_operacionais = [
        acao
        for acao in acoes
        if acao.get("intent") != "chat"
    ]

    if acoes_chat and not acoes_operacionais:

        resposta = await asyncio.to_thread(
            gerar_resposta_chat,
            texto,
            chat_id
        )

        adicionar_contexto(
            chat_id,
            "assistant",
            resposta
        )

        await update.message.reply_text(
            resposta
        )

        return

    # ========================================================
    # SEPARAR AÇÕES CRÍTICAS
    # ========================================================

    acoes_criticas = [
        acao
        for acao in acoes_operacionais
        if acao.get("intent")
        in ACTIONS_REQUIRING_CONFIRMATION
    ]

    acoes_nao_criticas = [
        acao
        for acao in acoes_operacionais
        if acao.get("intent")
        not in ACTIONS_REQUIRING_CONFIRMATION
    ]

    # ========================================================
    # MAIS DE UMA AÇÃO CRÍTICA
    # ========================================================
    #
    # Evitamos combinações perigosas como:
    # "reinicia e desliga o PC".
    # ========================================================

    if len(acoes_criticas) > 1:

        if idioma == "fr":
            resposta = (
                "⚠️ J'ai détecté plusieurs actions critiques "
                "dans la même commande. Choisis une seule "
                "action critique à exécuter."
            )

        elif idioma == "en":
            resposta = (
                "⚠️ I detected multiple critical actions "
                "in the same command. Choose one critical "
                "action to execute."
            )

        elif idioma == "es":
            resposta = (
                "⚠️ Detecté varias acciones críticas "
                "en el mismo comando. Elige una sola "
                "acción crítica para ejecutar."
            )

        else:
            resposta = (
                "⚠️ Detectei mais de uma ação crítica "
                "na mesma mensagem. Escolha uma única "
                "ação crítica para executar."
            )

        adicionar_contexto(
            chat_id,
            "assistant",
            resposta
        )

        await update.message.reply_text(
            resposta
        )

        return

    # ========================================================
    # EXECUTAR AÇÕES NÃO CRÍTICAS
    # ========================================================

    respostas_acoes = []

    for acao in acoes_nao_criticas:

        intent = acao.get(
            "intent"
        )

        target = acao.get(
            "target"
        )

        argumento = acao.get(
            "argumento"
        )

        # ----------------------------------------------------
        # AÇÃO LOCAL
        # ----------------------------------------------------

        if intent in LOCAL_ACTIONS:

            enviado = enviar_para_agente(
                intent,
                target,
                argumento
            )

            if enviado:
                respostas_acoes.append(
                    resposta_acao_local(
                        intent,
                        target,
                        argumento,
                        idioma
                    )
                )
            else:
                mensagens_offline = {
                    "pt": "🔴 O PC está offline ou o agente local não está conectado.",
                    "en": "🔴 The PC is offline or the local agent is not connected.",
                    "fr": "🔴 Le PC est hors ligne ou l’agent local n’est pas connecté.",
                    "es": "🔴 El PC está desconectado o el agente local no está conectado."
                }

                respostas_acoes.append(
                    mensagens_offline.get(
                        idioma,
                        mensagens_offline["pt"]
                    )
                )

        # ----------------------------------------------------
        # PESQUISA WEB
        # ----------------------------------------------------

        elif intent == "web_search":

            consulta = (
                str(argumento).strip()
                if argumento
                else texto
            )

            mensagens_busca = {

                "pt":
                    f"🔍 Pesquisando:\n{consulta}",

                "en":
                    f"🔍 Searching:\n{consulta}",

                "fr":
                    f"🔍 Recherche en cours :\n{consulta}",

                "es":
                    f"🔍 Buscando:\n{consulta}"
            }

            await update.message.reply_text(
                mensagens_busca.get(
                    idioma,
                    mensagens_busca["pt"]
                )
            )

            resultados = await asyncio.to_thread(
                pesquisar_web,
                consulta
            )

            resposta = await asyncio.to_thread(
                gerar_resposta_pesquisa,
                texto,
                consulta,
                resultados,
                chat_id
            )

            respostas_acoes.append(
                resposta
            )

    # ========================================================
    # PEDIR CONFIRMAÇÃO PARA AÇÃO CRÍTICA
    # ========================================================

    if acoes_criticas:

        acao_critica = acoes_criticas[0]

        pending_actions[chat_id] = {
            "acoes": [acao_critica],
            "criado_em": time.time()
        }

        resposta = resposta_confirmacao_pendente(
            acao_critica["intent"],
            idioma
        )

        adicionar_contexto(
            chat_id,
            "assistant",
            resposta
        )

        await update.message.reply_text(
            resposta
        )

        return

    # ========================================================
    # RESPOSTA FINAL DAS AÇÕES NÃO CRÍTICAS
    # ========================================================

    if respostas_acoes:

        resposta = "\n".join(
            respostas_acoes
        )

        adicionar_contexto(
            chat_id,
            "assistant",
            resposta
        )

        await update.message.reply_text(
            resposta
        )

        return

    # ========================================================
    # FALLBACK
    # ========================================================

    resposta = await asyncio.to_thread(
        gerar_resposta_chat,
        texto,
        chat_id
    )

    adicionar_contexto(
        chat_id,
        "assistant",
        resposta
    )

    await update.message.reply_text(
        resposta
    )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():

    global telegram_app

    if not TELEGRAM_TOKEN:

        logger.error(
            "TELEGRAM_TOKEN não configurado."
        )

        return

    try:

        # O FastAPI recebe os webhooks do Telegram diretamente.
        telegram_app = (
            ApplicationBuilder()
            .token(TELEGRAM_TOKEN)
            .updater(None)
            .build()
        )

        telegram_app.add_handler(
            CommandHandler(
                "start",
                start
            )
        )

        telegram_app.add_handler(
            MessageHandler(
                filters.TEXT & (~filters.COMMAND),
                handle_message
            )
        )

        await telegram_app.initialize()
        await telegram_app.start()

        webhook_url = f"{RENDER_EXTERNAL_URL}/telegram/webhook"

        webhook_kwargs = {
            "url": webhook_url,
            "allowed_updates": Update.ALL_TYPES,
        }

        if TELEGRAM_WEBHOOK_SECRET:
            webhook_kwargs["secret_token"] = TELEGRAM_WEBHOOK_SECRET

        await telegram_app.bot.set_webhook(
            **webhook_kwargs
        )

        logger.info(
            f"J.A.R.V.I.S. Telegram Webhook configurado: "
            f"{webhook_url}"
        )

    except Exception as e:

        logger.exception(
            f"Erro ao iniciar o Telegram via Webhook: {e}"
        )

# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown_event():

    global telegram_app

    if not telegram_app:
        return

    try:

        await telegram_app.bot.delete_webhook(
            drop_pending_updates=False
        )

        await telegram_app.stop()
        await telegram_app.shutdown()

        logger.info(
            "J.A.R.V.I.S. Telegram encerrado corretamente."
        )

    except Exception as e:

        logger.error(
            f"Erro ao encerrar Telegram: {e}"
        )

# ============================================================
# ROTAS FASTAPI
# ============================================================

@app.get("/")
def home():

    return {
        "status":
        "JARVIS Core Online — "
        "Conversação, Memória, Web e Controle Local ativos.",
        "agent_online": agente_esta_online()
    }


@app.get("/status")
def status():

    ultimo_heartbeat = (
        time.time() - agent_last_seen
        if agent_last_seen > 0
        else None
    )

    return {
        "jarvis": "online",
        "agent_online": agente_esta_online(),
        "agent_last_seen_seconds": ultimo_heartbeat
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):

    if not telegram_app:
        logger.error(
            "Webhook do Telegram recebido antes do bot ser inicializado."
        )
        return Response(status_code=503)

    if TELEGRAM_WEBHOOK_SECRET:

        recebido = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )

        if recebido != TELEGRAM_WEBHOOK_SECRET:
            logger.warning(
                "Webhook do Telegram rejeitado: secret token inválido."
            )
            return Response(status_code=403)

    try:

        data = await request.json()

        update = Update.de_json(
            data=data,
            bot=telegram_app.bot
        )

        await telegram_app.update_queue.put(
            update
        )

        return Response(status_code=200)

    except Exception as e:

        logger.exception(
            f"Erro ao processar webhook do Telegram: {e}"
        )

        return Response(status_code=500)


@app.post("/agente/heartbeat")
def agente_heartbeat():

    global agent_last_seen

    agent_last_seen = time.time()

    return {
        "status": "ok",
        "agent_online": True
    }


@app.get("/pegar-comando")
def pegar_comando():

    # Long polling: espera por até 25s por um comando.
    try:

        comando = fila_comandos.get(
            timeout=25
        )

        logger.info(
            f"Agente local retirou comando da fila: "
            f"{comando}"
        )

        return comando

    except queue.Empty:

        return {
            "status": "vazio"
        }
