import os
import json
import logging
from fastapi import FastAPI
from pydantic import BaseModel
import queue
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from openai import OpenAI

# --- CONFIGURAÇÃO DE LOGS ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("JARVIS_CORE")

# --- CREDENCIAIS ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") # Vamos configurar no Render

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
fila_comandos = queue.Queue()
pending_actions = {} 
historico_logs = []

app = FastAPI()
telegram_app = None

# --- MOTOR DE INTENÇÃO E RISCO (LLM) ---
def analisar_intencao(texto_usuario: str):
    if not client:
        # Fallback de segurança caso a chave da IA não esteja configurada
        return {"intent": "unknown", "target": None, "risk": "verde"}
    
    prompt = f"""
    Você é o cérebro do assistente J.A.R.V.I.S. Analise o comando do usuário e retorne APENAS um JSON válido com a estrutura:
    {{
      "intent": "open_app" | "restart" | "shutdown" | "unknown",
      "target": "nome do aplicativo ou site se houver, senão null",
      "risk": "verde" (para ações seguras como abrir apps), "vermelho" (para desligar/reiniciar PC)
    }}
    
    Comando do usuário: "{texto_usuario}"
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo", # ou gpt-4o-mini
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        resultado = json.loads(response.choices[0].message.content)
        return resultado
    except Exception as e:
        logger.error(f"Erro na análise da IA: {e}")
        return {"intent": "unknown", "target": None, "risk": "verde"}

# --- LÓGICA DO TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="J.A.R.V.I.S. Core Online (Fase 1 Ativa), Senhor. Às suas ordens.")

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

    # 2. ANÁLISE DE INTENÇÃO VIA LLM
    analise = analisar_intencao(texto)
    intent = analise.get("intent")
    target = analise.get("target")
    risk = analise.get("risk")

    logger.info(f"Intenção detectada: {intent} | Alvo: {target} | Risco: {risk}")

    # 3. AVALIAÇÃO DE SEGURANÇA (POLICY ENGINE)
    if risk == "vermelho":
        pending_actions[chat_id] = intent
        await update.message.reply_text(f"⚠️ Alerta de Segurança: Ação crítica ({intent}) identificada. Responda 'sim' para confirmar.")
        return

    # 4. EXECUÇÃO DE SKILLS (RISCO VERDE)
    if intent == "open_app":
        if target:
            fila_comandos.put({"acao": "open_app", "argumento": target})
            log_msg = f"[EXECUTADO] Skill open_app acionada para: {target}"
            historico_logs.append(log_msg)
            await update.message.reply_text(f"Comando enviado ao computador: Abrir {target}")
        else:
            await update.message.reply_text("Não consegui identificar qual aplicativo abrir, Senhor.")
            
    elif intent == "restart":
        pending_actions[chat_id] = "restart"
        await update.message.reply_text("⚠️ Tem certeza que deseja REINICIAR o computador? Responda 'sim' para confirmar.")
        
    elif intent == "shutdown":
        pending_actions[chat_id] = "shutdown"
        await update.message.reply_text("⚠️ Tem certeza que deseja DESLIGAR o computador? Responda 'sim' para confirmar.")
        
    else:
        historico_logs.append(f"[FALHA] Comando não compreendido: {texto}")
        await update.message.reply_text("Intenção não reconhecida pelo núcleo do J.A.R.V.I.S., Senhor.")

# --- EVENTOS DE CICLO DE VIDA ---
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

# --- ENDPOINTS ---
@app.get("/")
def home():
    return {"status": "Jarvis Core Cloud (Fase 1) online, Senhor!"}

@app.get("/pegar-comando")
def pegar_comando():
    if not fila_comandos.empty():
        return fila_comandos.get()
    return {"status": "vazio"}

@app.get("/logs")
def ver_logs():
    return {"historico": historico_logs[-10:]} # Retorna os últimos 10 logs do sistema
