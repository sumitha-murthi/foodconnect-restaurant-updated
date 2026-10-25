const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function showCsv(url, el) {
  const rows = (await (await fetch(url)).text()).trim().split(/\r?\n/).map(r => r.split(','));
  el.innerHTML = '<table><tr>' + rows[0].map(h => `<th>${esc(h)}</th>`).join('') + '</tr>' +
    rows.slice(1).map(r => '<tr>' + r.map(c => `<td>${esc(c)}</td>`).join('') + '</tr>').join('') + '</table>';
}
