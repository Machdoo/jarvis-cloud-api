import os
import json
import logging
from datetime import datetime
from fastapi import FastAPI
import queue
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from groq import Groq
import gspread
from duckduckgo_search import DDGS

# ============================================================
# CONFIGURAÇÃO DE LOGS
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

fila_comandos = queue.Queue()

# Guarda ações que estão aguardando confirmação
pending_actions = {}

historico_logs = []
user_context = {}

app = FastAPI()
telegram_app = None

# ============================================================
# GOOGLE SHEETS — MEMÓRIA PERMANENTE
# ============================================================

planilha_memoria = None

if GOOGLE_CREDENTIALS:
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)

        gc = gspread.service_account_from_dict(creds_dict)

        planilha_memoria = gc.open("JARVIS_Memoria").sheet1

        logger.info(
            "Conectado à Memória Permanente (Google Sheets) com sucesso!"
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

        texto_memoria = ""

        for linha in registros:

            texto_memoria += (
                f"- {linha.get('Categoria', 'Geral')}: "
                f"{linha.get('Informação', '')}\n"
            )

        return texto_memoria

    except Exception as e:

        logger.error(
            f"Erro ao ler memória: {e}"
        )

        return "Erro ao acessar memória."


def salvar_memoria(categoria, informacao):

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
            f"Nova memória salva: {categoria} - {informacao}"
        )

    except Exception as e:

        logger.error(
            f"Erro ao salvar na memória: {e}"
        )


# ============================================================
# ANÁLISE DE INTENÇÃO — GROQ
# ============================================================

