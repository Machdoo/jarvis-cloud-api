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
from duckduckgo_search import DDGS


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

# Fila utilizada pelo agente local do PC
fila_comandos = queue.Queue()

# Ações que aguardam confirmação
pending_actions = {}

# Contexto de conversa temporário por usuário
user_context = {}

# Histórico geral reservado para futuras expansões/logs
historico_logs = []


# Tempo máximo que uma confirmação fica pendente
CONFIRMATION_TIMEOUT = 300  # 5 minutos


# ============================================================
# DEFINIÇÃO CENTRAL DAS AÇÕES LOCAIS
# ============================================================
#
# Esta estrutura facilita a futura criação de Skills.
#
# Exemplos futuros:
# - BrowserSkill
# - MusicSkill
# - SystemSkill
# - PhoneSkill
# - GlassesSkill
# - VisionSkill
#
# O agente atual continua recebendo apenas as ações que já
# conhece.
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
# GOOGLE SHEETS — MEMÓRIA PERMANENTE
# ============================================================

planilha_memoria = None

if GOOGLE_CREDENTIALS:
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)

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
    """
    Carrega a memória permanente do usuário.
    """

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


def salvar_memoria(categoria, informacao):
    """
    Salva uma nova informação importante na memória permanente.
    """

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

def obter_contexto_conversa(chat_id):
    """
    Retorna as últimas mensagens da conversa.
    """

    historico = user_context.get(
        chat_id,
        []
    )

    if not historico:
        return "Nenhuma conversa anterior disponível."

    linhas = []

    for item in historico[-10:]:

        papel = item.get("role")
        conteudo = item.get("content")

        if papel == "user":
            nome = "Gustavo"

        else:
            nome = "J.A.R.V.I.S."

        linhas.append(
            f"{nome}: {conteudo}"
        )

    return "\n".join(linhas)


def adicionar_contexto(
    chat_id,
    role,
    content
):
    """
    Adiciona uma mensagem ao contexto temporário.
    """

    if chat_id not in user_context:
        user_context[chat_id] = []

    user_context[chat_id].append(
        {
            "role": role,
            "content": content
        }
    )

    # Mantém apenas uma janela recente
    user_context[chat_id] = (
        user_context[chat_id][-20:]
    )


# ============================================================
# NORMALIZAÇÃO DE TEXTO
# ============================================================

def normalizar_texto(texto):
    """
    Remove acentos, pontuação desnecessária e normaliza espaços.
    """

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

def resposta_e_confirmacao(texto):
    """
    Decide deterministicamente se a resposta é confirmação,
    cancelamento ou ambígua.

    Não usa IA para confirmar ações críticas.
    """

    texto_normalizado = normalizar_texto(texto)

    # --------------------------------------------------------
    # CANCELAMENTOS
    # --------------------------------------------------------

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

    # Frases claramente negativas
    if (
        texto_normalizado.startswith("nao ")
        or texto_normalizado.startswith("nao,")
    ):
        return "cancelar"

    # --------------------------------------------------------
    # CONFIRMAÇÕES
    # --------------------------------------------------------

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

    # Algumas variações naturais
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
# IA — ANÁLISE DE INTENÇÃO
# ============================================================

