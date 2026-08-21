import os
import json
import logging
from fastapi import FastAPI
import queue
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from groq import Groq

# --- CONFIGURAÇÃO ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("JARVIS_CORE")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") 

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
fila_comandos = queue.Queue()
pending_actions = {} 
app = FastAPI()
telegram_app = None

# --- MOTOR DE INTENÇÃO (GROQ) ---
def analisar_intencao(texto_usuario: str):
    if not client:
        return {"intent": "unknown", "target": None, "argumento": None, "risk": "verde"}
    
    prompt = f"""
    Analise o comando do usuário e retorne um JSON com:
    - "intent": "open_app", "open_and_search", "send_whatsapp", "restart", "shutdown", "unknown"
    - "target": o aplicativo ou site (ex: "youtube", "spotify", "whatsapp").
    - "argumento": o termo de pesquisa ou a mensagem a ser enviada.
    - "risk": "verde" ou "vermelho".

    Comando: "{texto_usuario}"
    """
    
    try:
        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Erro IA: {e}")
        return {"intent": "unknown", "target": None, "argumento": None, "risk": "verde"}

# --- LÓGICA TELEGRAM ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    chat_id = update.effective_chat.id
    
    analise = analisar_intencao(texto)
    intent = analise.get("intent")
    target = analise.get("target")
    arg = analise.get("argumento")

    if intent in ["open_app", "open_and_search", "send_whatsapp"]:
        fila_comandos.put({"acao": intent, "target": target, "argumento": arg})
        await update.message.reply_text(f"Comando '{intent}' processado para {target}, Senhor.")
    else:
        await update.message.reply_text("Comando não compreendido ou não mapeado.")

@app.on_event("startup")
async def startup():
    global telegram_app
    telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()

@app.get("/pegar-comando")
def pegar_comando():
    return fila_comandos.get() if not fila_comandos.empty() else {"status": "vazio"}
