"""Tradutor de áudio em tempo real: inglês -> português falado.

O navegador captura o áudio da aba do curso e envia para este servidor local,
que transcreve (faster-whisper), traduz (Google Tradutor) e gera a fala em
português (edge-tts). O resultado volta para a página, que mostra a legenda
e toca a voz.
"""

import asyncio
import base64
import json
import os
import re
import socket
import time
import webbrowser
from collections import deque
from pathlib import Path

# Usa os certificados do Windows (como o navegador). Sem isso, antivírus que
# inspecionam HTTPS (ex.: Kaspersky) fazem a tradução e a voz falharem.
import truststore
truststore.inject_into_ssl()

import edge_tts
import numpy as np
from aiohttp import TCPConnector, WSMsgType, web
from aiohttp.abc import AbstractResolver
import requests
from deep_translator import MyMemoryTranslator
from faster_whisper import WhisperModel

# ---------------------------------------------------------------- configuração
PORTA = int(os.environ.get("TRADUTOR_PORTA", "8765"))
# Modelos: tiny.en (mais rápido) < base.en < small.en (mais preciso, mais lento)
MODELO = os.environ.get("TRADUTOR_MODELO", "base.en")
THREADS = int(os.environ.get("TRADUTOR_THREADS", str(min(8, os.cpu_count() or 4))))
VOZ_PADRAO = "pt-BR-FranciscaNeural"
# O português sai mais longo que o inglês. Pedir a voz já um pouco mais rápida soa
# mais natural do que o navegador acelerar o áudio depois.
RITMO_VOZ = "+10%"

SR = 16000           # taxa de amostragem esperada do navegador
QUADRO = 480         # 30 ms
SEG_MAX = 6.0        # corta o trecho à força depois disso (segundos)
LIMIAR = 0.012       # volume mínimo (RMS) para considerar que há fala
ESPERA_FRASE = 1.0   # quanto esperar o resto de uma frase incompleta (segundos)
ATRASO_MAX = 25.0    # se a fila de áudio passar disso, descarta o mais antigo

PASTA = Path(__file__).parent
RUIDO = re.compile(r"\[[^\]]*\]|[♪♫*]+")
TEM_PALAVRA = re.compile(r"\w")
FIM_DE_FRASE = re.compile(r"[.!?][\"')\]]?(?=\s|$)")


# ------------------------------------------------------------------ segmentação
class Segmentador:
    """Agrupa o áudio contínuo em trechos de fala, cortando nas pausas."""

    def __init__(self):
        self.resto = np.zeros(0, np.float32)
        self.pre = deque(maxlen=10)   # 300 ms antes do início da fala
        self.quadros = []
        self.rms = []
        self.silencio = 0
        self.nivel = 0.0              # pico recente, para ganho automático
        self.pos = 0                  # quadros já recebidos (relógio do áudio)
        self.inicio = 0.0             # instante em que o trecho atual começou

    def alimentar(self, pcm):
        saida = []
        dados = np.concatenate([self.resto, pcm])
        n = len(dados) // QUADRO
        self.resto = dados[n * QUADRO:]
        for i in range(n):
            q = dados[i * QUADRO:(i + 1) * QUADRO]
            r = float(np.sqrt(np.mean(q * q)))
            # ganho automático: compensa vídeos com volume baixo
            self.nivel = max(self.nivel * 0.9995, r)
            if self.nivel > 0.0008:
                ganho = min(20.0, max(1.0, 0.08 / self.nivel))
                q = q * ganho
                r *= ganho
            fala = r > LIMIAR
            self.pos += 1

            if not self.quadros:
                if fala:
                    self.inicio = (self.pos - 1 - len(self.pre)) * 0.03
                    self.quadros = [x for x, _ in self.pre] + [q]
                    self.rms = [x for _, x in self.pre] + [r]
                    self.pre.clear()
                    self.silencio = 0
                else:
                    self.pre.append((q, r))
                continue

            self.quadros.append(q)
            self.rms.append(r)
            self.silencio = 0 if fala else self.silencio + 1
            dur = len(self.quadros) * 0.03
            sil = self.silencio * 0.03
            if sil >= 0.45 or (dur >= 3 and sil >= 0.24) or (dur >= 4.5 and sil >= 0.12):
                self._emitir(len(self.quadros), saida, self.silencio)
            elif dur >= SEG_MAX:
                # sem pausa: corta no ponto mais silencioso dos últimos 2 s
                k = 66
                suave = np.convolve(self.rms[-k:], np.ones(5) / 5, mode="same")
                self._emitir(len(self.rms) - k + int(np.argmin(suave)) + 1, saida, 0)
        return saida

    def _emitir(self, corte, saida, silencio_final):
        """Entrega (áudio, início, fim) com os tempos em segundos desde o começo."""
        com_fala = sum(1 for r in self.rms[:corte] if r > LIMIAR)
        if com_fala >= 8:  # ignora estalos com menos de ~0,25 s de fala
            fim = self.inicio + (corte - silencio_final) * 0.03
            saida.append((np.clip(np.concatenate(self.quadros[:corte]), -1, 1), self.inicio, fim))
        self.inicio += corte * 0.03
        self.quadros = self.quadros[corte:]
        self.rms = self.rms[corte:]
        self.silencio = 0