def analisar_intencao(
    texto_usuario: str,
    chat_id: int
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

Seu objetivo é compreender naturalmente o que o Senhor Gustavo
quer fazer e identificar a melhor intenção para a mensagem.

Você não deve tratar toda mensagem como um comando de computador.
Gustavo também pode simplesmente conversar, fazer perguntas,
pedir explicações ou pedir pesquisas.

============================================================
MEMÓRIA PERMANENTE
============================================================

{memoria_atual}

============================================================
CONTEXTO RECENTE DA CONVERSA
============================================================

{contexto}

============================================================
INTENÇÕES DISPONÍVEIS
============================================================

open_app
→ Abrir aplicativo ou site.

open_and_search
→ Abrir um site/app e fazer uma pesquisa.

send_whatsapp
→ Enviar mensagem pelo WhatsApp.

media_control
→ Controlar mídia.

set_volume
→ Alterar volume do PC.

restart
→ Reiniciar o PC.

shutdown
→ Desligar o PC.

lock_screen
→ Bloquear a sessão do Windows.

chat
→ Conversar normalmente, responder perguntas gerais,
explicar conceitos, discutir assuntos ou simplesmente bater papo.

web_search
→ Pesquisar informações na internet quando isso for necessário.

unknown
→ Quando nenhuma intenção puder ser identificada com segurança.

============================================================
REGRAS
============================================================

1. Se Gustavo estiver apenas conversando ou fazendo uma
pergunta geral, use "chat".

2. Se a resposta depender de informação atual, recente,
preço, notícia, evento, pessoa atual, lançamento ou algo que
possa ter mudado, use "web_search".

3. Se Gustavo disser explicitamente:
"pesquise", "procure na internet", "veja na web",
"pesquisa isso", etc., use "web_search".

4. restart e shutdown são ações críticas.
Sempre use:

"risk": "vermelho"

5. lock_screen NÃO precisa de confirmação.
Use:

"risk": "verde"

6. Para set_volume, argumento deve ser um número de 0 a 100.

7. Para media_control, target deve ser um dos seguintes:

"play_pause"
"next"
"prev"

8. Para open_app, target deve identificar o app/site.

9. Para open_and_search:
target = plataforma
argumento = termo pesquisado.

10. Para chat, NÃO invente uma resposta dentro do campo
"argumento". Apenas classifique como "chat".
A resposta será gerada por outra etapa da IA.

11. Para web_search, argumento deve conter exatamente
o assunto/consulta que precisa ser pesquisado.

12. Identifique fatos realmente úteis e relativamente
permanentes sobre Gustavo em "new_fact".

13. Se não houver fato novo:

"new_fact": null

============================================================
FORMATO OBRIGATÓRIO
============================================================

Retorne APENAS JSON válido:

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

        resultado = json.loads(
            response.choices[0].message.content
        )

        novo_fato = resultado.get(
            "new_fact"
        )

        if novo_fato:

            salvar_memoria(
                novo_fato.get(
                    "categoria"
                ),
                novo_fato.get(
                    "informacao"
                )
            )

        return resultado

    except Exception as e:

        logger.error(
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
# IA — CONVERSAÇÃO
# ============================================================

def gerar_resposta_chat(
    texto_usuario,
    chat_id
):
    """
    Gera a resposta natural do JARVIS para conversas,
    dúvidas e explicações.
    """

    if not client:
        return (
            "Desculpe, Senhor. "
            "Meu núcleo de inteligência está indisponível no momento."
        )

    memoria_atual = carregar_memoria()

    contexto = obter_contexto_conversa(
        chat_id
    )

    prompt = f"""
Você é J.A.R.V.I.S., o assistente pessoal do Senhor Gustavo.

Responda naturalmente à mensagem dele.

Você deve conversar de forma inteligente, clara e humana,
mantendo a personalidade de um assistente pessoal avançado.

Não finja ter executado ações que não executou.

Quando não souber algo, seja honesto.

Não precisa mencionar que existe um classificador de intenção.

============================================================
MEMÓRIA
============================================================

{memoria_atual}

============================================================
CONTEXTO RECENTE
============================================================

{contexto}

============================================================
MENSAGEM DO USUÁRIO
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

        return resposta or (
            "Desculpe, Senhor. "
            "Não consegui formular uma resposta agora."
        )

    except Exception as e:

        logger.error(
            f"Erro ao gerar resposta de chat: {e}"
        )

        return (
            "Desculpe, Senhor. "
            "Meu núcleo de conversação apresentou uma falha."
        )


# ============================================================
# PESQUISA WEB
# ============================================================

def pesquisar_web(consulta):
    """
    Executa uma pesquisa web.
    """

    try:

        resultados = DDGS().text(
            consulta,
            max_results=5
        )

        return resultados

    except Exception as e:

        logger.error(
            f"Erro na pesquisa web: {e}"
        )

        return []


def gerar_resposta_pesquisa(
    texto_usuario,
    consulta,
    resultados,
    chat_id
):
    """
    Faz a IA analisar os resultados e formular a resposta.
    """

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
            "Senhor, não encontrei resultados confiáveis "
            "para essa pesquisa."
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
Você é J.A.R.V.I.S., assistente pessoal do Senhor Gustavo.

Responda à pergunta dele usando os resultados da pesquisa
abaixo como fonte principal.

Não invente fatos que não estejam sustentados pelos resultados.

Se os resultados forem insuficientes ou conflitantes,
deixe isso claro.

============================================================
MEMÓRIA DO USUÁRIO
============================================================

{memoria_atual}

============================================================
CONTEXTO RECENTE
============================================================

{contexto}

============================================================
PESQUISA REALIZADA
============================================================

Consulta:
{consulta}

Resultados:

{contexto_busca}

============================================================
PERGUNTA DO USUÁRIO
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

        return resposta or (
            "Senhor, encontrei resultados, "
            "mas não consegui montar a resposta."
        )

    except Exception as e:

        logger.error(
            f"Erro ao interpretar pesquisa: {e}"
        )

        return (
            "Senhor, a pesquisa foi realizada, "
            "mas houve um erro ao analisar os resultados."
        )


# ============================================================
# ENFILEIRAR COMANDO LOCAL
# ============================================================

def enviar_para_agente(
    intent,
    target=None,
    argumento=None
):
    """
    Centraliza o envio de comandos para o agente local.

    Isso facilita futuras Skills.
    """

    comando = {
        "acao": intent,
        "target": target,
        "argumento": argumento
    }

    fila_comandos.put(
        comando
    )

    logger.info(
        f"Comando enviado ao agente local: {comando}"
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
# TELEGRAM — PROCESSAMENTO
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global pending_actions

    if not update.message:
        return

    if not update.message.text:
        return

    texto = update.message.text.strip()

    chat_id = update.effective_chat.id

    # Salva a mensagem no contexto da conversa
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

        # Expira confirmação antiga
        if (
            time.time() - criado_em
            > CONFIRMATION_TIMEOUT
        ):

            pending_actions.pop(
                chat_id,
                None
            )

            await update.message.reply_text(
                "Senhor, essa solicitação de confirmação expirou. "
                "Se ainda desejar executar a ação, envie o comando novamente."
            )

            return

        decisao = resposta_e_confirmacao(
            texto
        )

        # ----------------------------------------------------
        # CONFIRMAÇÃO
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # CANCELAMENTO
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # AMBÍGUO
        # ----------------------------------------------------

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

    # Normalização básica
    if intent:
        intent = str(intent).strip().lower()

    # ========================================================
    # SEGURANÇA DETERMINÍSTICA
    # ========================================================
    #
    # Não dependemos somente do campo "risk" da IA.
    #
    # Restart e shutdown SEMPRE pedem confirmação.
    # Lock screen NUNCA pede confirmação.
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

        elif intent == "shutdown":

            resposta = (
                "⚠️ Senhor, o comando solicita o "
                "DESLIGAMENTO do computador.\n\n"
                "Deseja realmente executar essa ação?"
            )

        else:

            resposta = (
                f"⚠️ Senhor, a ação "
                f"{intent} requer confirmação.\n\n"
                f"Deseja executar?"
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
    # AÇÕES DO AGENTE LOCAL
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
            f"🔍 Buscando informações sobre:\n{consulta}"
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
    # CONVERSA NORMAL
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

    # Se até uma mensagem "unknown" tiver sido algo que
    # o classificador não entendeu, ainda tentamos conversar
    # em vez de simplesmente responder "não entendi".
    adicionar_contexto(
        chat_id,
        "assistant",
        resposta
    )

    await update.message.reply_text(
        resposta
    )


# ============================================================
# CICLO DE VIDA — STARTUP
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
# CICLO DE VIDA — SHUTDOWN
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
