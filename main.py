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

# --- CONFIGURAÇÃO DE LOGS ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("JARVIS_CORE")

# --- CREDENCIAIS ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
fila_comandos = queue.Queue()
pending_actions = {} 
historico_logs = []
user_context = {}

app = FastAPI()
telegram_app = None

# --- CONEXÃO COM GOOGLE SHEETS (MEMÓRIA) ---
planilha_memoria = None
if GOOGLE_CREDENTIALS:
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        gc = gspread.service_account_from_dict(creds_dict)
        planilha_memoria = gc.open("JARVIS_Memoria").sheet1
        logger.info("Conectado à Memória Permanente (Google Sheets) com sucesso!")
    except Exception as e:
        logger.error(f"Erro ao conectar no Google Sheets: {e}")

def carregar_memoria():
    if not planilha_memoria: return "Nenhuma memória conectada."
    try:
        registros = planilha_memoria.get_all_records()
        if not registros: return "A memória ainda está vazia."
        
        texto_memoria = ""
        for linha in registros:
            texto_memoria += f"- {linha.get('Categoria', 'Geral')}: {linha.get('Informação', '')}\n"
        return texto_memoria
    except Exception as e:
        logger.error(f"Erro ao ler memória: {e}")
        return "Erro ao acessar memória."

def salvar_memoria(categoria, informacao):
    if not planilha_memoria: return
    try:
        data_atual = datetime.now().strftime("%d/%m/%Y %H:%M")
        planilha_memoria.append_row([data_atual, categoria, informacao])
        logger.info(f"Nova memória salva: {categoria} - {informacao}")
    except Exception as e:
        logger.error(f"Erro ao salvar na memória: {e}")

# --- MOTOR DE INTENÇÃO E CONTEXTO (GROQ) ---
def analisar_intencao(texto_usuario: str, chat_id: int):
    if not client:
        return {"intent": "unknown", "target": None, "argumento": None, "risk": "verde", "new_fact": None}
    
    contexto_anterior = user_context.get(chat_id, {"last_intent": None, "last_target": None})
    memoria_atual = carregar_memoria()
    
    prompt = f"""
    Você é o assistente virtual J.A.R.V.I.S.
    
    --- FATOS SOBRE O USUÁRIO (MEMÓRIA PERMANENTE) ---
    {memoria_atual}
    
    --- CONTEXTO DA CONVERSA ANTERIOR ---
    - Última intenção: {contexto_anterior.get('last_intent')}
    - Último alvo: {contexto_anterior.get('last_target')}

    Instruções:
    1. Analise o comando.
    2. Identifique se você DEVE APRENDER algo novo sobre o usuário (uma preferência, um nome, um projeto, rotina).
    3. Retorne APENAS um JSON válido com a estrutura:
    {{
      "intent": "open_app" | "open_and_search" | "send_whatsapp" | "restart" | "shutdown" | "chat" | "unknown",
      "target": "nome do app, site, ou número (ou null)",
      "argumento": "termo de busca, ou a MENSAGEM que você vai responder se a intent for 'chat' (ou null)",
      "risk": "verde" ou "vermelho",
      "new_fact": {{
          "categoria": "Preferência|Contato|Rotina|Projeto|Outros",
          "informacao": "Fato novo e resumido que você aprendeu nesta mensagem"
      }} // Retorne null se não houver nada de novo para salvar.
    }}
    
    Nota: Se for apenas uma conversa, cumprimento, ou pergunta que você saiba responder, use a intent "chat" e coloque a sua resposta no campo "argumento", falando como o J.A.R.V.I.S. falaria com o Senhor.
    
    Comando atual: "{texto_usuario}"
    """
    
    try:
        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        resultado = json.loads(response.choices[0].message.content)
        
        # Salva o novo fato silenciosamente
        novo_fato = resultado.get("new_fact")
        if novo_fato:
            salvar_memoria(novo_fato.get("categoria"), novo_fato.get("informacao"))
            
        if resultado.get("intent") != "unknown":
            user_context[chat_id] = {
                "last_intent": resultado.get("intent"),
                "last_target": resultado.get("target")
            }
            
        return resultado
    except Exception as e:
        logger.error(f"Erro na IA: {e}")
        return {"intent": "unknown", "target": None, "argumento": None, "risk": "verde", "new_fact": None}

# --- LÓGICA DO TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="J.A.R.V.I.S. Core Online. Acesso à Memória Permanente estabelecido, Senhor.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_actions, historico_logs
    texto = update.message.text
    chat_id = update.effective_chat.id

    # VERIFICAÇÃO DE RISCO (Ações de desligar PC)
    if chat_id in pending_actions:
        if "sim" in texto.lower() or "confirmo" in texto.lower():
            acao_confirmada = pending_actions.pop(chat_id)
            fila_comandos.put({"acao": acao_confirmada, "target": None, "argumento": None})
            historico_logs.append(f"[EXECUTADO] Ação crítica: {acao_confirmada}")
            await update.message.reply_text(f"Comando confirmado, Senhor. Executando: {acao_confirmada}.")
        else:
            pending_actions.pop(chat_id)
            await update.message.reply_text("Operação cancelada, Senhor.")
        return

    # ANÁLISE DE INTENÇÃO
    analise = analisar_intencao(texto, chat_id)
    intent = analise.get("intent")
    target = analise.get("target")
    argumento = analise.get("argumento")
    risk = analise.get("risk")

    if risk == "vermelho":
        pending_actions[chat_id] = intent
        await update.message.reply_text(f"⚠️ Alerta de Segurança: Ação crítica ({intent}) identificada. Responda 'sim' para confirmar.")
        return

    # EXECUÇÃO DE SKILLS DO PC
    if intent in ["open_app", "open_and_search", "send_whatsapp"]:
        fila_comandos.put({"acao": intent, "target": target, "argumento": argumento})
        historico_logs.append(f"[EXECUTADO] {intent} -> Alvo: {target} | Arg: {argumento}")
        await update.message.reply_text("Comando processado para a máquina local, Senhor.")

    # BATE-PAPO COM A IA (NÃO ENVIA PRO PC)
    elif intent == "chat":
        await update.message.reply_text(argumento, parse_mode="Markdown")
        
    elif intent == "restart":
        pending_actions[chat_id] = "restart"
        await update.message.reply_text("⚠️ Tem certeza que deseja REINICIAR o computador? Responda 'sim' para confirmar.")
        
    elif intent == "shutdown":
        pending_actions[chat_id] = "shutdown"
        await update.message.reply_text("⚠️ Tem certeza que deseja DESLIGAR o computador? Responda 'sim' para confirmar.")
        
    else:
        historico_logs.append(f"[FALHA] Comando não compreendido: {texto}")
        await update.message.reply_text("Desculpe Senhor, minha lógica central não conseguiu processar esse comando.")

# --- CICLO DE VIDA ---
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
    return {"status": "Jarvis Core com Memória Neural Online, Senhor!"}

@app.get("/pegar-comando")
def pegar_comando():
    if not fila_comandos.empty():
        return fila_comandos.get()
    return {"status": "vazio"}
