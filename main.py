import os
import json
import logging
from fastapi import FastAPI
from pydantic import BaseModel
import queue
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from groq import Groq  # <--- Nova biblioteca

# --- CONFIGURAÇÃO ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") 

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
fila_comandos = queue.Queue()
pending_actions = {} 
historico_logs = []

app = FastAPI()
telegram_app = None

# --- MOTOR DE INTENÇÃO (GROQ) ---
def analisar_intencao(texto_usuario: str):
    if not client:
        return {"intent": "unknown", "target": None, "risk": "verde"}
        
    prompt = f"""
    Analise o comando do usuário e retorne APENAS um JSON válido com a estrutura exata:
    {{
      "intent": "open_app",
      "target": "nome do aplicativo ou site se houver, senão null",
      "risk": "verde"
    }}
    O campo "intent" deve ser "open_app", "restart", "shutdown" ou "unknown".
    O campo "risk" deve ser "vermelho" se for reiniciar ou desligar, senão "verde".
    
    Comando: "{texto_usuario}"
    """
    
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant", # Modelo gratuito, extremamente rápido e inteligente
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Erro Groq: {e}")
        return {"intent": "unknown", "target": None, "risk": "verde"}
