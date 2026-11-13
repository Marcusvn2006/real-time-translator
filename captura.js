// Roda na thread de áudio: junta o som em blocos de 100 ms e manda para a página.
const SILENCIO = new Float32Array(128);

class Captura extends AudioWorkletProcessor {
  constructor() {
    super();
    this.bloco = new Float32Array(1600);
    this.pos = 0;
  }
  process(inputs) {
    // Sem som na aba o navegador não entrega nada; contamos silêncio mesmo assim,
    // para o relógio do áudio nunca ficar atrás do relógio do vídeo.
    const canal = (inputs[0] && inputs[0][0]) || SILENCIO;
    for (let i = 0; i < canal.length; i++) {
      this.bloco[this.pos++] = canal[i];
      if (this.pos === this.bloco.length) {
        this.port.postMessage(this.bloco.slice());
        this.pos = 0;
      }
    }
    return true;
  }
}
registerProcessor("captura", Captura);
