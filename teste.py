"""Teste de ponta a ponta sem navegador.

Gera uma fala em inglês, envia para o servidor em tempo real (como a página
faria) e mostra as legendas e as vozes que voltam. O servidor precisa estar
rodando (iniciar.bat ou python server.py).
"""

import asyncio
import base64
import json
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
import av
import numpy as np

from server import PORTA, sintetizar

TEXTO = (
    "Welcome back to the course. In this lesson, we are going to learn how neural "
    "networks are trained. First, we feed the input data through the network to get a "
    "prediction. Then we compare that prediction with the correct answer and measure "
    "the error. Finally, we adjust the weights a little bit, so that next time the "
    "error is smaller. Does that make sense? Great, let's look at a simple example."
)


async def main():
    pasta = Path(tempfile.mkdtemp())
    mp3 = pasta / "ingles.mp3"
    mp3.write_bytes(await sintetizar(TEXTO, "en-US-GuyNeural"))
    conversor = av.AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(str(mp3)) as arquivo:
        quadros = [q for f in arquivo.decode(audio=0) for q in conversor.resample(f)]
    pcm = np.concatenate([q.to_ndarray().reshape(-1) for q in quadros])
    dur = len(pcm) / 16000
    print(f"Áudio de teste: {dur:.1f}s de fala em inglês\n")

    inicio = time.perf_counter()
    legendas = vozes = 0
    inicios, pior = {}, 0.0

    async with aiohttp.ClientSession() as s, s.ws_connect(f"ws://127.0.0.1:{PORTA}/ws") as ws:
        async def enviar():
            for i in range(0, len(pcm), 1600):
                await ws.send_bytes(pcm[i:i + 1600].tobytes())
                # segue o relógio real, como o navegador faria
                await asyncio.sleep(max(0, inicio + (i + 1600) / 16000 - time.perf_counter()))
            for _ in range(30):  # 3 s de silêncio para fechar a última frase
                await ws.send_bytes(np.zeros(1600, np.int16).tobytes())
                await asyncio.sleep(0.1)

        tarefa = asyncio.create_task(enviar())
        try:
            while True:
                msg = await ws.receive(timeout=12)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                m = json.loads(msg.data)
                t = time.perf_counter() - inicio
                if m["tipo"] == "legenda":
                    legendas += 1
                    inicios[m["id"]] = m["inicio"]
                    print(f"[{t:5.1f}s] ({m['inicio']:.1f}-{m['fim']:.1f}s) EN: {m['en']}\n         PT: {m['pt']}")
                elif m["tipo"] == "voz":
                    vozes += 1
                    dados = base64.b64decode(m["audio"])
                    (pasta / f"voz{m['id']}.mp3").write_bytes(dados)
                    demora = t - inicios[m["id"]]
                    pior = max(pior, demora)
                    print(f"[{t:5.1f}s] voz {m['id']} pronta {demora:.1f}s depois do início da frase")
                elif m["tipo"] == "estado":
                    print(f"[{t:5.1f}s] transcrição a {m['velocidade']}x do tempo real, fila {m['atraso']}s")
                elif m["tipo"] == "aviso":
                    print(f"[{t:5.1f}s] AVISO: {m['texto']}")
        except asyncio.TimeoutError:
            pass
        tarefa.cancel()

    print(f"\n{legendas} legendas e {vozes} falas recebidas. Arquivos em {pasta}")
    print(f"Atraso de vídeo necessário para sincronizar este trecho: {pior:.1f}s")
    sys.exit(0 if legendas and vozes == legendas else 1)


asyncio.run(main())
