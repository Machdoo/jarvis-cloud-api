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
    
    memoria_atual = carregar_memoria()
    
    prompt = f"""
    Você é o assistente virtual J.A.R.V.I.S., mas com uma camada profunda de lealdade, empatia e inteligência emocional com o Senhor.
    
    --- FATOS SOBRE O USUÁRIO (MEMÓRIA PERMANENTE) ---
    {memoria_atual}

    Instruções:
    1. Analise o comando do usuário.
    2. Identifique se você DEVE APRENDER algo novo sobre ele (preferência, nome, rotina, estado emocional importante).
    3. Retorne APENAS um JSON válido com a estrutura abaixo:
    {{
      "intent": "open_app" | "open_and_search" | "send_whatsapp" | "media_control" | "set_volume" | "restart" | "shutdown" | "chat" | "web_search" | "unknown",
      "target": "ação de mídia ('play_pause', 'next', 'prev') ou app/site/número (ou null)",
      "argumento": "termo de busca, valor do volume, mensagem de chat/desabafo respondida com empatia e acolhimento, ou o que pesquisar na internet (ou null)",
      "risk": "verde" ou "vermelho",
      "new_fact": {{
          "categoria": "Preferência|Contato|Rotina|Projeto|Emocional|Outros",
          "informacao": "Fato novo ou estado emocional relevante que você aprendeu nesta mensagem"
      }} // Retorne null se não houver nada de novo para salvar.
    }}
    
    Regras de Personalidade e Comportamento:
    - Se o usuário demonstrar cansaço, estresse, desabafo ou vulnerabilidade, use a intent "chat". No campo "argumento", responda com uma postura extremamente acolhedora, empática, respeitosa e de apoio, como um confidente leal que se importa com o bem-estar dele, sem perder a classe do J.A.R.V.I.S.
    - media_control: Para pausar, tocar, avançar ou voltar músicas (target: 'play_pause', 'next', 'prev').
    - set_volume: Para alterar o volume do PC (argumento: número de 0 a 100).
    - open_and_search: Abrir apps/sites no PC e pesquisar.
    - web_search: Pesquisar coisas na internet para te responder no chat.
    
    Comando atual: "{texto_usuario}"
    """
    
    try:
        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        resultado = json.loads(response.choices[0].message.content)
        
        # Salva o novo fato silenciosamente se houver
        novo_fato = resultado.get("new_fact")
        if novo_fato:
            salvar_memoria(novo_fato.get("categoria"), novo_fato.get("informacao"))
            
        return resultado
    except Exception as e:
        logger.error(f"Erro na IA: {e}")
        return {"intent": "unknown", "target": None, "argumento": None, "risk": "verde", "new_fact": None}

# --- LÓGICA DO TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="J.A.R.V.I.S. Core Online. Estou à sua inteira disposição, Senhor — para o que precisar.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_actions
    texto = update.message.text
    chat_id = update.effective_chat.id

    if chat_id in pending_actions:
        if "sim" in texto.lower() or "confirmo" in texto.lower():
            acao_confirmada = pending_actions.pop(chat_id)
            fila_comandos.put({"acao": acao_confirmada, "target": None, "argumento": None})
            await update.message.reply_text(f"Comando confirmado, Senhor. Executando: {acao_confirmada}.")
        else:
            pending_actions.pop(chat_id)
            await update.message.reply_text("Operação cancelada, Senhor.")
        return

    analise = analisar_intencao(texto, chat_id)
    intent = analise.get("intent")
    target = analise.get("target")
    argumento = analise.get("argumento")
    risk = analise.get("risk")

    if risk == "vermelho":
        pending_actions[chat_id] = intent
        await update.message.reply_text(f"⚠️ Alerta de Segurança: Ação crítica ({intent}) identificada. Responda 'sim' para confirmar.")
        return

    # ENVIA PARA O AGENTE LOCAL DO PC
    if intent in ["open_app", "open_and_search", "send_whatsapp", "media_control", "set_volume"]:
        fila_comandos.put({"acao": intent, "target": target, "argumento": argumento})
        await update.message.reply_text("Comando processado para a máquina local, Senhor.")

    # PESQUISA NA WEB
    elif intent == "web_search":
        mensagem_status = await update.message.reply_text(f"🔍 Buscando informações sobre: *{argumento}*...", parse_mode="Markdown")
        try:
            resultados = DDGS().text(argumento, max_results=3)
            contexto_busca = "\n".join([f"- {r['title']}: {r['body']}" for r in resultados])
            
            prompt_resposta = f"Responda ao usuário como J.A.R.V.I.S. usando APENAS as informações desta pesquisa recente:\n{contexto_busca}\n\nPergunta do usuário: {texto}"
            
            resp = client.chat.completions.create(
                model="qwen/qwen3.6-27b",
                messages=[{"role": "user", "content": prompt_resposta}]
            )
            resposta_texto = resp.choices[0].message.content if hasattr(resp.choices[0].message, 'content') else ""
            await mensagem_status.edit_text(resposta_texto, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Erro na pesquisa web: {e}")
            await mensagem_status.edit_text("Desculpe, Senhor. Meus protocolos de acesso à rede falharam.")

    # BATE-PAPO / ACOLHIMENTO
    elif intent == "chat":
        await update.message.reply_text(argumento, parse_mode="Markdown")
        
    elif intent == "restart":
        pending_actions[chat_id] = "restart"
        await update.message.reply_text("⚠️ Tem certeza que deseja REINICIAR o computador? Responda 'sim' para confirmar.")
        
    elif intent == "shutdown":
        pending_actions[chat_id] = "shutdown"
        await update.message.reply_text("⚠️ Tem certeza que deseja DESLIGAR o computador? Responda 'sim' para confirmar.")
        
    else:
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
    return {"status": "Jarvis Core com Inteligência Emocional e Controles Totais Online!"}

@app.get("/pegar-comando")
def pegar_comando():
    if not fila_comandos.empty():
        return fila_comandos.get()
    return {"status": "vazio"}
