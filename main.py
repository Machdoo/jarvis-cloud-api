from fastapi import FastAPI
from pydantic import BaseModel
import queue

app = FastAPI()

# Fila para guardar os comandos enviados pelo Telegram
fila_comandos = queue.Queue()

class Comando(BaseModel):
    acao: str
    argumento: str = None

@app.get("/")
def home():
    return {"status": "Jarvis Cloud está online, Senhor!"}

@app.post("/enviar-comando")
def enviar_comando(cmd: Comando):
    fila_comandos.put(cmd)
    return {"status": "Comando adicionado à fila"}

@app.get("/pegar-comando")
def pegar_comando():
    if not fila_comandos.empty():
        cmd = fila_comandos.get()
        return cmd
    return {"status": "vazio"}