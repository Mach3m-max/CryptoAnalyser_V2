// HTT v2 dashboard.js

const socket = io();

socket.on('update', function(data) {
  updatePrices(data.prices || {});
  updatePositions(data.portfolio || {});
  updateSignals(data.signals || {});
  updateBalances(data.balances || {});
  updateHeader(data);
});

function updateHeader(data) {
  const modeEl = document.getElementById('modeLabel');
  if (modeEl) {
    modeEl.textContent = data.mode || 'DEMO';
    modeEl.className = 'badge ' + (data.mode === 'REAL' ? 'bg-danger' : 'bg-primary');
  }
  const tradingEl = document.getElementById('tradingStatus');
  if (tradingEl) {
    tradingEl.innerHTML = data.trading_allowed
      ? '<span class="status-dot green"></span>Торговля активна'
      : '<span class="status-dot red"></span>Торговля выключена';
  }
}

function updatePrices(prices) {
  Object.entries(prices).forEach(([pair, info]) => {
    const el = document.getElementById('price_' + pair);
    if (el) el.textContent = formatPrice(info.price || info);
  });
}

function updatePositions(portfolio) {
  const tbody = document.getElementById('positionsBody');
  if (!tbody) return;
  const entries = Object.entries(portfolio).filter(([,v]) => v && v.qty > 0);
  if (entries.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-3">Нет открытых позиций</td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(([pair, pos]) => {
    const pnl    = pos.pnl_pct || 0;
    const cls    = pnl > 0 ? 'pnl-positive' : pnl < 0 ? 'pnl-negative' : 'pnl-zero';
    const sign   = pnl > 0 ? '+' : '';
    return '<tr>'
      + '<td><strong>' + pair + '</strong></td>'
      + '<td>' + formatPrice(pos.entry_price) + '</td>'
      + '<td id="price_' + pair + '">' + formatPrice(pos.current_price) + '</td>'
      + '<td class="' + cls + '">' + sign + pnl.toFixed(2) + '%</td>'
      + '<td class="' + cls + '">' + sign + (pos.pnl_usdt||0).toFixed(2) + ' USDT</td>'
      + '<td>' + pos.qty + '</td>'
      + '<td><button class="btn btn-danger btn-sm" onclick="closePosition(' + JSON.stringify(pair) + ')">X</button></td>'
      + '</tr>';
  }).join('');
}

function updateSignals(signals) {
  const tbody = document.getElementById('signalsBody');
  if (!tbody) return;
  if (Object.keys(signals).length === 0) return;
  tbody.innerHTML = Object.entries(signals).map(([pair, s]) => {
    const sig  = s.signal || 'HOLD';
    const cls  = sig === 'BUY' ? 'signal-buy' : sig === 'SELL' ? 'signal-sell' : 'signal-hold';
    const conf = ((s.confidence || 0) * 100).toFixed(0);
    const dev  = (s.avg_deviation || 0).toFixed(2);
    return '<tr>'
      + '<td><strong>' + pair + '</strong></td>'
      + '<td class="' + cls + '">' + sig + '</td>'
      + '<td>' + conf + '%<div class="conf-bar"><div class="conf-fill" style="width:' + conf + '%"></div></div></td>'
      + '<td>' + dev + '%</td>'
      + '<td id="price_' + pair + '">' + formatPrice(s.price) + '</td>'
      + '<td><button class="btn btn-success btn-sm" onclick="openTrade(' + JSON.stringify(pair) + ')">Купить</button></td>'
      + '</tr>';
  }).join('');
}

function updateBalances(balances) {
  const el = document.getElementById('usdtBalance');
  if (el && balances.USDT !== undefined) {
    el.textContent = parseFloat(balances.USDT).toFixed(2) + ' USDT';
  }
}

async function closePosition(pair) {
  if (!confirm('Закрыть позицию ' + pair + '?')) return;
  const r = await fetch('/api/close_position', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pair: pair})
  });
  const d = await r.json();
  if (!d.success) alert('Ошибка: ' + (d.error || 'неизвестная'));
}

async function openTrade(pair) {
  const amount = prompt('Сумма USDT для ' + pair + ':', '100');
  if (!amount) return;
  const r = await fetch('/api/buy', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pair: pair, amount: parseFloat(amount)})
  });
  const d = await r.json();
  alert(d.success ? 'Ордер выставлен' : 'Ошибка: ' + (d.error || ''));
}

async function toggleTrading() {
  await fetch('/api/toggle_trading', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}'
  });
}

function formatPrice(p) {
  if (!p) return '-';
  p = parseFloat(p);
  if (p >= 1000) return p.toLocaleString('ru-RU', {maximumFractionDigits: 2});
  if (p >= 1)    return p.toFixed(4);
  return p.toFixed(6);
}

function formatTime(ts) {
  if (!ts) return '-';
  return new Date(ts).toLocaleString('ru-RU');
}
