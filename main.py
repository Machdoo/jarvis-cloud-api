import os
from fastapi import FastAPI
from pydantic import BaseModel
import queue
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# --- CONFIGURAÇÃO ---
# Certifique-se de que a variável TELEGRAM_TOKEN esteja configurada no painel do Render
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
fila_comandos = queue.Queue()

app = FastAPI()

# Variável global para gerenciar o bot
telegram_app = None

# --- LÓGICA DO TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="J.A.R.V.I.S. Online, Senhor. Às suas ordens.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.lower()
    
    # Comandos para abrir aplicativos
    if "abre" in texto or "abrir" in texto:
        arg = texto.split(" ")[-1]
        fila_comandos.put({"acao": "open_app", "argumento": arg})
        await update.message.reply_text(f"Comando enviado: Abrir {arg}")
        
    # Comando para reiniciar
    elif "reinicia" in texto or "reiniciar" in texto:
        fila_comandos.put({"acao": "restart", "argumento": None})
        await update.message.reply_text("Comando enviado: Reiniciando o sistema.")
        
    # Comando para desligar
    elif "desliga" in texto or "desligar" in texto:
        fila_comandos.put({"acao": "shutdown", "argumento": None})
        await update.message.reply_text("Comando enviado: Desligando o computador.")
        
    else:
        await update.message.reply_text("Não compreendi o comando, Senhor.")

# --- EVENTOS DE INICIALIZAÇÃO ---
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
    return {"status": "Jarvis Cloud está online, Senhor!"}

@app.get("/pegar-comando")
def pegar_comando():
    # Retorna o comando se houver, senão retorna um status indicando vazio
    if not fila_comandos.empty():
        return fila_comandos.get()
    return {"status": "vazio"}
