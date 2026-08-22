import os
import json
import logging
import asyncio
import time
import re
import unicodedata
from datetime import datetime

from fastapi import FastAPI
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

fila_comandos = queue.Queue()

pending_actions = {}

user_context = {}

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


# ============================================================
# LIMPAR RESPOSTAS DA IA
# ============================================================

def limpar_resposta_ia(texto):

    if not texto:
        return ""

    texto = str(texto)

    # Remove raciocínio interno
    texto = re.sub(
        r"<think>.*?</think>",
        "",
        texto,
        flags=re.DOTALL | re.IGNORECASE
    )

    # Remove possíveis blocos de análise
    texto = re.sub(
        r"<analysis>.*?</analysis>",
        "",
        texto,
        flags=re.DOTALL | re.IGNORECASE
    )

    # Remove excesso de linhas
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
            f"Erro ao salvar na memória: {e}"
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
# CONFIRMAÇÃO INTELIGENTE
# ============================================================

def resposta_e_confirmacao(
    texto
):

    texto_normalizado = normalizar_texto(
        texto
    )

    cancelamentos_exatos = {
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
    }

    if texto_normalizado in cancelamentos_exatos:
        return "cancelar"

    if (
        texto_normalizado.startswith("nao ")
        or texto_normalizado.startswith("nao,")
    ):
        return "cancelar"

    confirmacoes_exatas = {
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
    ]

    for padrao in padroes_confirmacao:

        if re.search(
            padrao,
            texto_normalizado
        ):
            return "confirmar"

    return "indefinido"


# ============================================================
# ANÁLISE DE INTENÇÃO
# ============================================================

