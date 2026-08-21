import os
from fastapi import FastAPI
from pydantic import BaseModel
import queue
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# --- CONFIGURAÇÃO ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
fila_comandos = queue.Queue()

app = FastAPI()

# Variável global para gerenciar o bot
telegram_app = None

# --- LÓGICA DO TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="J.A.R.V.I.S. Online e pronto, Senhor!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.lower()
    
    if "abre" in texto:
        arg = texto.split(" ")[-1]
        fila_comandos.put({"acao": "open_app", "argumento": arg})
        await update.message.reply_text(f"Comando enviado ao seu computador, Senhor: Abrir {arg}")
    else:
        await update.message.reply_text("Entendido, mas não sei como processar este comando ainda.")

# --- EVENTOS DE INICIALIZAÇÃO E ENCERRAMENTO DO FASTAPI ---
@app.on_event("startup")
async def startup_event():
    global telegram_app
    if TELEGRAM_TOKEN:
        telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
        
        # Inicializa e inicia o bot de forma segura no loop do FastAPI
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
    return {"status": "Jarvis Cloud está online, Senhor!"}

@app.get("/pegar-comando")
def pegar_comando():
    if not fila_comandos.empty():
        return fila_comandos.get()
    return {"status": "vazio"}