# ------------------------------------------------------------------ modelos
modelo = None


def transcrever(audio, contexto):
    segmentos, _ = modelo.transcribe(
        audio,
        language="en",
        beam_size=1,
        temperature=0.0,
        vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=contexto or None,
        without_timestamps=True,
    )
    partes = []
    for s in segmentos:
        if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
            continue  # provavelmente não é fala
        if s.compression_ratio > 2.4:
            continue  # texto repetitivo = alucinação do modelo
        partes.append(s.text.strip())
    # tira marcações que não são fala, como [Music] e ♪
    return RUIDO.sub(" ", " ".join(partes)).strip()


http = requests.Session()


def traduzir(texto):
    try:  # Google Tradutor: rápido e com boa qualidade
        r = http.get("https://translate.googleapis.com/translate_a/single",
                     params={"client": "gtx", "sl": "en", "tl": "pt", "dt": "t", "q": texto},
                     timeout=6)
        r.raise_for_status()
        return "".join(p[0] for p in r.json()[0] if p[0]).strip()
    except Exception:  # reserva caso o Google recuse a requisição
        return MyMemoryTranslator(source="en-US", target="pt-BR").translate(texto) or ""


class ResolvedorTestado(AbstractResolver):
    """Resolve o DNS e devolve só os endereços que realmente aceitam conexão.

    O serviço de voz da Microsoft às vezes anuncia um IP que não responde, e a
    conexão ficava presa nele até estourar o tempo limite.
    """

    def __init__(self):
        self.cache = {}

    async def resolve(self, host, port=0, family=socket.AF_INET):
        chave = (host, port)
        achado = self.cache.get(chave)
        if achado and time.monotonic() - achado[0] < 300:
            return achado[1]
        achados = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
        infos = [{"hostname": host, "host": a[4][0], "port": port, "family": a[0],
                  "proto": 0, "flags": socket.AI_NUMERICHOST} for a in achados]

        async def responde(info):
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(info["host"], port), 1.5)
                w.close()
                return True
            except Exception:
                return False

        ok = await asyncio.gather(*(responde(i) for i in infos))
        bons = [i for i, r in zip(infos, ok) if r] or infos
        self.cache[chave] = (time.monotonic(), bons)
        return bons

    async def close(self):
        pass


resolvedor = ResolvedorTestado()


async def pedir_voz(texto, voz, ritmo):
    audio = bytearray()
    conector = TCPConnector(resolver=resolvedor, use_dns_cache=False)
    fala = edge_tts.Communicate(texto, voz, rate=ritmo, connector=conector, connect_timeout=4)
    async for parte in fala.stream():
        if parte["type"] == "audio":
            audio += parte["data"]
    return bytes(audio)