def analisar_intencao(texto_usuario: str, chat_id: int):

    if not client:

        return {
            "intent": "unknown",
            "target": None,
            "argumento": None,
            "risk": "verde",
            "new_fact": None
        }

    memoria_atual = carregar_memoria()

    prompt = f"""
Você é o assistente virtual J.A.R.V.I.S.

Você possui uma personalidade inteligente, natural,
educada, leal e prestativa com o Senhor Gustavo.

Sua função é compreender o que o usuário realmente deseja
e transformar isso em uma intenção estruturada.

============================================================
MEMÓRIA PERMANENTE DO USUÁRIO
============================================================

{memoria_atual}

============================================================
FORMATO OBRIGATÓRIO
============================================================

Retorne APENAS um JSON válido:

{{
    "intent": "open_app" | "open_and_search" | "send_whatsapp" |
               "media_control" | "set_volume" | "restart" |
               "shutdown" | "lock_screen" | "chat" |
               "web_search" | "unknown",

    "target": "app, site, número, ação de mídia ou null",

    "argumento": "termo, valor, mensagem ou null",

    "risk": "verde" | "vermelho",

    "new_fact": {{
        "categoria": "Preferência|Contato|Rotina|Projeto|Emocional|Outros",
        "informacao": "novo fato aprendido"
    }}
}}

Se não houver novo fato, use:

"new_fact": null

============================================================
REGRAS DE INTENÇÃO
============================================================

open_app
→ Abrir aplicativo ou site.

open_and_search
→ Abrir um site/app e realizar uma pesquisa.

send_whatsapp
→ Enviar mensagem pelo WhatsApp.

media_control
→ Controlar mídia.

Valores possíveis para target:

"play_pause"
"next"
"prev"

set_volume
→ Alterar o volume do computador.

argumento deve ser um número entre 0 e 100.

restart
→ Reiniciar o computador.

shutdown
→ Desligar o computador.

lock_screen
→ Bloquear a sessão do Windows.

IMPORTANTE:
Bloquear a tela NÃO precisa de confirmação.

restart e shutdown são ações críticas.
Elas DEVEM usar:

"risk": "vermelho"

lock_screen deve usar:

"risk": "verde"

web_search
→ Pesquisar informações recentes na internet.

chat
→ Conversar normalmente com o usuário.

Se o usuário fizer uma pergunta que possa ser respondida
com conhecimento geral, pode usar chat.

Se ele pedir informações atuais, notícias, preços,
eventos recentes ou explicitamente pedir uma pesquisa,
use web_search.

============================================================
PERSONALIDADE
============================================================

Se o usuário estiver conversando normalmente,
responda naturalmente.

Se ele demonstrar cansaço, estresse, desabafo ou vulnerabilidade,
use intent "chat" e responda com empatia e acolhimento.

============================================================
MEMÓRIA
============================================================

Só salve fatos realmente úteis e relativamente permanentes.

Não salve mensagens aleatórias.

============================================================
COMANDO ATUAL
============================================================

"{texto_usuario}"
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

        resultado = json.loads(
            response.choices[0].message.content
        )

        novo_fato = resultado.get("new_fact")

        if novo_fato:

            salvar_memoria(
                novo_fato.get("categoria"),
                novo_fato.get("informacao")
            )

        return resultado

    except Exception as e:

        logger.error(
            f"Erro na IA: {e}"
        )

        return {
            "intent": "unknown",
            "target": None,
            "argumento": None,
            "risk": "verde",
            "new_fact": None
        }


# ============================================================
# CONFIRMAÇÃO INTELIGENTE
# ============================================================

def resposta_e_confirmacao(texto):

    texto = texto.lower().strip()

    confirmacoes = [
        "sim",
        "sim pode",
        "pode",
        "pode executar",
        "pode confirmar",
        "confirmo",
        "confirma",
        "confirmar",
        "confirma a ação",
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
        "faça",
        "faz",
        "pode prosseguir",
        "prossiga"
    ]

    cancelamentos = [
        "não",
        "nao",
        "não pode",
        "nao pode",
        "cancela",
        "cancelar",
        "cancele",
        "deixa",
        "deixa pra lá",
        "deixa pra la",
        "esquece",
        "não quero",
        "nao quero",
        "pare",
        "para",
        "abort"
    ]

    if texto in cancelamentos:
        return "cancelar"

    if texto in confirmacoes:
        return "confirmar"

    return "indefinido"


# ============================================================
# TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "J.A.R.V.I.S. Core Online. "
            "Estou à sua inteira disposição, Senhor."
        )
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global pending_actions

    texto = update.message.text
    chat_id = update.effective_chat.id

    # ========================================================
    # VERIFICAÇÃO DE CONFIRMAÇÃO PENDENTE
    # ========================================================

    if chat_id in pending_actions:

        decisao = resposta_e_confirmacao(texto)

        if decisao == "confirmar":

            acao_confirmada = pending_actions.pop(chat_id)

            fila_comandos.put(
                {
                    "acao": acao_confirmada,
                    "target": None,
                    "argumento": None
                }
            )

            await update.message.reply_text(
                f"Comando confirmado, Senhor. "
                f"Executando: {acao_confirmada}."
            )

            return

        elif decisao == "cancelar":

            pending_actions.pop(chat_id)

            await update.message.reply_text(
                "Operação cancelada, Senhor."
            )

            return

        else:

            await update.message.reply_text(
                "Preciso de uma confirmação clara, Senhor. "
                "Diga algo como 'pode executar' para confirmar "
                "ou 'cancela' para abortar."
            )

            return

    # ========================================================
    # ANALISAR COMANDO
    # ========================================================

    analise = analisar_intencao(
        texto,
        chat_id
    )

    intent = analise.get("intent")
    target = analise.get("target")
    argumento = analise.get("argumento")
    risk = analise.get("risk")

    # ========================================================
    # AÇÕES CRÍTICAS
    # ========================================================

    if risk == "vermelho":

        pending_actions[chat_id] = intent

        if intent == "restart":

            mensagem = (
                "⚠️ Senhor, o comando solicita a "
                "REINICIALIZAÇÃO do computador.\n\n"
                "Deseja realmente executar essa ação?"
            )

        elif intent == "shutdown":

            mensagem = (
                "⚠️ Senhor, o comando solicita o "
                "DESLIGAMENTO do computador.\n\n"
                "Deseja realmente executar essa ação?"
            )

        else:

            mensagem = (
                f"⚠️ Ação crítica detectada: {intent}.\n\n"
                "Deseja realmente executar?"
            )

        await update.message.reply_text(
            mensagem
        )

        return

    # ========================================================
    # AÇÕES DO AGENTE LOCAL
    # ========================================================

    if intent in [
        "open_app",
        "open_and_search",
        "send_whatsapp",
        "media_control",
        "set_volume",
        "lock_screen"
    ]:

        fila_comandos.put(
            {
                "acao": intent,
                "target": target,
                "argumento": argumento
            }
        )

        await update.message.reply_text(
            "Comando processado para a máquina local, Senhor."
        )

    # ========================================================
    # PESQUISA NA WEB
    # ========================================================

    elif intent == "web_search":

        mensagem_status = await update.message.reply_text(
            f"🔍 Buscando informações sobre: "
            f"*{argumento}*...",
            parse_mode="Markdown"
        )

        try:

            resultados = DDGS().text(
                argumento,
                max_results=3
            )

            contexto_busca = "\n".join(
                [
                    f"- {r['title']}: {r['body']}"
                    for r in resultados
                ]
            )

            prompt_resposta = f"""
