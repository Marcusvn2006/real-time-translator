# Tradutor ao vivo (inglês → português)

Assista a vídeos e cursos em inglês com dublagem em português gerada na hora.

O programa escuta o áudio de uma aba do navegador, transcreve o que é dito, traduz
para o português e fala a tradução. O vídeo é exibido com alguns segundos de atraso
para que a voz em português fique em sincronia com a imagem.

## Como funciona

1. A página captura o vídeo e o áudio da aba do curso (compartilhamento de aba do Chrome/Edge).
2. Um servidor local transcreve o áudio com [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
3. Cada frase é traduzida pelo Google Tradutor e falada com [edge-tts](https://github.com/rany2/edge-tts).
4. A página mostra o vídeo atrasado, com a legenda e a voz em português no tempo certo.

## Requisitos

- Windows 10 ou 11
- Python 3.10 ou mais recente
- Google Chrome ou Microsoft Edge
- Conexão com a internet (para a tradução e a voz)

Não precisa de placa de vídeo: a transcrição roda no processador.

## Instalação

No PowerShell, dentro da pasta do projeto:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Uso

1. Dê dois cliques em `iniciar.bat`. Na primeira vez o modelo de transcrição é baixado.
   A página abre sozinha em `http://localhost:8765`.
2. Abra o curso em outra aba do Chrome ou Edge e dê play no vídeo.
3. Na página do tradutor, clique em **Iniciar**, escolha a **aba** do curso e marque
   **"Compartilhar também o áudio da guia"**.
4. Assista pela página do tradutor.

### Controles

| Ação | Como |
|---|---|
| Pausar / continuar | botão **Pausar**, clique no vídeo ou **Espaço** |
| Voltar 10 s | botão **⏪ 10 s** ou **seta para a esquerda** |
| Avançar 10 s | botão **10 s ⏩** ou **seta para a direita** |
| Ir para o ponto mais recente | botão **⏭ Mais recente** |
| Tela cheia | botão **Tela cheia** ou duplo clique no vídeo |

Dá para voltar até cerca de 3 minutos e avançar até o ponto mais recente já traduzido.
A pausa é local: a aba do curso continua tocando e sendo gravada. Para pular para outra
parte da aula, use a aba do curso.

A página também permite escolher a voz, o atraso do vídeo, os volumes da tradução e do
áudio original, e desligar a voz para ficar só com as legendas.

## Configuração

Variáveis de ambiente opcionais, definidas antes de abrir o `iniciar.bat`:

| Variável | Padrão | Para que serve |
|---|---|---|
| `TRADUTOR_MODELO` | `base.en` | Modelo de transcrição: `tiny.en` (mais rápido), `base.en`, `small.en` (mais preciso) |
| `TRADUTOR_PORTA` | `8765` | Porta do servidor local |
| `TRADUTOR_THREADS` | até 8 | Núcleos do processador usados na transcrição |

## Teste sem navegador

Com o servidor rodando:

```powershell
.venv\Scripts\python teste.py
```

Ele gera uma fala em inglês, envia ao servidor e mostra as legendas e as vozes recebidas.

## Limitações e avisos

- **Privacidade**: a transcrição é feita no seu computador, mas o texto de cada frase é
  enviado ao Google Tradutor e ao serviço de voz da Microsoft.
- **Serviços não oficiais**: a tradução e a voz usam endpoints gratuitos que não são
  APIs oficiais. Servem para uso pessoal, podem ficar lentos, limitar o uso ou parar de
  funcionar sem aviso, e não devem ser usados em um produto comercial.
- **Antivírus**: programas que inspecionam conexões HTTPS (por exemplo, Kaspersky) podem
  deixar a voz mais lenta.
- **Atraso**: o vídeo fica cerca de 8 segundos atrás do original, tempo necessário para
  transcrever, traduzir e gerar a voz.
- Use apenas com conteúdo ao qual você tem acesso legítimo e para consumo próprio.