async def sintetizar(texto, voz, ritmo="+0%"):
    """Gera a fala. O serviço às vezes leva vários segundos só para aceitar a conexão
    normalmente leva 1 s; por isso, se a resposta demora (ou falha), um segundo e
    um terceiro pedido saem em paralelo e vale o que terminar primeiro."""
    relogio = asyncio.get_running_loop().time
    tarefas, erro = set(), None
    try:
        for espera in (1.5, 2.5, 10.0):
            tarefas.add(asyncio.create_task(pedir_voz(texto, voz, ritmo)))
            limite = relogio() + espera
            falhou = False
            while tarefas and not falhou and relogio() < limite:
                prontas, _ = await asyncio.wait(tarefas, timeout=limite - relogio(),
                                                return_when=asyncio.FIRST_COMPLETED)
                for t in prontas:
                    tarefas.discard(t)
                    if t.exception() is None:
                        return t.result()
                    erro, falhou = t.exception(), True
            if isinstance(erro, edge_tts.exceptions.NoAudioReceived):
                raise erro                      # texto sem nada falável: repetir não adianta
            if falhou:
                resolvedor.cache.clear()
        raise erro or TimeoutError("o serviço de voz não respondeu a tempo")
    finally:
        for t in tarefas:
            t.cancel()


# ------------------------------------------------------------------ sessão
class Sessao:
    def __init__(self, ws):
        self.ws = ws
        self.seg = Segmentador()
        self.fila_audio = asyncio.Queue()
        self.fila_texto = asyncio.Queue()
        self.atraso = 0.0
        self.voz = VOZ_PADRAO
        self.falar = True
        self.pendente = ""
        self.pend_inicio = 0.0        # tempos do texto que espera o fim da frase
        self.pend_fim = 0.0
        self.contexto = ""
        self.n = 0

    async def enviar(self, **msg):
        try:
            await self.ws.send_str(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    async def executar(self):
        tarefas = [asyncio.create_task(self.laco_transcricao()),
                   asyncio.create_task(self.laco_fala())]
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.BINARY:
                    pcm = np.frombuffer(msg.data, dtype=np.int16).astype(np.float32) / 32768
                    for trecho in self.seg.alimentar(pcm):
                        self.atraso += len(trecho[0]) / SR
                        self.fila_audio.put_nowait(trecho)
                    await self.limitar_atraso()
                elif msg.type == WSMsgType.TEXT:
                    cfg = json.loads(msg.data)
                    self.voz = cfg.get("voz", self.voz)
                    self.falar = cfg.get("falar", self.falar)
        finally:
            for t in tarefas:
                t.cancel()

    async def limitar_atraso(self):
        if self.atraso <= ATRASO_MAX:
            return
        descartado = 0.0
        while self.atraso > 8 and not self.fila_audio.empty():
            d = len(self.fila_audio.get_nowait()[0]) / SR
            self.atraso -= d
            descartado += d
        await self.enviar(tipo="aviso", texto=f"O computador não acompanhou: {descartado:.0f}s de "
                          "áudio foram pulados. Considere um modelo mais leve (tiny.en).")

    def frases_prontas(self, forcar):
        """Devolve (texto, início, fim) das frases completas, ou None."""
        p = self.pendente
        if not p:
            return None
        if forcar:
            self.pendente = ""
            return p, self.pend_inicio, self.pend_fim
        fins = list(FIM_DE_FRASE.finditer(p))
        if not fins:
            return None
        corte = fins[-1].end()
        self.pendente = p[corte:].strip()
        inicio = self.pend_inicio
        # o que sobrou começa, em proporção, perto do fim do trecho
        meio = self.pend_fim - (self.pend_fim - inicio) * len(self.pendente) / len(p)
        self.pend_inicio = meio
        return p[:corte].strip(), inicio, meio

    async def laco_transcricao(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                audio, inicio, fim = await asyncio.wait_for(self.fila_audio.get(), ESPERA_FRASE)
            except asyncio.TimeoutError:
                # a fala parou: solta o que ficou esperando o fim da frase
                pronto = self.frases_prontas(forcar=True)
                if pronto:
                    await self.fila_texto.put(pronto)
                continue
            dur = len(audio) / SR
            self.atraso -= dur
            t0 = time.perf_counter()
            try:
                texto = await loop.run_in_executor(None, transcrever, audio, self.contexto[-200:])
            except Exception as e:
                await self.enviar(tipo="aviso", texto=f"Erro na transcrição: {e}")
                continue
            gasto = time.perf_counter() - t0
            if TEM_PALAVRA.search(texto):
                self.contexto = (self.contexto + " " + texto)[-400:]
                if not self.pendente:
                    self.pend_inicio = inicio
                self.pend_fim = fim
                self.pendente = (self.pendente + " " + texto).strip()
            pronto = self.frases_prontas(forcar=len(self.pendente) > 240)
            if pronto:
                await self.fila_texto.put(pronto)
            await self.enviar(tipo="estado", atraso=round(max(0.0, self.atraso), 1),
                              velocidade=round(dur / gasto, 1) if gasto else 0)

    async def laco_fala(self):
        loop = asyncio.get_running_loop()
        while True:
            en, inicio, fim = await self.fila_texto.get()
            self.n += 1
            try:
                pt = await loop.run_in_executor(None, traduzir, en)
            except Exception as e:
                await self.enviar(tipo="aviso", texto=f"Erro na tradução (sem internet?): {e}")
                await self.enviar(tipo="legenda", id=self.n, en=en, pt="", inicio=round(inicio, 2), fim=round(fim, 2))
                continue
            await self.enviar(tipo="legenda", id=self.n, en=en, pt=pt, inicio=round(inicio, 2), fim=round(fim, 2))
            if not self.falar:
                continue
            if not TEM_PALAVRA.search(pt):   # só pontuação ("..."): não há o que falar
                await self.enviar(tipo="semvoz", id=self.n)
                continue
            try:
                mp3 = await sintetizar(pt, self.voz, RITMO_VOZ)
            except Exception as e:
                await self.enviar(tipo="semvoz", id=self.n)   # a página mostra só a legenda
                if not isinstance(e, edge_tts.exceptions.NoAudioReceived):
                    await self.enviar(tipo="aviso", texto=f"Erro ao gerar a voz: {e}")
                continue
            await self.enviar(tipo="voz", id=self.n, audio=base64.b64encode(mp3).decode())


# ------------------------------------------------------------------ servidor
async def rota_ws(request):
    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)
    await Sessao(ws).executar()
    return ws


async def rota_pagina(request):
    return web.FileResponse(PASTA / "index.html", headers={"Cache-Control": "no-store"})


async def rota_worklet(request):
    return web.FileResponse(PASTA / "captura.js", headers={"Cache-Control": "no-store"})


async def ao_iniciar(app):
    url = f"http://localhost:{PORTA}"
    print(f"\nPronto! Abra {url} no Chrome ou Edge.  (Ctrl+C para encerrar)\n")
    if not os.environ.get("TRADUTOR_SEM_NAVEGADOR"):
        webbrowser.open(url)


def main():
    global modelo
    print(f"Carregando o modelo de transcrição '{MODELO}' "
          "(na primeira vez ele é baixado, pode demorar alguns minutos)...")
    modelo = WhisperModel(MODELO, device="cpu", compute_type="int8", cpu_threads=THREADS)
    app = web.Application()
    app.add_routes([web.get("/", rota_pagina),
                    web.get("/captura.js", rota_worklet),
                    web.get("/ws", rota_ws)])
    app.on_startup.append(ao_iniciar)
    web.run_app(app, host="127.0.0.1", port=PORTA, print=None)


if __name__ == "__main__":
    main()
