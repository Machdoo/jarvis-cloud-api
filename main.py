import os
import asyncio
from fastapi import FastAPI
from pydantic import BaseModel
import queue
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# --- CONFIGURAÇÃO ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") # Vamos configurar isso no Render
fila_comandos = queue.Queue()

app = FastAPI()

# --- LÓGICA DO TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="J.A.R.V.I.S. Online e pronto, Senhor!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.lower()
    
    # Exemplo simples: Se a mensagem contiver "abre", enviamos o comando
    if "abre" in texto:
        # Pega a última palavra como argumento (ex: "abre instagram" -> "instagram")
        arg = texto.split(" ")[-1]
        fila_comandos.put({"acao": "open_app", "argumento": arg})
        await update.message.reply_text(f"Comando enviado ao seu computador, Senhor: Abrir {arg}")
    else:
        await update.message.reply_text("Entendido, mas não sei como processar este comando ainda.")

# --- INICIALIZAÇÃO ---
@app.on_event("startup")
async def startup_event():
    # Inicia o bot em segundo plano
    if TELEGRAM_TOKEN:
        bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        bot_app.add_handler(CommandHandler("start", start))
        bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
        asyncio.create_task(bot_app.run_polling())

# --- ENDPOINTS ---
@app.get("/pegar-comando")
def pegar_comando():
    if not fila_comandos.empty():
        return fila_comandos.get()
    return {"status": "vazio"}
