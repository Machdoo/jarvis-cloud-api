import os
import json
import logging
from fastapi import FastAPI
from pydantic import BaseModel
import queue
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from groq import Groq

# --- CONFIGURAÇÃO DE LOGS ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("JARVIS_CORE")

# --- CREDENCIAIS ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") 

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
fila_comandos = queue.Queue()
pending_actions = {} 
historico_logs = []

# Dicionário para armazenar o contexto recente de cada chat
user_context = {}

app = FastAPI()
telegram_app = None

# --- MOTOR DE INTENÇÃO E CONTEXTO (GROQ) ---
def analisar_intencao(texto_usuario: str, chat_id: int):
    if not client:
        return {"intent": "unknown", "target": None, "risk": "verde"}
    
    # Recupera o contexto anterior do usuário, se existir
    contexto_anterior = user_context.get(chat_id, {"last_intent": None, "last_target": None})
    
    prompt = f"""
    Você é o cérebro do assistente J.A.R.V.I.S. 
    Contexto da conversa anterior com este usuário:
    - Última intenção: {contexto_anterior.get('last_intent')}
    - Último alvo/aplicativo: {contexto_anterior.get('last_target')}

    Analise o novo comando do usuário considerando o contexto acima. Se o comando fizer referência ao que estava aberto antes (ex: pesquisar algo dentro da plataforma anterior), vincule-o corretamente.
    
    Retorne APENAS um JSON válido com a estrutura exata:
    {{
      "intent": "open_app" | "search" | "restart" | "shutdown" | "unknown",
      "target": "nome do aplicativo, site ou termo de pesquisa se houver, senão null",
      "risk": "verde" ou "vermelho"
    }}
    
    Comando atual: "{texto_usuario}"
    """
    
    try:
        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",  # Modelo validado e disponível na sua conta
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        resultado = json.loads(response.choices[0].message.content)
        
        # Atualiza a memória de contexto do chat se a intenção for válida
        if resultado.get("intent") != "unknown":
            user_context[chat_id] = {
                "last_intent": resultado.get("intent"),
                "last_target": resultado.get("target")
            }
            
        return resultado
    except Exception as e:
        logger.error(f"Erro na análise de contexto da IA: {e}")
        return {"intent": "unknown", "target": None, "risk": "verde"}

# --- LÓGICA DO TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="J.A.R.V.I.S. Core Online com Motor de Contexto Ativo, Senhor.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_actions, historico_logs
    texto = update.message.text
    chat_id = update.effective_chat.id

    logger.info(f"Comando recebido do Telegram [{chat_id}]: {texto}")

    # 1. VERIFICAÇÃO DE CONFIRMAÇÃO PENDENTE (RISCO VERMELHO)
    if chat_id in pending_actions:
        if "sim" in texto.lower() or "confirmo" in texto.lower():
            acao_confirmada = pending_actions.pop(chat_id)
            fila_comandos.put({"acao": acao_confirmada, "argumento": None})
            
            log_msg = f"[EXECUTADO] Ação crítica confirmada: {acao_confirmada}"
            historico_logs.append(log_msg)
            logger.info(log_msg)
            
            await update.message.reply_text(f"Comando confirmado, Senhor. Executando: {acao_confirmada}.")
        else:
            pending_actions.pop(chat_id)
            await update.message.reply_text("Operação cancelada, Senhor.")
        return

    # 2. ANÁLISE DE INTENÇÃO COM BASE NO CONTEXTO
    analise = analisar_intencao(texto, chat_id)
    intent = analise.get("intent")
    target = analise.get("target")
    risk = analise.get("risk")

    logger.info(f"Intenção: {intent} | Alvo: {target} | Risco: {risk} | Contexto Ativo: {user_context.get(chat_id)}")

    # 3. AVALIAÇÃO DE SEGURANÇA
    if risk == "vermelho":
        pending_actions[chat_id] = intent
        await update.message.reply_text(f"⚠️ Alerta de Segurança: Ação crítica ({intent}) identificada. Responda 'sim' para confirmar.")
        return

    # 4. EXECUÇÃO DE SKILLS (Com normalização de intenções da IA)
    intent_normalizada = intent.lower() if intent else "unknown"
    
    if intent_normalizada in ["open_app", "open", "abrir", "iniciar", "launch"]:
        intent_normalizada = "open_app"

    if intent_normalizada == "open_app" or intent_normalizada == "search":
        if target:
            fila_comandos.put({"acao": intent_normalizada, "argumento": target})
            log_msg = f"[EXECUTADO] Skill {intent_normalizada} acionada para: {target}"
            historico_logs.append(log_msg)
            await update.message.reply_text(f"Processando comando: {target}, Senhor.")
        else:
            await update.message.reply_text("Não consegui identificar o alvo da ação, Senhor.")
            
    elif intent_normalizada == "restart":
        pending_actions[chat_id] = "restart"
        await update.message.reply_text("⚠️ Tem certeza que deseja REINICIAR o computador? Responda 'sim' para confirmar.")
        
    elif intent_normalizada == "shutdown":
        pending_actions[chat_id] = "shutdown"
        await update.message.reply_text("⚠️ Tem certeza que deseja DESLIGAR o computador? Responda 'sim' para confirmar.")
        
    else:
        historico_logs.append(f"[FALHA] Comando não compreendido: {texto}")
        await update.message.reply_text("Intenção não reconhecida pelo núcleo do J.A.R.V.I.S., Senhor.")

# --- CICLO DE VIDA E ENDPOINTS ---
@app.on_event("startup")
async def startup_event():
    global telegram_app
    if TELEGRAM_TOKEN:
        telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
        
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

@app.get("/")
def home():
    return {"status": "Jarvis Core Cloud com Contexto Ativo, Senhor!"}

@app.get("/pegar-comando")
def pegar_comando():
    if not fila_comandos.empty():
        return fila_comandos.get()
    return {"status": "vazio"}

@app.get("/logs")
def ver_logs():
    return {"historico": historico_logs[-10:]}