Responda ao usuário como J.A.R.V.I.S.

Use apenas as informações desta pesquisa recente:

{contexto_busca}

Pergunta do usuário:

{texto}
"""

            resp = client.chat.completions.create(

                model="qwen/qwen3.6-27b",

                messages=[
                    {
                        "role": "user",
                        "content": prompt_resposta
                    }
                ]
            )

            resposta_texto = (
                resp.choices[0].message.content
                if hasattr(
                    resp.choices[0].message,
                    "content"
                )
                else ""
            )

            await mensagem_status.edit_text(
                resposta_texto,
                parse_mode="Markdown"
            )

        except Exception as e:

            logger.error(
                f"Erro na pesquisa web: {e}"
            )

            await mensagem_status.edit_text(
                "Desculpe, Senhor. "
                "Meus protocolos de acesso à rede falharam."
            )

    # ========================================================
    # BATE-PAPO
    # ========================================================

    elif intent == "chat":

        await update.message.reply_text(
            argumento,
            parse_mode="Markdown"
        )

    # ========================================================
    # FALLBACK
    # ========================================================

    else:

        await update.message.reply_text(
            "Desculpe, Senhor. "
            "Minha lógica central não conseguiu "
            "processar esse comando."
        )


# ============================================================
# CICLO DE VIDA
# ============================================================

@app.on_event("startup")
async def startup_event():

    global telegram_app

    if TELEGRAM_TOKEN:

        telegram_app = (
            ApplicationBuilder()
            .token(TELEGRAM_TOKEN)
            .build()
        )

        telegram_app.add_handler(
            CommandHandler("start", start)
        )

        telegram_app.add_handler(
            MessageHandler(
                filters.TEXT & (~filters.COMMAND),
                handle_message
            )
        )

        await telegram_app.initialize()

        await telegram_app.start()

        await telegram_app.updater.start_polling()


@app.on_event("shutdown")
async def shutdown_event():

    global telegram_app

    if telegram_app:

        await telegram_app.updater.stop()

        await telegram_app.stop()

        await telegram_app.shutdown()


# ============================================================
# ROTAS FASTAPI
# ============================================================

@app.get("/")
def home():

    return {
        "status":
        "Jarvis Core com Inteligência Emocional "
        "e Controles Totais Online!"
    }


@app.get("/pegar-comando")
def pegar_comando():

    if not fila_comandos.empty():

        return fila_comandos.get()

    return {
        "status": "vazio"
    }
