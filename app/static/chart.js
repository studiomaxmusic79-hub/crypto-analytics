/* Self-contained OHLC renderer: no CDN, analytics trackers or remote scripts. */
class CandleChart {
  constructor(canvas, onHover) {
    this.canvas = canvas; this.ctx = canvas.getContext('2d'); this.onHover = onHover;
    this.candles = []; this.visible = 70; this.offset = 0; this.hover = null;
    this.drag = null; this.width = 0; this.height = 0;
    new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
    canvas.addEventListener('wheel', event => {
      event.preventDefault(); this.zoom(event.deltaY < 0 ? 0.8 : 1.25);
    }, {passive: false});
    canvas.addEventListener('pointerdown', event => {
      this.drag = {x: event.clientX, y: event.clientY, offset: this.offset};
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointermove', event => {
      const bounds = canvas.getBoundingClientRect();
      const x = event.clientX - bounds.left, y = event.clientY - bounds.top;
      if (this.drag && Math.abs(event.clientX - this.drag.x) > 5) {
        this.offset = Math.max(0, Math.min(this.candles.length - this.visible,
          this.drag.offset + Math.round((event.clientX - this.drag.x) / Math.max(this.step, 1))));
      }
      this.hover = {x, y}; this.draw();
    });
    const release = () => { this.drag = null; };
    canvas.addEventListener('pointerup', release); canvas.addEventListener('pointercancel', release);
    canvas.addEventListener('pointerleave', () => { if (!this.drag) {this.hover = null; this.draw();} });
  }
  resize() {
    const rect = this.canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    this.width = rect.width; this.height = rect.height;
    this.canvas.width = Math.round(rect.width * dpr); this.canvas.height = Math.round(rect.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0); this.draw();
  }
  setData(candles, reset = false) {
    this.candles = candles;
    if (reset) {this.offset = 0; this.hover = null;}
    this.offset = Math.min(this.offset, Math.max(0, candles.length - this.visible));
    this.draw();
  }
  zoom(factor) {this.visible = Math.min(180, Math.max(20, Math.round(this.visible * factor))); this.offset = Math.min(this.offset, Math.max(0, this.candles.length - this.visible)); this.draw();}
  reset() {this.visible = 70; this.offset = 0; this.hover = null; this.draw();}
  draw() {
    const ctx = this.ctx, w = this.width, h = this.height;
    ctx.clearRect(0, 0, w, h); if (!this.candles.length || w < 100) return;
    const end = this.candles.length - this.offset;
    const data = this.candles.slice(Math.max(0, end - this.visible), end);
    const left = 12, right = w - 65, top = 19, bottom = h * 0.76, volumeBottom = h - 24;
    const highest = Math.max(...data.map(c => c.high)), lowest = Math.min(...data.map(c => c.low));
    const padding = Math.max((highest - lowest) * 0.12, highest * 0.0005);
    const hi = highest + padding, lo = lowest - padding;
    const priceY = p => top + (hi - p) / (hi - lo) * (bottom - top);
    this.step = (right - left) / data.length;
    ctx.font = '9px -apple-system, BlinkMacSystemFont, Segoe UI, Arial'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const price = hi - (hi - lo) * i / 4, y = priceY(price);
      ctx.strokeStyle = '#edf1e9'; ctx.beginPath(); ctx.moveTo(left, y + 0.5); ctx.lineTo(right, y + 0.5); ctx.stroke();
      ctx.fillStyle = '#9aa994'; ctx.fillText(price.toLocaleString('en-US', {maximumFractionDigits: price > 10000 ? 0 : 2}), right + 9, y + 3);
    }
    const maxVol = Math.max(1, ...data.map(c => c.volume));
    data.forEach((c, i) => {
      const x = left + (i + 0.5) * this.step, up = c.close >= c.open;
      ctx.strokeStyle = up ? '#5a9f7e' : '#ca9383'; ctx.fillStyle = ctx.strokeStyle;
      ctx.globalAlpha = c.close_time >= Date.now() ? 0.55 : 1;
      ctx.beginPath(); ctx.moveTo(x, priceY(c.high)); ctx.lineTo(x, priceY(c.low)); ctx.stroke();
      ctx.fillRect(x - this.step * 0.29, Math.min(priceY(c.open), priceY(c.close)), Math.max(1, this.step * 0.58), Math.max(1, Math.abs(priceY(c.open) - priceY(c.close))));
      ctx.globalAlpha = 0.23;
      const vh = c.volume / maxVol * (volumeBottom - bottom - 12);
      ctx.fillRect(x - this.step * 0.3, volumeBottom - vh, Math.max(1, this.step * 0.6), vh); ctx.globalAlpha = 1;
    });
    const last = data[data.length - 1], lastY = priceY(last.close);
    ctx.strokeStyle = '#87b198'; ctx.setLineDash([3, 4]); ctx.beginPath(); ctx.moveTo(left, lastY); ctx.lineTo(right, lastY); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = '#eef6ed'; ctx.fillRect(right + 2, lastY - 9, 63, 18);
    ctx.fillStyle = '#61936e'; ctx.fillText(last.close.toLocaleString('en-US', {maximumFractionDigits: 2}), right + 6, lastY + 3);
    for (let i = 0; i < 5; i++) {
      const index = Math.min(data.length - 1, Math.round(i * (data.length - 1) / 4));
      const x = left + (index + 0.5) * this.step, d = new Date(data[index].time);
      const text = d.toLocaleString('ru-RU', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'}).replace(',', '');
      ctx.fillStyle = '#a1ad98'; ctx.textAlign = i === 0 ? 'left' : i === 4 ? 'right' : 'center';
      ctx.fillText(text, x, h - 6); ctx.textAlign = 'left';
    }
    let selected = last;
    if (this.hover && this.hover.x >= left && this.hover.x <= right) {
      const index = Math.max(0, Math.min(data.length - 1, Math.floor((this.hover.x - left) / this.step)));
      selected = data[index]; const x = left + (index + .5) * this.step;
      ctx.strokeStyle = '#abb9a0'; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, volumeBottom); ctx.stroke();
      if (this.hover.y >= top && this.hover.y <= bottom) {ctx.beginPath(); ctx.moveTo(left, this.hover.y); ctx.lineTo(right, this.hover.y); ctx.stroke();}
      ctx.setLineDash([]); ctx.fillStyle = '#416c51'; ctx.beginPath(); ctx.arc(x, priceY(selected.close), 3, 0, 2 * Math.PI); ctx.fill();
    }
    this.onHover(selected);
  }
}
