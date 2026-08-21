import os
from fastapi import FastAPI
from pydantic import BaseModel
import queue
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# --- CONFIGURAÇÃO ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
fila_comandos = queue.Queue()

# Dicionário para gerenciar confirmações pendentes por usuário
# (Isso garante que ele saiba quem está respondendo ao pedido de confirmação)
pending_actions = {} 

app = FastAPI()
telegram_app = None

# --- LÓGICA DO TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="J.A.R.V.I.S. Online, Senhor. Às suas ordens.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_actions
    texto = update.message.text.lower()
    chat_id = update.effective_chat.id

    # 1. VERIFICAÇÃO DE CONFIRMAÇÃO PENDENTE
    if chat_id in pending_actions:
        if "sim" in texto or "confirmo" in texto:
            acao_confirmada = pending_actions.pop(chat_id)
            fila_comandos.put({"acao": acao_confirmada, "argumento": None})
            await update.message.reply_text(f"Comando confirmado, Senhor. Executando: {acao_confirmada}.")
        else:
            pending_actions.pop(chat_id)
            await update.message.reply_text("Operação cancelada, Senhor.")
        return

    # 2. PROCESSAMENTO DE NOVOS COMANDOS
    # Comandos para abrir aplicativos
    if "abre" in texto or "abrir" in texto:
        arg = texto.split(" ")[-1]
        fila_comandos.put({"acao": "open_app", "argumento": arg})
        await update.message.reply_text(f"Comando enviado: Abrir {arg}")
        
    # Comando para reiniciar (Com confirmação)
    elif "reinicia" in texto or "reiniciar" in texto:
        pending_actions[chat_id] = "restart"
        await update.message.reply_text("⚠️ Tem certeza que deseja REINICIAR o computador? Responda 'sim' para confirmar.")
        
    # Comando para desligar (Com confirmação)
    elif "desliga" in texto or "desligar" in texto:
        pending_actions[chat_id] = "shutdown"
        await update.message.reply_text("⚠️ Tem certeza que deseja DESLIGAR o computador? Responda 'sim' para confirmar.")
        
    else:
        await update.message.reply_text("Não compreendi o comando, Senhor.")

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
    return {"status": "Jarvis Cloud está online, Senhor!"}

@app.get("/pegar-comando")
def pegar_comando():
    if not fila_comandos.empty():
        return fila_comandos.get()
    return {"status": "vazio"}