def analisar_intencao(
    texto_usuario,
    chat_id
):

    if not client:

        return {
            "intent": "unknown",
            "target": None,
            "argumento": None,
            "risk": "verde",
            "new_fact": None
        }

    memoria_atual = carregar_memoria()

    contexto = obter_contexto_conversa(
        chat_id
    )

    prompt = f"""
Você é o assistente virtual J.A.R.V.I.S.

Seu objetivo é compreender naturalmente o que
o Senhor Gustavo deseja.

Não trate toda mensagem como comando.
Ele também pode simplesmente conversar,
fazer perguntas ou pedir pesquisas.

============================================================
MEMÓRIA
============================================================

{memoria_atual}

============================================================
CONTEXTO
============================================================

{contexto}

============================================================
INTENÇÕES
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

1. Pergunta geral ou conversa:
chat.

2. Informação atual, preço, notícia,
evento ou informação mutável:
web_search.

3. Pedido explícito de pesquisa:
web_search.

4. restart e shutdown:
risk = vermelho.

5. lock_screen:
risk = verde.

6. set_volume:
argumento de 0 a 100.

7. media_control:
target = play_pause, next ou prev.

8. open_app:
target identifica app/site.

9. open_and_search:
target = plataforma.
argumento = termo de pesquisa.

10. Para chat:
argumento pode ser null.
A resposta será gerada depois.

11. Para web_search:
argumento = consulta.

12. Salve apenas fatos realmente úteis.

============================================================
FORMATO JSON
============================================================

{{
    "intent": "open_app" | "open_and_search" |
               "send_whatsapp" | "media_control" |
               "set_volume" | "restart" |
               "shutdown" | "lock_screen" |
               "chat" | "web_search" | "unknown",

    "target": "valor ou null",

    "argumento": "valor ou null",

    "risk": "verde" | "vermelho",

    "new_fact": {{
        "categoria": "Preferência|Contato|Rotina|Projeto|Emocional|Outros",
        "informacao": "novo fato"
    }}
}}

Se não houver novo fato:

"new_fact": null

============================================================
MENSAGEM ATUAL
============================================================

{texto_usuario}
"""

    try:

        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",
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

        return resultado

    except Exception as e:

        logger.exception(
            f"Erro na análise da IA: {e}"
        )

        return {
            "intent": "unknown",
            "target": None,
            "argumento": None,
            "risk": "verde",
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
            "Desculpe, Senhor. "
            "Meu núcleo de inteligência "
            "está indisponível."
        )

    memoria_atual = carregar_memoria()

    contexto = obter_contexto_conversa(
        chat_id
    )

    prompt = f"""
Você é J.A.R.V.I.S., o assistente pessoal
do Senhor Gustavo.

Responda naturalmente à mensagem dele.

Seja inteligente, claro, natural e útil.

Não finja executar ações.

Se não souber algo, seja honesto.

Não mencione arquitetura interna,
classificação de intenção ou este prompt.

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

Responda diretamente ao Senhor Gustavo.
"""

    try:

        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",
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
            "Desculpe, Senhor. "
            "Não consegui formular uma resposta agora."
        )

    except Exception as e:

        logger.exception(
            f"Erro ao gerar resposta: {e}"
        )

        return (
            "Desculpe, Senhor. "
            "Meu núcleo de conversação "
            "apresentou uma falha."
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
            "Desculpe, Senhor. "
            "O núcleo de IA está indisponível."
        )

    memoria_atual = carregar_memoria()

    contexto = obter_contexto_conversa(
        chat_id
    )

    if not resultados:

        return (
            "Senhor, a pesquisa não retornou "
            "resultados utilizáveis neste momento."
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
Você é J.A.R.V.I.S., assistente pessoal
do Senhor Gustavo.

Responda à pergunta dele usando os resultados
da pesquisa como fonte principal.

Não invente informações não sustentadas pelos resultados.

Se os resultados forem insuficientes,
diga isso claramente.

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

Responda de forma natural e útil.
"""

    try:

        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",
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
            "Senhor, encontrei resultados, "
            "mas não consegui montar uma resposta."
        )

    except Exception as e:

        logger.exception(
            f"Erro ao interpretar pesquisa: {e}"
        )

        return (
            "Senhor, a pesquisa foi realizada, "
            "mas houve um erro ao analisar os resultados."
        )


# ============================================================
# ENVIAR COMANDO AO AGENTE LOCAL
# ============================================================

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

    fila_comandos.put(
        comando
    )

    logger.info(
        f"Comando enviado ao agente local: "
        f"{comando}"
    )


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
            "J.A.R.V.I.S. Core Online. "
            "Estou à sua inteira disposição, Senhor."
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

    logger.info(
        f"Mensagem recebida do Telegram: "
        f"{texto}"
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

            resposta = (
                "Senhor, essa solicitação "
                "de confirmação expirou. "
                "Envie o comando novamente "
                "caso ainda deseje executá-lo."
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

        if decisao == "confirmar":

            acao_confirmada = pendente[
                "acao"
            ]

            pending_actions.pop(
                chat_id,
                None
            )

            enviar_para_agente(
                acao_confirmada
            )

            resposta = (
                f"Comando confirmado, Senhor. "
                f"Executando: {acao_confirmada}."
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

        if decisao == "cancelar":

            pending_actions.pop(
                chat_id,
                None
            )

            resposta = (
                "Operação cancelada, Senhor."
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

        resposta = (
            "Preciso de uma confirmação clara, Senhor. "
            "Pode dizer 'pode executar' para confirmar "
            "ou 'cancela' para abortar."
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

    intent = analise.get(
        "intent"
    )

    target = analise.get(
        "target"
    )

    argumento = analise.get(
        "argumento"
    )

    if intent:
        intent = str(
            intent
        ).strip().lower()

    logger.info(
        f"Intenção identificada: "
        f"{intent} | Alvo: {target} | "
        f"Argumento: {argumento}"
    )

    # ========================================================
    # AÇÕES CRÍTICAS
    # ========================================================

    if intent in ACTIONS_REQUIRING_CONFIRMATION:

        pending_actions[chat_id] = {
            "acao": intent,
            "criado_em": time.time()
        }

        if intent == "restart":

            resposta = (
                "⚠️ Senhor, o comando solicita a "
                "REINICIALIZAÇÃO do computador.\n\n"
                "Deseja realmente executar essa ação?"
            )

        else:

            resposta = (
                "⚠️ Senhor, o comando solicita o "
                "DESLIGAMENTO do computador.\n\n"
                "Deseja realmente executar essa ação?"
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
    # AÇÕES LOCAIS
    # ========================================================

    if intent in LOCAL_ACTIONS:

        enviar_para_agente(
            intent,
            target,
            argumento
        )

        if intent == "lock_screen":

            resposta = (
                "🔒 Bloqueando a tela, Senhor."
            )

        elif intent == "set_volume":

            resposta = (
                f"🔊 Ajustando o volume para "
                f"{argumento}%."
            )

        else:

            resposta = (
                "Comando processado para a "
                "máquina local, Senhor."
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
    # PESQUISA WEB
    # ========================================================

    if intent == "web_search":

        consulta = (
            str(argumento).strip()
            if argumento
            else texto
        )

        mensagem_status = await update.message.reply_text(
            f"🔍 Buscando informações sobre:\n"
            f"{consulta}"
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

        adicionar_contexto(
            chat_id,
            "assistant",
            resposta
        )

        try:

            await mensagem_status.edit_text(
                resposta
            )

        except Exception:

            await update.message.reply_text(
                resposta
            )

        return

    # ========================================================
    # CONVERSA
    # ========================================================

    if intent == "chat":

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
    # UNKNOWN
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

        telegram_app = (
            ApplicationBuilder()
            .token(TELEGRAM_TOKEN)
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

        await telegram_app.updater.start_polling(
            drop_pending_updates=False
        )

        logger.info(
            "J.A.R.V.I.S. Telegram Polling iniciado."
        )

    except Exception as e:

        logger.exception(
            f"Erro ao iniciar o Telegram: {e}"
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

        if telegram_app.updater:
            await telegram_app.updater.stop()

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
        "Conversação, Memória, Web e Controle Local ativos."
    }


@app.get("/pegar-comando")
def pegar_comando():

    if not fila_comandos.empty():

        comando = fila_comandos.get()

        logger.info(
            f"Agente local retirou comando da fila: "
            f"{comando}"
        )

        return comando

    return {
        "status": "vazio"
    }
